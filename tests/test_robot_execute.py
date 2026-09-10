import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from scripts.robot_bridge import Bridge
from tests.robot_fakes import FakeArm, ManualClock


@pytest.fixture
def bridge():
    clock = ManualClock()
    b = Bridge(lambda: ({}, {}), clock=clock, sleep=clock.sleep)
    b.arms = {side: FakeArm() for side in ("left", "right")}
    return b


def request(degrees=5, **kwargs):
    return {
        "arm": "left",
        "kind": "joint_target",
        "joints_rad": np.deg2rad([degrees, 0, 0, 0, 0, 0]).tolist(),
        **kwargs,
    }


@pytest.mark.parametrize("duration", [0.001, 0.5, 61])
def test_execute_has_no_size_speed_temperature_or_observation_gate(bridge, duration):
    arm = bridge.arms["left"]
    arm.temperature = 65
    arm.velocity[0] = np.deg2rad(100)
    before = bridge.clock()
    result = bridge.execute(request(100, duration_s=duration))
    assert result["status"] == "completed"
    np.testing.assert_allclose(arm.q, np.deg2rad([100, 0, 0, 0, 0, 0]))
    assert result["actual"]["temperature_c"] == 65
    assert bridge.clock() - before == pytest.approx(duration)
    assert not arm.closed


def test_relative_action_uses_live_joints_and_can_be_repeated_without_observation(bridge):
    bridge.observe()
    bridge.clock.sleep(1000)
    bridge.arms["left"].q[0] = 0.2
    delta = request(5, kind="joint_delta")
    assert bridge.execute(delta)["status"] == "completed"
    assert bridge.execute(delta)["status"] == "completed"
    assert bridge.arms["left"].q[0] == pytest.approx(0.2 + np.deg2rad(10))


@pytest.mark.parametrize(
    "change",
    [
        {"joints_rad": [0] * 5},
        {"joints_rad": [float("nan")] * 6},
        {"joints_rad": [4, 0, 0, 0, 0, 0]},
        {"duration_s": 0},
        {"duration_s": float("inf")},
        {"arm": "unknown"},
        {"extra_field": 1},
        {
            "kind": "ee_target",
            "joints_rad": None,
            "position_m": [10] * 3,
            "quaternion_xyzw": [0, 0, 0, 1],
        },
        {"joints_rad": np.deg2rad([0, 200, 0, 0, 0, 0]).tolist()},
    ],
)
def test_bad_request_does_not_poison_session(bridge, change):
    result = bridge.execute(request(**change))
    assert result["status"] == "rejected"
    assert not bridge.arms["left"].commands
    assert not bridge.faults
    assert bridge.execute(request())["status"] == "completed"


def test_session_must_exist(bridge):
    bridge.arms.clear()
    assert bridge.execute(request())["status"] == "rejected"


@pytest.mark.parametrize(
    "problem", ["unhealthy", "stale", "invalid_feedback", "read_failure", "write_failure"]
)
def test_control_fault_latches_only_affected_arm_without_releasing(bridge, problem):
    arm = bridge.arms["left"]
    original_sleep = bridge.sleep

    def fail(dt):
        original_sleep(dt)
        if problem == "unhealthy":
            arm.healthy = False
        elif problem == "stale":
            arm.feedback_age = 0.2
        elif problem == "invalid_feedback":
            arm.q[0] = float("nan")
        elif problem == "read_failure":
            arm.read = lambda: (_ for _ in ()).throw(RuntimeError("read failed"))
        else:
            arm.command = lambda q: (_ for _ in ()).throw(RuntimeError("write failed"))

    bridge.sleep = fail
    result = bridge.execute(request())
    assert result["status"] == "stopped"
    assert result["fault_latched"]
    assert len(arm.commands) == 1
    assert not arm.closed
    assert bridge.execute(request())["fault_latched"]
    bridge.sleep = original_sleep
    assert bridge.execute(request(arm="right"))["status"] == "completed"


def test_unhealthy_start_is_a_control_fault(bridge):
    bridge.arms["left"].healthy = False
    result = bridge.execute(request())
    assert result["fault_latched"]
    assert not bridge.arms["left"].commands


def test_tracking_failure_holds_measured_pose(bridge):
    bridge.arms["left"].frozen = True
    result = bridge.execute(request())
    assert result["status"] == "stopped"
    assert "tracking" in result["reason"]
    assert result["hold"] == "powered hold requested"
    np.testing.assert_array_equal(bridge.arms["left"].commands[-1], np.zeros(6))


def test_result_reports_error_without_task_success_threshold(bridge):
    bridge.arms["left"].frozen = True
    result = bridge.execute(request(2.5))
    assert result["status"] == "completed"
    assert result["joint_error_rad"][0] == pytest.approx(-np.deg2rad(2.5))
    assert "reached" not in result


def test_agent_explicitly_selects_return(bridge):
    assert bridge.execute(request(20))["status"] == "completed"
    assert bridge.arms["left"].q[0] > 0
    assert bridge.execute(request(0))["status"] == "completed"
    np.testing.assert_array_equal(bridge.arms["left"].q, np.zeros(6))
    assert not bridge.arms["left"].closed


def test_close_move_and_reopen_gripper_preserves_arm_and_jaw_state(bridge):
    arm = bridge.arms["left"]
    arm.q[0] = 0.2
    close = {"arm": "left", "kind": "gripper_target", "gripper_opening": 0, "duration_s": 2}
    result = bridge.execute(close)
    assert result["status"] == "completed"
    assert result["target_gripper_opening"] == 0
    assert result["gripper_error"] == 0
    assert arm.q[0] == 0.2
    assert arm.commands == []
    assert arm.gripper_commands[0] == 1
    assert arm.gripper_commands[-1] == 0
    assert bridge.execute(request())["status"] == "completed"
    assert arm.opening == 0
    assert bridge.execute({**close, "gripper_opening": 1})["status"] == "completed"
    assert arm.opening == 1
    assert not arm.closed


