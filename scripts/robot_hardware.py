"""Thin adapter for the pinned i2rt driver. Only explicit close releases torque."""

import copy
import fcntl
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import can
import numpy as np

from scripts.setup_can import inventory, ready, resolve

STARTUP_LOCK = threading.Lock()


def enable_once(interface, motor_id, motor_type, reset_communication):
    reply = interface._send_message_get_response(motor_id, motor_id, [255] * 7 + [252], max_retry=1)
    state = interface.parse_recv_message(reply, motor_type, ignore_error=True)
    if state.error_code == "0xd" and reset_communication:
        interface.bus.send(
            can.Message(arbitration_id=motor_id, data=[255] * 7 + [251], is_extended_id=False)
        )
        interface._drain_bus(timeout_s=0.02, idle_count=2)
        reply = interface._send_message_get_response(
            motor_id, motor_id, [255] * 7 + [252], max_retry=1
        )
        state = interface.parse_recv_message(reply, motor_type)
    if state.error_code != "0x1":
        raise RuntimeError(
            f"Motor {motor_id}: {state.error_message}; no automatic protection reset"
        )
    return state


class Hardware:
    def __init__(self, side, reset_communication=False):
        from i2rt.motor_drivers.dm_driver import DMSingleMotorCanInterface
        from i2rt.robots.get_robot import get_yam_robot
        from i2rt.robots.utils import ArmType, GripperType

        row = resolve(os.environ[f"{side.upper()}_CAN"], inventory())
        if not ready(row):
            raise RuntimeError("CAN not ready; use README setup-can instructions")
        calibration = json.loads(
            (Path("calibration") / f"{os.environ.get('ROBOT_ID', 'dual-yam')}.json").read_text()
        )[side]
        limits = np.asarray(calibration["gripper_limits"], dtype=float)
        if (
            calibration["adapter_serial"] != row["serial"]
            or limits.shape != (2,)
            or not np.isfinite(limits).all()
            or abs(limits[0] - limits[1]) < 1
        ):
            raise ValueError("Gripper calibration/adapter mismatch")
        directory = Path(f"/tmp/agentic-arms-{os.getuid()}")
        directory.mkdir(mode=0o700, exist_ok=True)
        self.lock = (directory / f"{row['serial']}.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with can.Bus(interface="socketcan", channel=row["name"]) as bus:
                if bus.recv(0.3) is not None:
                    raise RuntimeError("CAN already active; another controller may own this arm")
        except Exception:
            self.lock.close()  # No motor was enabled; a corrected startup can retry.
            raise

        def enable(interface, motor_id, motor_type):
            return enable_once(interface, motor_id, motor_type, reset_communication)

        with STARTUP_LOCK, patch.object(DMSingleMotorCanInterface, "motor_on", enable):
            self.robot = get_yam_robot(
                channel=row["name"],
                arm_type=ArmType.YAM,
                gripper_type=GripperType.LINEAR_4310,
                zero_gravity_mode=False,
                gripper_limits_override=limits,
                gripper_kp=0.0,
                gripper_kd=0.0,
                enable_auto_recovery=False,
            )
        self.gripper_gains = GripperType.LINEAR_4310.get_motor_kp_kd(ArmType.YAM)
        # Startup leaves the jaws passive. The driver's force limiter must use the
        # same nonzero gain as an eventual explicit jaw command (it divides by kp).
        self.robot._gripper_force_limiter._kp = self.gripper_gains[0]
        self.motor_state = self.joint_state = None
        self.motor_time = self.joint_time = time.monotonic()
        self.read_lock = threading.Lock()

    def cached(self, now):
        if not hasattr(self, "snapshot"):
            raise RuntimeError("No initial feedback")
        snapshot, captured_at = self.snapshot
        state = dict(snapshot)
        state["feedback_age_s"] += now - captured_at
        state["healthy"] = self.robot.motor_chain.running and self.robot._server_thread.is_alive()
        return state

    def read(self):
        if not self.read_lock.acquire(timeout=0.01):
            return self.cached(time.monotonic())
        try:
            return self._read()
        finally:
            self.read_lock.release()

    def _read(self):
        robot, now = self.robot, time.monotonic()
        if not robot.motor_chain.state_lock.acquire(timeout=0.01):
            return self.cached(now)
        try:
            state = robot.motor_chain.state
            if state is not self.motor_state:
                self.motor_state, self.motor_time = state, now
        finally:
            robot.motor_chain.state_lock.release()
        if not robot._state_lock.acquire(timeout=0.01):
            return self.cached(now)
        try:
            state = robot._joint_state
            if state is not self.joint_state:
                self.joint_state, self.joint_time = state, now
            result = {
                "joints_rad": state.pos[:6].tolist(),
                "velocity_rad_s": state.vel[:6].tolist(),
                "gripper_opening": float(state.pos[6]),
                "temperature_c": float(np.max(np.r_[state.temp_mos, state.temp_rotor])),
                "feedback_age_s": now - min(self.motor_time, self.joint_time),
                "healthy": robot.motor_chain.running and robot._server_thread.is_alive(),
            }
            self.snapshot = result, now
            return result
        finally:
            robot._state_lock.release()

    def command(self, q):
        robot = self.robot
        if not robot._command_lock.acquire(timeout=0.01):
            raise RuntimeError("Motor command lock timed out")
        try:
            command = copy.deepcopy(robot._commands)
            command.pos[:6] = q
            command.vel[:6], command.torques[:6] = 0, 0
            command.kp[:6], command.kd[:6] = robot._kp[:6], robot._kd[:6]
            robot._commands = command  # Atomic replacement; never briefly install zero arm gains.
        finally:
            robot._command_lock.release()

    def command_gripper(self, opening):
        robot = self.robot
        if not np.isfinite(opening) or not 0 <= opening <= 1:
            raise ValueError("Gripper opening must be finite and between 0 and 1")
        if not robot._command_lock.acquire(timeout=0.01):
            raise RuntimeError("Motor command lock timed out")
        try:
            command = copy.deepcopy(robot._commands)
            command.pos[6] = robot.remapper.to_robot_joint_pos_space(np.r_[np.zeros(6), opening])[6]
            command.vel[6], command.torques[6] = 0, 0
            command.kp[6], command.kd[6] = self.gripper_gains
            robot._commands = command  # Preserve every arm command and gain while moving jaws.
        finally:
            robot._command_lock.release()

    def close(self):
        from scripts.calibrate import close_robot

        close_robot(self.robot)
        self.lock.close()
