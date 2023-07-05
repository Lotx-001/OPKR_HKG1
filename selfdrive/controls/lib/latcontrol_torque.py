import math

from cereal import log
from common.numpy_fast import interp
from selfdrive.controls.lib.latcontrol import LatControl, MIN_STEER_SPEED
from selfdrive.controls.lib.pid import PIDController
from selfdrive.controls.lib.drive_helpers import apply_deadzone
from selfdrive.controls.lib.vehicle_model import ACCELERATION_DUE_TO_GRAVITY

from common.params import Params
from decimal import Decimal

# At higher speeds (25+mph) we can assume:
# Lateral acceleration achieved by a specific car correlates to
# torque applied to the steering rack. It does not correlate to
# wheel slip, or to speed.

# This controller applies torque to achieve desired lateral
# accelerations. To compensate for the low speed effects we
# use a LOW_SPEED_FACTOR in the error. Additionally, there is
# friction in the steering wheel that needs to be overcome to
# move it at all, this is compensated for too.


FRICTION_THRESHOLD = 0.2


class LatControlTorque(LatControl):
  def __init__(self, CP, CI):
    super().__init__(CP, CI)
    self.CP = CP

    self.mpc_frame = 0
    self.params = Params()
    
    self.kf = CP.lateralTuning.torque.kf

    self.pid = PIDController(CP.lateralTuning.torque.kp, CP.lateralTuning.torque.ki,
                             k_f=self.kf, pos_limit=self.steer_max, neg_limit=-self.steer_max)
    self.get_steer_feedforward = CI.get_steer_feedforward_function()
    self.use_steering_angle = CP.lateralTuning.torque.useSteeringAngle
    self.friction = CP.lateralTuning.torque.friction
    self.steering_angle_deadzone_deg = CP.lateralTuning.torque.steeringAngleDeadzoneDeg

    self.live_tune_enabled = False

    self.lt_timer = 0

  def live_tune(self, CP):
    self.mpc_frame += 1
    if self.mpc_frame % 300 == 0:
      self.max_lat_accel = float(Decimal(self.params.get("TorqueMaxLatAccel", encoding="utf8")) * Decimal('0.1'))
      self.kp = float(Decimal(self.params.get("TorqueKp", encoding="utf8")) * Decimal('0.1')) / self.max_lat_accel
      self.kf = float(Decimal(self.params.get("TorqueKf", encoding="utf8")) * Decimal('0.1')) / self.max_lat_accel
      self.ki = float(Decimal(self.params.get("TorqueKi", encoding="utf8")) * Decimal('0.1')) / self.max_lat_accel
      self.friction = float(Decimal(self.params.get("TorqueFriction", encoding="utf8")) * Decimal('0.001'))
      self.use_steering_angle = self.params.get_bool('TorqueUseAngle')
      self.steering_angle_deadzone_deg = float(Decimal(self.params.get("TorqueAngDeadZone", encoding="utf8")) * Decimal('0.1'))
      self.pid = PIDController(self.kp, self.ki,
                              k_f=self.kf, pos_limit=1.0, neg_limit=-1.0)
        
      self.mpc_frame = 0

  def update(self, active, CS, CP, VM, params, last_actuators, desired_curvature, desired_curvature_rate, llk):
    self.lt_timer += 1
    if self.lt_timer > 100:
      self.lt_timer = 0
      self.live_tune_enabled = self.params.get_bool("OpkrLiveTunePanelEnable")
    if self.live_tune_enabled:
      self.live_tune(CP)

    pid_log = log.ControlsState.LateralTorqueState.new_message()

    if CS.vEgo < MIN_STEER_SPEED or not active:
      output_torque = 0.0
      pid_log.active = False
    else:
      if self.use_steering_angle:
        actual_curvature = -VM.calc_curvature(math.radians(CS.steeringAngleDeg - params.angleOffsetDeg), CS.vEgo, params.roll)
        curvature_deadzone = abs(VM.calc_curvature(math.radians(self.steering_angle_deadzone_deg), CS.vEgo, 0.0))
      else:
        actual_curvature_vm = -VM.calc_curvature(math.radians(CS.steeringAngleDeg - params.angleOffsetDeg), CS.vEgo, params.roll)
        actual_curvature_llk = llk.angularVelocityCalibrated.value[2] / CS.vEgo
        actual_curvature = interp(CS.vEgo, [2.0, 5.0], [actual_curvature_vm, actual_curvature_llk])
        curvature_deadzone = 0.0
      desired_lateral_accel = desired_curvature * CS.vEgo ** 2

      # desired rate is the desired rate of change in the setpoint, not the absolute desired curvature
      #desired_lateral_jerk = desired_curvature_rate * CS.vEgo ** 2
      actual_lateral_accel = actual_curvature * CS.vEgo ** 2
      lateral_accel_deadzone = curvature_deadzone * CS.vEgo ** 2


      low_speed_factor = interp(CS.vEgo, [0, 10, 20], [500, 500, 200])
      setpoint = desired_lateral_accel + low_speed_factor * desired_curvature
      measurement = actual_lateral_accel + low_speed_factor * actual_curvature
      error = setpoint - measurement
      pid_log.error = error

      ff = desired_lateral_accel - params.roll * ACCELERATION_DUE_TO_GRAVITY
      # convert friction into lateral accel units for feedforward
      friction_compensation = interp(apply_deadzone(error, lateral_accel_deadzone), [-FRICTION_THRESHOLD, FRICTION_THRESHOLD], [-self.friction, self.friction])
      ff += friction_compensation / self.kf
      freeze_integrator = CS.steeringRateLimited or CS.steeringPressed or CS.vEgo < 5
      output_torque = self.pid.update(error,
                                      feedforward=ff,
                                      speed=CS.vEgo,
                                      freeze_integrator=freeze_integrator)

      pid_log.active = True
      pid_log.p = self.pid.p
      pid_log.i = self.pid.i
      pid_log.d = self.pid.d
      pid_log.f = self.pid.f
      pid_log.output = -output_torque
      pid_log.saturated = self._check_saturation(self.steer_max - abs(output_torque) < 1e-3, CS)
      pid_log.actualLateralAccel = actual_lateral_accel
      pid_log.desiredLateralAccel = desired_lateral_accel

    # TODO left is positive in this convention
    return -output_torque, 0.0, pid_log