@pytest.mark.parametrize(
    "change",
    [
        {"gripper_opening": None},
        {"gripper_opening": -0.01},
        {"gripper_opening": 1.01},
        {"gripper_opening": float("nan")},
        {"joints_rad": [0] * 6},
        {"frame": "tool"},
        {"kind": "joint_target", "joints_rad": [0] * 6},
    ],
)
def test_gripper_rejection_can_be_corrected_without_restarting(bridge, change):
    action = {"arm": "left", "kind": "gripper_target", "gripper_opening": 0}
    result = bridge.execute({**action, **change})
    assert result["status"] == "rejected"
    assert result["error"]["retryable"]
    assert bridge.arms["left"].gripper_commands == []
    assert bridge.execute(action)["status"] == "completed"


def test_stop_during_gripper_action_holds_intermediate_jaw_position(bridge):
    arm = bridge.arms["left"]
    sleep = bridge.sleep

    def interrupt(dt):
        sleep(dt)
        if arm.opening < 0.8:
            bridge.session("stop", "left")

    bridge.sleep = interrupt
    result = bridge.execute({"arm": "left", "kind": "gripper_target", "gripper_opening": 0})
    assert result["status"] == "stopped"
    assert not result["fault_latched"]
    assert result["hold"] == "powered hold requested"
    assert 0 < arm.opening < 0.8
    assert arm.gripper_commands[-1] == arm.opening
    assert not arm.closed


def test_gripper_failure_keeps_arm_session_and_reports_details(bridge):
    arm = bridge.arms["left"]

    def fail(opening):
        raise RuntimeError("gripper command lock timed out")

    arm.command_gripper = fail
    result = bridge.execute({"arm": "left", "kind": "gripper_target", "gripper_opening": 0})
    assert result["status"] == "stopped"
    assert result["fault_latched"]
    assert "gripper command lock" in result["error"]["message"]
    assert not arm.closed
    assert bridge.execute(request(arm="right"))["status"] == "completed"


def test_two_arms_can_execute_while_observing_and_stopping_one(bridge):
    ready = threading.Barrier(3)
    resume = threading.Event()
    seen = set()

    def wait_once(dt):
        ident = threading.get_ident()
        if ident not in seen:
            seen.add(ident)
            ready.wait(timeout=5)
            assert resume.wait(timeout=5)

    bridge.sleep = wait_once
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(bridge.execute, request(duration_s=0.04))
        right = pool.submit(bridge.execute, request(arm="right", duration_s=0.04))
        try:
            ready.wait(timeout=5)
            assert set(bridge.observe()["arms"]) == {"left", "right"}
            assert bridge.execute(request())["status"] == "rejected"
            assert bridge.session("release", "left", supported=True)["error"]["code"] == "arm_busy"
            bridge.session("stop", "left")
        finally:
            resume.set()
        assert left.result(timeout=5)["status"] == "stopped"
        assert right.result(timeout=5)["status"] == "completed"
    assert not bridge.faults  # Explicit stop is not a hardware fault.
    bridge.sleep = lambda dt: None
    assert bridge.execute(request())["status"] == "completed"
    assert not any(arm.closed for arm in bridge.arms.values())


def test_rejection_feedback_contains_numbers_needed_to_correct_target(bridge):
    bad = request(joints_rad=[4, 0, 0, 0, 0, 0])
    result = bridge.execute(bad)
    assert result["request"] == bad
    assert result["last_feedback"]["joints_rad"] == [0] * 6
    error = result["error"]
    assert error["code"] == "joint_limits"
    assert error["retryable"]
    assert error["details"]["violating_joints_1based"] == [1]
    limits = error["details"]["joint_limits_rad"]
    assert limits[0][1] < 4
    correction = request(joints_rad=[limits[0][1] / 2, 0, 0, 0, 0, 0])
    assert bridge.execute(correction)["status"] == "completed"


def test_collision_feedback_identifies_bodies_and_intermediate_pose(bridge):
    result = bridge.execute(request(joints_rad=np.deg2rad([0, 200, 0, 0, 0, 0]).tolist()))
    details = result["error"]["details"]
    assert result["error"]["code"] == "self_collision"
    assert len(details["bodies"]) == 2
    assert len(details["sampled_joints_rad"]) == 6
    assert details["penetration_m"] > 0.001


def test_ik_feedback_reports_residuals_and_tolerances(bridge):
    result = bridge.execute(
        {
            "arm": "left",
            "kind": "ee_target",
            "position_m": [10] * 3,
            "quaternion_xyzw": [0, 0, 0, 1],
        }
    )
    error = result["error"]
    assert error["code"] == "ik_unreachable"
    assert error["details"]["position_error_m"] > error["details"]["position_tolerance_m"]
    assert error["retryable"]


def test_invalid_numbers_return_serializable_field_feedback(bridge):
    import json

    result = bridge.execute(request(duration_s=float("nan")))
    json.dumps(result, allow_nan=False)
    assert result["error"]["details"]["fields"][0]["loc"] == ["duration_s"]
    assert result["request"]["duration_s"] == "nan"


def test_latched_fault_explains_original_failure(bridge):
    bridge.arms["left"].feedback_age = 1
    first = bridge.execute(request())
    assert first["error"]["details"]["feedback_age_s"] == 1
    assert not first["error"]["retryable"]
    next_result = bridge.execute(request())
    assert next_result["error"]["details"]["original_fault"] == first["error"]
