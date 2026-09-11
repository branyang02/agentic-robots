"""Cartesian geometry and execution checks with simulated arms only."""

import numpy as np
import pytest
from scipy.spatial.transform import Rotation, Slerp

from agentic_robots.bridge import Action, Bridge, Motion, RobotError
from tests.robot_fakes import FakeArm, ManualClock


@pytest.mark.parametrize("frame", ["base", "tool"])
@pytest.mark.parametrize("duration", [0.01, 0.4])
def test_entire_path_tracks_translation_and_orientation(frame, duration):
    motion = Motion()
    start = np.deg2rad([0, 15, 30, 0, 20, 0])
    action = Action(
        arm="left",
        kind="ee_delta",
        path="cartesian",
        frame=frame,
        position_m=[0, 0, 0.02],
        rotation_vector_rad=[0, 0.04, 0],
        duration_s=duration,
    )
    path = motion.trajectory(action, start)
    first, last = motion.fk(start), motion.ee_pose(action, start)
    rotation = Slerp([0, 1], Rotation.from_matrix([first[:3, :3], last[:3, :3]]))
    assert len(path) >= 11  # Geometry sampling also applies to very short actions.
    for index, (a, b) in enumerate(zip(path[:-1], path[1:])):
        for t in [0, 0.25, 0.5, 0.75, 1]:
            fraction = (index + t) / (len(path) - 1)
            pose = motion.fk(a + t * (b - a))
            expected = first[:3, 3] + fraction * (last[:3, 3] - first[:3, 3])
            assert np.linalg.norm(pose[:3, 3] - expected) <= 0.001
            assert (
                rotation(fraction).inv() * Rotation.from_matrix(pose[:3, :3])
            ).magnitude() <= np.deg2rad(0.5)


def test_absolute_target_and_translation_only_preserve_orientation():
    motion = Motion()
    q = np.deg2rad([0, 15, 30, 0, 20, 0])
    before = motion.fk(q)
    a = Action(
        arm="left",
        kind="ee_target",
        path="cartesian",
        position_m=(before[:3, 3] + [0, 0, 0.015]).tolist(),
        quaternion_xyzw=(-Rotation.from_matrix(before[:3, :3]).as_quat()).tolist(),
        duration_s=0.4,
    )
    path = motion.trajectory(a, q)
    for joints in path:
        pose = motion.fk(joints)
        assert Rotation.from_matrix(before[:3, :3].T @ pose[:3, :3]).magnitude() <= np.deg2rad(0.5)
    assert np.linalg.norm(motion.fk(path[-1])[:3, 3] - a.position_m) <= 0.001


def make_bridge():
    clock = ManualClock()
    bridge = Bridge(lambda: ({}, {}), clock=clock, sleep=clock.sleep)
    bridge.arms["left"] = FakeArm()
    bridge.arms["left"].q = np.deg2rad([0, 15, 30, 0, 20, 0])
    return bridge


def command(**extra):
    return dict(
        arm="left",
        kind="ee_delta",
        path="cartesian",
        position_m=[0, 0, 0.02],
        duration_s=0.4,
        **extra,
    )


def test_execution_uses_full_path_and_preserves_jaw_and_duration():
    bridge = make_bridge()
    arm = bridge.arms["left"]
    first = bridge.motion.fk(arm.q, arm.opening)
    result = bridge.execute(command())
    assert result["status"] == "completed"
    assert result["path"]["samples"] == len(arm.commands)
    assert not arm.gripper_commands
    assert bridge.clock() == pytest.approx(0.4 + result["path"]["settling_s"])
    for q in arm.commands:
        pose = bridge.motion.fk(q, arm.opening)
        assert np.linalg.norm(pose[:2, 3] - first[:2, 3]) < 0.001


