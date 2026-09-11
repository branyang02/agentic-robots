"""Attempt completion checks and continuation feedback; never chooses robot motions."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentic_robots.bridge import Bridge, RobotError

# Completion tolerances, not motion limits. Report actual residuals to the agent.
NEUTRAL_RAD = math.radians(3)
STATIONARY_RAD_S = 0.05
TERMINAL = {"success", "blocked", "needs_intervention", "paused"}


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    outcome: Literal["retry", "success", "blocked", "needs_intervention", "paused"]
    summary: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    correction: str = ""
    constraint: str = ""
    intervention: Literal["", "control_unavailable", "physical_obstruction"] = ""

    @model_validator(mode="after")
    def explained(self):
        if not all(item.strip() for item in self.evidence):
            raise ValueError("Evidence must identify observed frames/timestamps or tool feedback")
        if self.outcome == "retry" and not self.correction:
            raise ValueError("A retry needs a correction explaining what will change")
        if self.outcome in {"blocked", "needs_intervention"} and not self.constraint:
            raise ValueError(
                "Identify the specific constraint and why alternatives cannot solve it"
            )
        if self.outcome == "needs_intervention" and not self.intervention:
            raise ValueError("Identify unavailable control or an observed physical obstruction")
        return self


def neutral_feedback(state):
    """Check both measured arms, preserving exact feedback and per-arm explanations."""
    problems = {}
    for side in ("left", "right"):
        arm = state.get("arms", {}).get(side)
        try:
            if arm is None:
                raise ValueError("No measured arm state")
            Bridge.healthy(arm)
            if side in state.get("faults", {}):
                raise ValueError("Control fault remains latched; inspect recovery feedback")
            if max(abs(q) for q in arm["joints_rad"]) > NEUTRAL_RAD:
                raise ValueError("Measured joints have not reached neutral")
            if max(abs(v) for v in arm["velocity_rad_s"]) > STATIONARY_RAD_S:
                raise ValueError("Arm is still moving")
        except (ValueError, KeyError, RobotError) as exc:
            problems[side] = str(exc)
    if state.get("errors") or state.get("error"):
        problems["feedback"] = state.get("errors") or state["error"]
    return {
        "verified": not problems,
        "checked": state,
        "problems": problems,
        "neutral_tolerance_rad": NEUTRAL_RAD,
        "stationary_tolerance_rad_s": STATIONARY_RAD_S,
    }


def control_unavailable(state):
    """A recoverable tracking latch alone does not establish unavailable control."""
    if state.get("errors") or state.get("error"):
        return True
    for side in ("left", "right"):
        try:
            Bridge.healthy(state.get("arms", {}).get(side, {}))
        except (ValueError, KeyError, RobotError):
            return True
        fault = state.get("faults", {}).get(side)
        if fault and fault.get("code") != "tracking_error":
            return True
    return False


def continuation(status, page, after_turn):
    """Return an idle completed turn to resume, or None. Never interrupt active/failed turns."""
    task = status.get("task", {})
    if not task or task.get("phase") in TERMINAL or task.get("watch_paused"):
        return None
    if page.get("thread", {}).get("status", {}).get("type") != "idle":
        return None
    turn = next(iter(page.get("turns", [])), {})
    if turn.get("status") != "completed" or not turn.get("id") or turn["id"] == after_turn:
        return None
    return turn["id"]


def continuation_message(status, feedback):
    return (
        "The robot task is still active; your previous turn ended before its recorded "
        "completion requirements were satisfied. Continue the existing task using the "
        "initialization instructions and the feedback below.\n\n"
        f"Recording/task state: {status}\nCurrent controller feedback: {feedback}"
    )
