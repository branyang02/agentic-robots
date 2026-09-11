from unittest.mock import Mock

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from agentic_robots.bridge import IK_POSITION_TOLERANCE, IK_ROTATION_TOLERANCE, Action, Motion


@pytest.fixture
def motion():
    return Motion()


def action(kind, **kwargs):
    return Action(arm="left", kind=kind, **kwargs)


@pytest.mark.parametrize("kind", ["joint_target", "joint_delta"])
def test_joint_targets_and_deltas_use_current_state(motion, kind):
    start = np.deg2rad([5, 1, 1, 0, 0, 0])
    a = action(kind, joints_rad=[0.02, 0.02, 0.02, 0, 0, 0])
    actual_start, target = motion.plan(a, start)
    np.testing.assert_allclose(actual_start, start)
    np.testing.assert_allclose(
        target, np.array(a.joints_rad) + (start if kind == "joint_delta" else 0)
    )


@pytest.mark.parametrize("duration", [0.001, 5, 120])
def test_no_action_size_or_duration_caps(motion, duration):
    a = action(
        "joint_target", joints_rad=np.deg2rad([100, 0, 0, 0, 0, 0]).tolist(), duration_s=duration
    )
    _, target = motion.plan(a, np.zeros(6))
    np.testing.assert_allclose(target, a.joints_rad)
    assert a.duration_s == duration


def assert_pose(actual, expected):
    assert np.linalg.norm(actual[:3, 3] - expected[:3, 3]) <= IK_POSITION_TOLERANCE
    assert (
        Rotation.from_matrix(expected[:3, :3].T @ actual[:3, :3]).magnitude()
        <= IK_ROTATION_TOLERANCE
    )


def test_absolute_cartesian_target(motion):
    start = np.deg2rad([0, 15, 30, 0, 20, 0])
    goal = motion.fk(start + [0.05, 0, 0, 0, 0, 0])
    a = action(
        "ee_target",
        position_m=goal[:3, 3].tolist(),
        quaternion_xyzw=Rotation.from_matrix(goal[:3, :3]).as_quat().tolist(),
    )
    _, target = motion.plan(a, start)
    assert_pose(motion.fk(target), goal)


@pytest.mark.parametrize("frame", ["base", "tool"])
@pytest.mark.parametrize("component", ["translation", "rotation", "both"])
def test_cartesian_delta_frames(motion, frame, component):
    start = np.deg2rad([0, 15, 30, 0, 20, 0])
    before = motion.fk(start)
    translation = [0, 0, 0.002] if component != "rotation" else None
    rotation_vector = [0, 0.01, 0] if component != "translation" else None
    a = action("ee_delta", frame=frame, position_m=translation, rotation_vector_rad=rotation_vector)
    _, target = motion.plan(a, start)
    rotation = Rotation.from_rotvec(rotation_vector or [0, 0, 0]).as_matrix()
    expected = before.copy()
    expected[:3, 3] += (
        before[:3, :3] @ (translation or [0, 0, 0])
        if frame == "tool"
        else (translation or [0, 0, 0])
    )
    expected[:3, :3] = before[:3, :3] @ rotation if frame == "tool" else rotation @ before[:3, :3]
    assert_pose(motion.fk(target), expected)


def test_solver_and_endpoint_use_same_tolerance(motion, monkeypatch):
    start = np.deg2rad([0, 15, 30, 0, 20, 0])
    goal = motion.fk(start)
    goal[0, 3] += 0.0005  # Beyond the old 0.1 mm cutoff, within the new tolerance.
    ik = Mock(return_value=(True, motion.model_q(start, 0)))
    monkeypatch.setattr(motion.kin, "ik", ik)
    a = action(
        "ee_target",
        position_m=goal[:3, 3].tolist(),
        quaternion_xyzw=Rotation.from_matrix(goal[:3, :3]).as_quat().tolist(),
    )
    motion.plan(a, start)
    assert ik.call_args.kwargs["pos_threshold"] == IK_POSITION_TOLERANCE
    assert ik.call_args.kwargs["ori_threshold"] == IK_ROTATION_TOLERANCE
    a.position_m[0] += 0.01
    with pytest.raises(ValueError, match="IK"):
        motion.plan(a, start)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "joint_target", "joints_rad": [float("nan")] * 6},
        {"kind": "joint_target", "joints_rad": [0] * 5},
        {"kind": "joint_target", "joints_rad": [0, -0.1, 0, 0, 0, 0]},
        {"kind": "joint_target", "joints_rad": [4, 0, 0, 0, 0, 0]},
        {"kind": "ee_target", "position_m": [10] * 3, "quaternion_xyzw": [0, 0, 0, 1]},
        {"kind": "ee_target", "position_m": [0] * 3, "quaternion_xyzw": [0] * 4},
        {"kind": "ee_delta", "frame": "world", "position_m": [0] * 3},
        {"kind": "ee_delta"},
        {"kind": "joint_delta", "joints_rad": [0] * 6, "position_m": [0] * 3},
    ],
)
def test_rejects_invalid_or_unreachable_commands(motion, kwargs):
    with pytest.raises(ValueError):
        motion.plan(action(**kwargs), np.zeros(6))


def test_model_self_collision_blocks_path(motion):
    a = action("joint_target", joints_rad=np.deg2rad([0, 200, 0, 0, 0, 0]).tolist())
    with pytest.raises(ValueError, match="self-collision"):
        motion.plan(a, np.zeros(6))


def test_encoder_quantization_at_joint_boundary(motion):
    a = action("joint_target", joints_rad=[0] * 6)
    motion.plan(a, np.array([0, -0.0002, -0.0002, 0, 0, 0]))
    with pytest.raises(ValueError, match="starting pose"):
        motion.plan(a, np.array([0, -0.01, 0, 0, 0, 0]))