@pytest.mark.parametrize("failure", ["ik", "collision", "branch", "limits"])
def test_infeasible_intermediate_sample_rejects_before_any_command(monkeypatch, failure):
    bridge = make_bridge()
    original = bridge.motion.solve_ik
    calls = 0

    def solve(pose, seed, opening):
        nonlocal calls
        calls += 1
        if calls == 3:
            if failure == "ik":
                raise RobotError("ik_unreachable", "Intermediate pose is unreachable")
            q = original(pose, seed, opening)
            if failure == "branch":
                q[0] += 0.2
            elif failure == "limits":
                bridge.motion.limits[0, 1] = -1
            return q
        return original(pose, seed, opening)

    monkeypatch.setattr(bridge.motion, "solve_ik", solve)
    check = bridge.motion.check_segment

    def segment(*args):
        if failure == "collision" and calls == 3:
            raise RobotError("self_collision", "Intermediate collision")
        return check(*args)

    monkeypatch.setattr(bridge.motion, "check_segment", segment)
    result = bridge.execute(command())
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "cartesian_path_infeasible"
    assert result["error"]["details"]["sample"] == 3
    assert not bridge.arms["left"].commands
    assert not bridge.faults


def test_tracking_fault_during_cartesian_execution_retains_hold():
    bridge = make_bridge()
    sleep = bridge.sleep

    def slip(dt):
        sleep(dt)
        bridge.arms["left"].q[0] += 0.1

    bridge.sleep = slip
    result = bridge.execute(command())
    assert result["status"] == "stopped"
    assert result["error"]["code"] == "tracking_error"
    assert result["fault_latched"]
    assert not bridge.arms["left"].closed


@pytest.mark.parametrize(
    "kind,fields",
    [("joint_target", {"joints_rad": [0] * 6}), ("gripper_target", {"gripper_opening": 0.5})],
)
def test_cartesian_option_cannot_silently_apply_to_other_actions(kind, fields):
    bridge = make_bridge()
    result = bridge.execute(dict(arm="left", kind=kind, path="cartesian", **fields))
    assert result["status"] == "rejected"
    assert not bridge.arms["left"].commands


@pytest.mark.parametrize("final_only", [False, True])
def test_cartesian_guard_detects_lag_below_joint_threshold_and_can_recover(final_only):
    bridge = make_bridge()
    arm = bridge.arms["left"]
    original = arm.command
    count = 0

    def lag(q):
        nonlocal count
        original(q)
        count += 1
        if not final_only or count == 21:
            arm.q[2] -= 0.02  # 1.15 degrees, below legacy 3-degree joint guard.

    arm.command = lag
    result = bridge.execute(command())
    assert result["status"] == "stopped"
    assert result["error"]["code"] == "tracking_error"
    assert result["error"]["details"]["space"] == "cartesian"
    assert result["error"]["recoverable"]
    assert result["error"]["details"]["position_error_m"] > 0.001
    arm.command = original
    assert bridge.session("recover", arm="left")["status"] == "recovered"
    assert bridge.execute(command())["status"] == "completed"


@pytest.mark.parametrize("mode", ["delayed", "frozen", "stale", "stop"])
def test_endpoint_waits_for_fresh_feedback_and_remains_bounded(mode):
    clock = ManualClock()
    arm = FakeArm()
    arm.q = np.deg2rad([0, 15, 30, 0, 20, 0])
    pending = arm.q.copy()
    writes = 0
    bridge = Bridge(lambda: ({}, {}), clock=clock, sleep=lambda dt: None)
    bridge.arms["left"] = arm
    action = dict(
        arm="left", kind="ee_delta", path="cartesian", position_m=[0, 0, 0.02], duration_s=0.2
    )
    count = len(bridge.motion.trajectory(Action(**action), arm.q, arm.opening))

    def write(q):
        nonlocal pending, writes
        pending = q.copy()
        writes += 1
        arm.commands.append(q.copy())

    def tick(dt):
        clock.sleep(dt)
        final = writes == count
        if not final or mode != "frozen":
            arm.q = pending.copy()
        if final and mode == "stale":
            arm.feedback_age = clock.now - 0.2 + 0.001
        if final and mode == "stop":
            bridge.stops["left"].set()

    arm.command = write
    bridge.sleep = tick
    result = bridge.execute(action)
    if mode == "delayed":
        assert result["status"] == "completed"
        assert result["path"]["settling_s"] >= 0.019
    else:
        assert result["status"] == "stopped"
        expected = {
            "frozen": "tracking_error",
            "stale": "feedback_unavailable",
            "stop": "stop_requested",
        }
        assert result["error"]["code"] == expected[mode]
    assert clock.now <= 0.37
