"""Agent-selected actions with command, geometry, control, and ownership checks."""

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from scipy.spatial.transform import Rotation

from scripts.cameras import capture, configured_cameras

IK_POSITION_TOLERANCE = 0.001
IK_ROTATION_TOLERANCE = np.deg2rad(0.5)
FEEDBACK_TIMEOUT_S = 0.15
TRACKING_ERROR_RAD = np.deg2rad(3)


def json_ready(value):
    """Keep diagnostics valid JSON even when a failed sensor/request contains NaN."""
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, float) and not np.isfinite(value):
        return str(value)
    return value


class RobotError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def failure(exc, request=None, state=None, *, stopped=False, latched=False, hold=None):
    code = getattr(exc, "code", "control_failure" if latched else "invalid_command")
    details = getattr(exc, "details", {})
    if isinstance(exc, ValidationError):
        details = {"fields": exc.errors(include_url=False, include_context=False)}
    next_step = (
        "Inspect session status and the reported hardware fault. Motion on this arm is blocked; "
        "do not retry or clear motor protection faults automatically."
        if latched
        else "Correct the reported request/configuration or choose another target, then retry. "
        "No session restart is needed for a rejected action."
    )
    return json_ready(
        {
            "status": "stopped" if stopped else "rejected",
            "reason": str(exc),
            "error": {
                "code": code,
                "message": str(exc),
                "details": details,
                "retryable": not latched,
                "next_step": next_step,
            },
            "request": request,
            "last_feedback": state,
            "fault_latched": latched,
            "hold": hold,
        }
    )


def vector(value, size):
    result = np.asarray(value, dtype=float)
    if result.shape != (size,) or not np.isfinite(result).all():
        raise ValueError(f"Expected {size} finite numbers")
    return result


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arm: Literal["left", "right"]
    kind: Literal["joint_target", "joint_delta", "ee_target", "ee_delta", "gripper_target"]
    joints_rad: list[float] | None = None
    position_m: list[float] | None = None
    quaternion_xyzw: list[float] | None = None
    rotation_vector_rad: list[float] | None = None
    gripper_opening: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    frame: Literal["base", "tool", "world"] = "base"
    duration_s: float = Field(default=5, gt=0, allow_inf_nan=False)


