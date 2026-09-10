"""Recovery uses simulated arms and never opens a hardware connection."""

import numpy as np
import pytest

from scripts.robot_bridge import Bridge
from tests.robot_fakes import FakeArm, ManualClock


@pytest.fixture
def stopped_bridge():
    clock = ManualClock()
    bridge = Bridge(lambda: ({}, {}), clock=clock, sleep=clock.sleep)
    bridge.arms = {side: FakeArm() for side in ("left", "right")}
    arm = bridge.arms["left"]
    arm.command_gripper(0.2)
    arm.frozen = True
    result = bridge.execute(
        {"arm": "left", "kind": "joint_target", "joints_rad": [0.2, 0, 0, 0, 0, 0]}
    )
    assert result["error"]["code"] == "tracking_error"
    arm.frozen = False
    return bridge


def test_recover_holds_actual_pose_preserves_jaws_then_allows_correction(stopped_bridge):
    bridge = stopped_bridge
    arm = bridge.arms["left"]
    arm.q[0] = 0.1  # Feedback changed since the failed action; use it, not the old target.
    before, jaws = len(arm.commands), list(arm.gripper_commands)
    fault = bridge.faults["left"]
    result = bridge.session("recover", "left")
    assert result["status"] == "recovered"
    assert result["cleared_fault"] == fault
    assert not result["fault_latched"]
    assert result["hold"] == "powered hold verified"
    assert result["actual"]["joints_rad"] == [0.1, 0, 0, 0, 0, 0]
    np.testing.assert_array_equal(arm.commands[before:], [[0.1, 0, 0, 0, 0, 0]])
    assert arm.gripper_commands == jaws
    assert arm.opening == 0.2
    assert not bridge.faults
    assert not bridge.arms["right"].commands
    assert not any(device.closed for device in bridge.arms.values())

    # Repeating a successful recovery has no command side effects.
    assert bridge.session("recover", "left")["status"] == "no_fault"
    assert len(arm.commands) == before + 1
    correction = bridge.execute(
        {"arm": "left", "kind": "joint_delta", "joints_rad": [0.02, 0, 0, 0, 0, 0]}
    )
    assert correction["status"] == "completed"
    assert correction["actual"]["joints_rad"][0] == pytest.approx(0.12)
    assert (
        bridge.execute({"arm": "left", "kind": "joint_target", "joints_rad": [0] * 6})["status"]
        == "completed"
    )
    np.testing.assert_array_equal(arm.q, np.zeros(6))
    assert not arm.closed


@pytest.mark.parametrize("phase", ["before_hold", "after_hold"])
@pytest.mark.parametrize("problem", ["unhealthy", "stale", "invalid_feedback", "read_failure"])
def test_recovery_keeps_fault_when_feedback_cannot_verify_hold(stopped_bridge, phase, problem):
    bridge = stopped_bridge
    arm = bridge.arms["left"]
    fault = bridge.faults["left"]
    count = len(arm.commands)

    def fail():
        if problem == "unhealthy":
            arm.healthy = False
        elif problem == "stale":
            arm.feedback_age = 1
        elif problem == "invalid_feedback":
            arm.q[0] = float("nan")
        else:
            arm.read = lambda: (_ for _ in ()).throw(RuntimeError("read failed"))

    if phase == "before_hold":
        fail()
    else:
        sleep = bridge.sleep

        def fail_after_command(dt):
            sleep(dt)
            fail()

        bridge.sleep = fail_after_command
    result = bridge.session("recover", "left")
    assert result["status"] == "stopped"
    assert result["fault_latched"]
    assert result["error"]["code"] == "recovery_failed"
    assert result["error"]["details"]["original_fault"] == fault
    assert result["error"]["details"]["check_error"]
    assert bridge.faults["left"] == fault
    assert len(arm.commands) == count + (phase == "after_hold")
    assert not arm.closed
    assert bridge.execute({"arm": "left", "kind": "joint_target", "joints_rad": [0] * 6})[
        "fault_latched"
    ]


def test_recovery_command_failure_is_reported_and_can_be_corrected(stopped_bridge):
    bridge = stopped_bridge
    arm = bridge.arms["left"]
    command = arm.command
    arm.command = lambda q: (_ for _ in ()).throw(RuntimeError("command lock timed out"))
    result = bridge.session("recover", "left")
    assert result["fault_latched"]
    assert "command lock timed out" in result["error"]["message"]
    assert result["error"]["recoverable"]  # Eligible again only after hold checks succeed.
    assert not arm.closed
    arm.command = command
    assert bridge.session("recover", "left")["status"] == "recovered"


def test_recovery_requires_tracking_the_hold_target(stopped_bridge):
    bridge = stopped_bridge
    arm = bridge.arms["left"]
    sleep = bridge.sleep

    def drift(dt):
        sleep(dt)
        arm.q[0] += 0.1

    bridge.sleep = drift
    result = bridge.session("recover", "left")
    assert result["fault_latched"]
    check = result["error"]["details"]["check_error"]
    assert check["code"] == "hold_unverified"
    assert check["details"]["error_rad"][0] == pytest.approx(0.1)
    assert result["last_feedback"]["joints_rad"][0] == pytest.approx(0.1)
    assert "left" in bridge.faults
    assert not arm.closed


@pytest.mark.parametrize("code", ["feedback_unavailable", "control_failure", "protection_fault"])
def test_recovery_refuses_other_faults_even_with_healthy_feedback(stopped_bridge, code):
    bridge = stopped_bridge
    arm = bridge.arms["left"]
    bridge.faults["left"] = {"code": code, "message": "original fault"}
    count = len(arm.commands)
    result = bridge.session("recover", "left")
    assert result["fault_latched"]
    assert result["error"]["code"] == "recovery_not_allowed"
    assert not result["error"]["recoverable"]
    assert bridge.faults["left"]["code"] == code
    assert len(arm.commands) == count
    assert not arm.closed


def test_recovery_shares_action_lock_and_respects_stop(stopped_bridge):
    bridge = stopped_bridge
    arm = bridge.arms["left"]
    count = len(arm.commands)
    with bridge.locks["left"]:
        assert bridge.session("recover", "left")["error"]["code"] == "arm_busy"
    assert len(arm.commands) == count
    sleep = bridge.sleep

    def stop_during_hold(dt):
        sleep(dt)
        assert (
            bridge.execute({"arm": "left", "kind": "joint_target", "joints_rad": [0] * 6})["error"][
                "code"
            ]
            == "arm_busy"
        )
        assert bridge.session("recover", "left")["error"]["code"] == "arm_busy"
        assert bridge.session("release", "left", supported=True)["error"]["code"] == "arm_busy"
        bridge.session("stop", "left")

    bridge.sleep = stop_during_hold
    result = bridge.session("recover", "left")
    assert result["fault_latched"]
    assert "Stop requested" in result["error"]["message"]
    assert "left" in bridge.faults
    assert not arm.closed
    bridge.sleep = sleep
    assert bridge.session("recover", "left")["status"] == "recovered"


def test_recovery_requires_arm_session_and_cannot_reset_communication(stopped_bridge):
    bridge = stopped_bridge
    count = len(bridge.arms["left"].commands)
    assert bridge.session("recover")["status"] == "rejected"
    assert bridge.session("recover", "unknown")["status"] == "rejected"
    assert bridge.session("recover", "left", reset_communication=True)["status"] == "rejected"
    assert len(bridge.arms["left"].commands) == count
    bridge.arms.pop("right")
    assert bridge.session("recover", "right")["error"]["code"] == "session_inactive"
