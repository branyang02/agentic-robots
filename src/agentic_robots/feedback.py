"""Measured action feedback for the camera-owning recorder; never sends motion."""

import copy
import threading

import numpy as np
from scipy.spatial.transform import Rotation

from agentic_robots.bridge import Bridge, Motion, vector


class ActionFeedback:
    def __init__(self):
        self.motion = None
        self.lock = threading.Lock()

    def enrich(self, observation, action, result):
        """Compute FK from fresh measured joints, not the requested endpoint."""
        observation = copy.deepcopy(observation)
        errors = observation.setdefault("errors", {})
        for side in ("left", "right"):
            arm = observation.get("arms", {}).get(side)
            if arm is None:
                errors.setdefault(f"arm:{side}", "No measured arm state")
                continue
            arm["ee_pose"] = None
            arm["gripper"] = {
                "measured_opening": arm.get("gripper_opening"),
                "requested_opening": None,
                "opening_error": None,
                "command_status": None,
                "interrupted": False,
            }
            if side == action.arm and action.kind == "gripper_target":
                gripper = arm["gripper"]
                gripper.update(
                    requested_opening=action.gripper_opening,
                    command_status=result["status"],
                    interrupted=result["status"] == "stopped",
                )
                measured = arm.get("gripper_opening")
                if (
                    measured is not None
                    and np.isfinite(measured)
                    and action.gripper_opening is not None
                ):
                    gripper["opening_error"] = measured - action.gripper_opening
            try:
                Bridge.healthy(arm)
                opening = float(vector([arm["gripper_opening"]], 1)[0])
                with self.lock:
                    if self.motion is None:
                        self.motion = Motion()
                    pose = self.motion.fk(vector(arm["joints_rad"], 6), opening)
                arm["ee_pose"] = {
                    "position_m": pose[:3, 3].tolist(),
                    "quaternion_xyzw": Rotation.from_matrix(pose[:3, :3]).as_quat().tolist(),
                    "frame": f"{side}_base",
                    "site": "grasp_site",
                }
            except Exception as exc:
                errors[f"pose:{side}"] = str(exc)

        error = result.get("error", {})
        message = error.get("message", result.get("reason", "Inspect execution feedback"))
        diagnostics = [
            "Command completed; assess the post-action images and feedback for task success."
            if result["status"] == "completed"
            else f"{result['status']}: {message}"
        ]
        if error.get("next_step"):
            diagnostics.append(error["next_step"])
        if action.kind == "gripper_target" and result["status"] == "stopped":
            diagnostics.append(
                "Jaw action interrupted: recovery does not finish the requested opening. "
                "Inspect measured opening and reassess the grasp before carrying."
            )
        if errors:
            diagnostics.append(
                "Post-action evidence incomplete: "
                + "; ".join(f"{k}: {v}" for k, v in errors.items())
            )
        return {**result, "post_action": observation, "diagnostics": diagnostics}