class Motion:
    def __init__(self):
        import mujoco
        from i2rt.robots.kinematics import Kinematics
        from i2rt.robots.utils import ArmType, GripperType, combine_arm_and_gripper_xml

        self.mj = mujoco
        self.kin = Kinematics(
            combine_arm_and_gripper_xml(ArmType.YAM, GripperType.LINEAR_4310), "grasp_site"
        )
        self.model = self.kin._configuration.model
        self.data = mujoco.MjData(self.model)
        self.limits = self.model.jnt_range[:6]

    def model_q(self, joints, opening):
        return np.r_[joints, np.clip(opening, 0, 1) * self.model.jnt_range[6:, 1]]

    def fk(self, joints, opening=0):
        return self.kin.fk(self.model_q(joints, opening)).copy()

    def target(self, action, joints, opening=0):
        if action.kind == "gripper_target":
            if (
                action.gripper_opening is None
                or action.frame != "base"
                or any(
                    x is not None
                    for x in (
                        action.joints_rad,
                        action.position_m,
                        action.quaternion_xyzw,
                        action.rotation_vector_rad,
                    )
                )
            ):
                raise ValueError("Gripper actions take only gripper_opening (0 closed, 1 open)")
            return joints.copy()
        if action.gripper_opening is not None:
            raise ValueError("Use a separate gripper_target action to move the jaws")
        if action.frame == "world":
            raise ValueError("World-to-base calibration is not configured")
        if action.kind.startswith("joint"):
            if action.frame != "base" or any(
                x is not None
                for x in (action.position_m, action.quaternion_xyzw, action.rotation_vector_rad)
            ):
                raise ValueError("Joint actions take only joints_rad")
            q = vector(action.joints_rad, 6)
            return q + joints if action.kind == "joint_delta" else q
        if action.joints_rad is not None:
            raise ValueError("Cartesian actions cannot contain joint commands")
        current = self.fk(joints, opening)
        target = current.copy()
        if action.kind == "ee_target":
            if action.frame != "base" or action.rotation_vector_rad is not None:
                raise ValueError("EE targets use base-frame position and quaternion")
            target[:3, 3] = vector(action.position_m, 3)
            quaternion = vector(action.quaternion_xyzw, 4)
            if abs(np.linalg.norm(quaternion) - 1) > 0.001:
                raise ValueError("Quaternion must have unit length")
            target[:3, :3] = Rotation.from_quat(quaternion).as_matrix()
        else:
            if action.quaternion_xyzw is not None or (
                action.position_m is None and action.rotation_vector_rad is None
            ):
                raise ValueError("EE delta needs translation and/or rotation vector")
            translation = vector(action.position_m if action.position_m is not None else [0] * 3, 3)
            rotation = Rotation.from_rotvec(
                vector(
                    action.rotation_vector_rad
                    if action.rotation_vector_rad is not None
                    else [0] * 3,
                    3,
                )
            ).as_matrix()
            if action.frame == "tool":
                target[:3, 3] += current[:3, :3] @ translation
                target[:3, :3] = current[:3, :3] @ rotation
            else:
                target[:3, 3] += translation
                target[:3, :3] = rotation @ current[:3, :3]
        ok, solution = self.kin.ik(
            target,
            "grasp_site",
            init_q=self.model_q(joints, opening),
            pos_threshold=IK_POSITION_TOLERANCE,
            ori_threshold=IK_ROTATION_TOLERANCE,
        )
        q = vector(solution[:6], 6)
        reached = self.fk(q, opening)
        position_error = float(np.linalg.norm(reached[:3, 3] - target[:3, 3]))
        rotation_error = float(Rotation.from_matrix(target[:3, :3].T @ reached[:3, :3]).magnitude())
        if (
            not ok
            or position_error > IK_POSITION_TOLERANCE
            or rotation_error > IK_ROTATION_TOLERANCE
        ):
            raise RobotError(
                "ik_unreachable",
                "IK could not reach the requested endpoint",
                target_pose=target.tolist(),
                candidate_joints_rad=q.tolist(),
                position_error_m=position_error,
                rotation_error_rad=rotation_error,
                position_tolerance_m=IK_POSITION_TOLERANCE,
                rotation_tolerance_rad=float(IK_ROTATION_TOLERANCE),
            )
        return q

    def plan(self, action, joints, opening=0):
        if not np.isfinite(opening):
            raise ValueError("Gripper feedback must be finite")
        measured = vector(joints, 6)
        start = np.clip(measured, self.limits[:, 0], self.limits[:, 1])
        # Allow encoder quantization at a joint boundary, not an out-of-range pose.
        if np.max(abs(measured - start)) > 0.001:
            raise RobotError(
                "starting_joint_limits",
                "Measured starting pose exceeds model limits",
                measured_joints_rad=measured.tolist(),
                joint_limits_rad=self.limits.tolist(),
            )
        target = vector(self.target(action, start, opening), 6)
        if np.any(target < self.limits[:, 0] - 1e-8) or np.any(target > self.limits[:, 1] + 1e-8):
            raise RobotError(
                "joint_limits",
                "Joint limit exceeded",
                target_joints_rad=target.tolist(),
                joint_limits_rad=self.limits.tolist(),
                violating_joints_1based=(
                    np.flatnonzero(
                        (target < self.limits[:, 0] - 1e-8) | (target > self.limits[:, 1] + 1e-8)
                    )
                    + 1
                ).tolist(),
            )
        # Spatial sampling is independent of action duration. It never changes timing.
        samples = max(2, int(np.ceil(np.max(abs(target - start)) / np.deg2rad(0.5))) + 1)
        target_opening = action.gripper_opening if action.kind == "gripper_target" else opening
        samples = max(samples, int(np.ceil(abs(target_opening - opening) * 50)) + 1)
        for q, jaw in zip(
            np.linspace(start, target, samples), np.linspace(opening, target_opening, samples)
        ):
            self.data.qpos[:] = self.model_q(q, jaw)
            self.mj.mj_forward(self.model, self.data)
            for contact in self.data.contact:
                a, b = self.model.geom_bodyid[contact.geom1], self.model.geom_bodyid[contact.geom2]
                if (
                    contact.dist < -0.001
                    and self.model.body_parentid[a] != b
                    and self.model.body_parentid[b] != a
                ):
                    raise RobotError(
                        "self_collision",
                        "Modeled self-collision",
                        sampled_joints_rad=q.tolist(),
                        penetration_m=float(-contact.dist),
                        bodies=[self.model.body(a).name, self.model.body(b).name],
                    )
        return start, target


def camera_snapshot():
    """Return available image paths and per-camera errors; no camera is mandatory."""
    name = f"robot/{uuid.uuid4().hex}"

    def one(role):
        camera = configured_cameras((role,))[role]
        return str(Path(capture(f"{name}/{role}", camera, 0)["preview"]).resolve())

    paths, errors = {}, {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {role: pool.submit(one, role) for role in ("left", "right", "top")}
        for role, job in jobs.items():
            try:
                paths[role] = job.result()
            except Exception as exc:
                errors[f"camera:{role}"] = str(exc)
    return paths, errors


class Bridge:
    def __init__(
        self,
        capture_images=camera_snapshot,
        clock=time.monotonic,
        sleep=time.sleep,
        arm_factory=None,
    ):
        self.capture_images, self.clock, self.sleep = capture_images, clock, sleep
        self.arm_factory = arm_factory
        self.arms, self.faults = {}, {}
        self.locks = {side: threading.Lock() for side in ("left", "right")}
        self.stops = {side: threading.Event() for side in self.locks}
        self.motion, self.motion_lock = Motion(), threading.Lock()

    def status(self):
        arms, errors = {}, {}
        for side, device in tuple(self.arms.items()):
            try:
                arms[side] = device.read()
            except Exception as exc:
                errors[f"arm:{side}"] = str(exc)
        return {"arms": arms, "errors": errors, "faults": dict(self.faults)}

    def observe(self):
        started = time.time()
        try:
            paths, errors = self.capture_images()
        except Exception as exc:
            paths, errors = {}, {"cameras": str(exc)}
        images = {}
        for name, path in paths.items():
            try:
                with Image.open(path) as img:
                    img.load()
                    images[name] = {
                        "path": str(Path(path).resolve()),
                        "width": img.width,
                        "height": img.height,
                    }
            except Exception as exc:
                errors[f"camera:{name}"] = str(exc)
        state = self.status()
        state["errors"].update(errors)
        return {
            "capture_started_unix": started,
            "capture_finished_unix": time.time(),
            "images": images,
            **state,
            "frame": "arm base; no calibrated world transform",
        }

    def session(self, operation, arm=None, supported=False, reset_communication=False):
        request = dict(
            operation=operation,
            arm=arm,
            supported=supported,
            reset_communication=reset_communication,
        )
        if arm is not None and arm not in self.locks:
            return failure(ValueError("Specify left/right arm"), request)
        if operation == "status":
            return self.status()
        if operation == "stop":
            for side in [arm] if arm else self.stops:
                self.stops[side].set()
            return {"status": "stop requested; torque is not released"}
        if operation not in ("start", "release") or arm is None:
            return failure(ValueError("Specify start/release and left/right arm"), request)
        if not supported:
            return failure(ValueError("Startup/release requires physical support"), request)
        if not self.locks[arm].acquire(blocking=False):
            return failure(
                RobotError(
                    "arm_busy", "This arm is busy; stop its action before changing the session"
                ),
                request,
            )
        try:
            if operation == "start":
                if arm in self.arms:
                    return failure(
                        RobotError("already_connected", "Arm is already connected"), request
                    )
                factory = self.arm_factory
                if factory is None:
                    from scripts.robot_hardware import Hardware

                    factory = Hardware
                self.arms[arm] = factory(arm, reset_communication=reset_communication)
            elif arm in self.arms:
                self.arms[arm].close()
                del self.arms[arm]
                self.faults.pop(arm, None)
            return self.status()
        except Exception as exc:
            result = failure(
                RobotError(
                    "session_error",
                    str(exc),
                    operation=operation,
                    exception_type=type(exc).__name__,
                ),
                request,
            )
            result["error"]["next_step"] = (
                "Inspect the reported configuration/hardware problem before retrying. "
                "Startup failure may leave motors enabled; "
                "release failure may leave partial state. "
                "No automatic protection reset or torque release was performed."
            )
            return result
        finally:
            self.locks[arm].release()

    @staticmethod
    def healthy(state):
        vector(state["joints_rad"], 6)
        vector(state["velocity_rad_s"], 6)
        vector([state["gripper_opening"]], 1)
        if (
            not state["healthy"]
            or not np.isfinite(state["feedback_age_s"])
            or state["feedback_age_s"] < 0
            or state["feedback_age_s"] > FEEDBACK_TIMEOUT_S
        ):
            raise RobotError(
                "feedback_unavailable",
                "Motor feedback/control unavailable",
                feedback_age_s=state["feedback_age_s"],
                healthy=state["healthy"],
                maximum_feedback_age_s=FEEDBACK_TIMEOUT_S,
            )

    @staticmethod
    def hold(arm, gripper=False):
        try:
            state = arm.read()
            Bridge.healthy(state)
            arm.command(vector(state["joints_rad"], 6))
            if gripper:
                opening = float(state["gripper_opening"])
                if not np.isfinite(opening):
                    raise ValueError("Gripper feedback must be finite")
                arm.command_gripper(float(np.clip(opening, 0, 1)))
            return "powered hold requested"
        except Exception:
            return "unavailable; last command retained"

    def execute(self, action):
        request = action.model_dump() if isinstance(action, Action) else action
        try:
            action = Action.model_validate(action)
        except ValidationError as exc:
            return failure(exc, request)
        side = action.arm
        gripper = action.kind == "gripper_target"
        if not self.locks[side].acquire(blocking=False):
            return failure(RobotError("arm_busy", "This arm is busy"), request)
        arm, state = None, None
        try:
            if side not in self.arms:
                return failure(
                    RobotError("session_inactive", "Start the arm session first"), request
                )
            if side in self.faults:
                return failure(
                    RobotError(
                        "fault_latched",
                        "Control fault remains latched",
                        original_fault=self.faults[side],
                    ),
                    request,
                    stopped=True,
                    latched=True,
                )
            arm = self.arms[side]
            self.stops[side].clear()
            # The model is mutable. Serialize only planning, not arm execution/capture.
            with self.motion_lock:
                state = arm.read()
                self.healthy(state)
                try:
                    start, target = self.motion.plan(
                        action, state["joints_rad"], state["gripper_opening"]
                    )
                except ValueError as exc:
                    return failure(exc, request, state)
            steps = max(1, int(np.ceil(action.duration_s / 0.02)))
            dt = action.duration_s / steps
            previous = start
            opening = float(np.clip(state["gripper_opening"], 0, 1))
            for i in range(steps + 1):
                tick = self.clock()
                if self.stops[side].is_set():
                    raise InterruptedError("Stop requested")
                state = arm.read()
                self.healthy(state)
                if np.max(abs(np.asarray(state["joints_rad"]) - previous)) > TRACKING_ERROR_RAD:
                    raise RobotError(
                        "tracking_error",
                        "Joint tracking error exceeds 3 degrees",
                        previous_command_rad=previous.tolist(),
                        error_rad=(np.asarray(state["joints_rad"]) - previous).tolist(),
                        maximum_error_rad=float(TRACKING_ERROR_RAD),
                    )
                command = start + (target - start) * (i / steps)
                if gripper:
                    arm.command_gripper(opening + (action.gripper_opening - opening) * (i / steps))
                else:
                    arm.command(command)
                previous = command
                if i < steps:
                    self.sleep(max(0, dt - (self.clock() - tick)))
            state = arm.read()
            self.healthy(state)
            result = {
                "status": "completed",
                "target_rad": target.tolist(),
                "actual": state,
                "joint_error_rad": (np.asarray(state["joints_rad"]) - target).tolist(),
            }
            if gripper:
                result["target_gripper_opening"] = action.gripper_opening
                result["gripper_error"] = state["gripper_opening"] - action.gripper_opening
            return result
        except InterruptedError as exc:
            result = failure(
                RobotError("stop_requested", str(exc)),
                request,
                state,
                stopped=True,
                hold=self.hold(arm, gripper),
            )
            result["error"]["next_step"] = (
                "Action interrupted. Inspect the current state; a new action is allowed."
            )
            return result
        except Exception as exc:
            result = failure(
                exc, request, state, stopped=True, latched=True, hold=self.hold(arm, gripper)
            )
            self.faults[side] = result["error"]
            return result
        finally:
            self.locks[side].release()


def write_result(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_ready(result), indent=2, allow_nan=False) + "\n")
