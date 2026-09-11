"""Post-action evidence and failure preservation using simulated feedback only."""

import copy
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image
from scipy.spatial.transform import Rotation

from agentic_robots.bridge import Action, Motion
from agentic_robots.feedback import ActionFeedback
from agentic_robots.mcp import structured
from agentic_robots.recording import ORDER, RecordingBridge, Rollout
from tests.robot_fakes import FakeArm
from tests.test_robot_record import cameras, ready_fake


def observation():
    return {
        "images": {},
        "arms": {s: FakeArm().read() for s in ("left", "right")},
        "errors": {},
        "faults": {},
    }


def test_fk_uses_measured_pose_and_distinguishes_incomplete_jaw_command():
    obs = observation()
    obs["arms"]["left"]["joints_rad"] = [0.2, 0, 0, 0, 0, 0]
    obs["arms"]["left"]["gripper_opening"] = 0.316
    before = copy.deepcopy(obs)
    action = Action(arm="left", kind="gripper_target", gripper_opening=0.1)
    result = {"status": "stopped", "error": {"code": "tracking_error"}}
    enriched = ActionFeedback().enrich(obs, action, result)
    assert obs == before  # Do not mutate the underlying telemetry/result.
    arm = enriched["post_action"]["arms"]["left"]
    assert arm["gripper"]["interrupted"]
    assert arm["gripper"]["opening_error"] == pytest.approx(0.216)
    assert arm["gripper"]["requested_opening"] == 0.1
    pose = arm["ee_pose"]
    expected = Motion().fk([0.2, 0, 0, 0, 0, 0], 0.316)
    assert pose["position_m"] == pytest.approx(expected[:3, 3])
    assert np.allclose(Rotation.from_quat(pose["quaternion_xyzw"]).as_matrix(), expected[:3, :3])
    assert pose["frame"] == "left_base" and pose["site"] == "grasp_site"
    assert enriched["status"] == "stopped" and enriched["error"] == result["error"]
    assert any("does not finish" in d for d in enriched["diagnostics"])


@pytest.mark.parametrize("problem", ["stale", "unhealthy", "nan"])
def test_bad_feedback_never_becomes_a_claimed_current_ee_pose(problem):
    obs = observation()
    arm = obs["arms"]["left"]
    if problem == "stale":
        arm["feedback_age_s"] = 10
    elif problem == "unhealthy":
        arm["healthy"] = False
    else:
        arm["joints_rad"][0] = float("nan")
    result = ActionFeedback().enrich(
        obs, Action(arm="left", kind="joint_target", joints_rad=[1] * 6), {"status": "completed"}
    )
    assert result["status"] == "completed"
    assert result["post_action"]["arms"]["left"]["ee_pose"] is None
    assert result["post_action"]["arms"]["right"]["ee_pose"] is not None
    assert "pose:left" in result["post_action"]["errors"]


def test_snapshots_wait_for_post_action_publication_and_keep_immutable_files(tmp_path):
    r = Rollout(tmp_path / "run", "test", cameras(), "unused")
    ready_fake(r)
    after = time.time()

    def publish():
        time.sleep(0.08)
        for role in ORDER:
            p = r.output / f"{role}-next.png"
            Image.new("RGB", (20, 10), "blue").save(p)
            p.replace(r.output / f"{role}.png")

    with ThreadPoolExecutor() as pool:
        task = pool.submit(publish)
        images, errors = r.snapshots(after=after)
        task.result()
    assert not errors and set(images) == set(ORDER)
    for record in images.values():
        assert record["published_unix"] >= after
        assert Image.open(record["path"]).getpixel((0, 0)) == (0, 0, 255)
    ready_fake(r)
    assert Image.open(images["top"]["path"]).getpixel((0, 0)) == (0, 0, 255)


def test_stale_frames_are_not_returned_as_post_action_evidence(tmp_path):
    r = Rollout(tmp_path / "run", "test", cameras(), "unused")
    ready_fake(r)
    for role in ORDER:
        os.utime(r.output / f"{role}.png", (1, 1))
    r.process.poll.return_value = 1
    images, errors = r.snapshots(after=time.time())
    assert not images and set(errors) == {f"camera:{role}" for role in ORDER}


def test_observation_exception_preserves_motion_outcome_and_does_not_retry(tmp_path, monkeypatch):
    bridge = RecordingBridge(output_root=tmp_path)
    execute = Mock(return_value={"status": "completed", "actual": {"joints_rad": [0] * 6}})
    monkeypatch.setattr(bridge, "_execute", execute)
    monkeypatch.setattr(bridge, "observe", Mock(side_effect=OSError("camera missing")))
    result = bridge.execute(Action(arm="left", kind="joint_target", joints_rad=[0] * 6))
    execute.assert_called_once()
    assert result["status"] == "completed"
    assert result["actual"]["joints_rad"] == [0] * 6
    assert result["post_action"]["errors"]["observation"] == "camera missing"
    assert result["diagnostics"]


def test_camera_failure_still_returns_measured_pose(tmp_path, monkeypatch):
    r = Rollout(tmp_path / "run", "test", cameras(), "unused")
    ready_fake(r)
    bridge = RecordingBridge(r)
    monkeypatch.setattr(bridge, "_execute", lambda _: {"status": "completed"})
    monkeypatch.setattr(r, "snapshots", Mock(side_effect=OSError("disk full")))
    monkeypatch.setattr("agentic_robots.recording.upstream_call", lambda *_: observation())
    result = bridge.execute(Action(arm="left", kind="joint_target", joints_rad=[0] * 6))
    assert result["status"] == "completed"
    assert result["post_action"]["errors"]["cameras"] == "disk full"
    assert result["post_action"]["arms"]["left"]["ee_pose"] is not None


def test_finish_refuses_while_post_action_evidence_is_being_collected(tmp_path, monkeypatch):
    r = Rollout(tmp_path / "run", "test", cameras(), "unused")
    ready_fake(r)
    bridge = RecordingBridge(r)
    reading, release = threading.Event(), threading.Event()

    def observe(**_):
        reading.set()
        assert release.wait(3)
        return observation()

    monkeypatch.setattr(bridge, "_execute", lambda _: {"status": "completed"})
    monkeypatch.setattr(bridge, "observe", observe)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            bridge.execute, Action(arm="left", kind="joint_target", joints_rad=[0] * 6)
        )
        try:
            assert reading.wait(3)
            assert bridge.recording("finish")["error"]["code"] == "actions_in_flight"
        finally:
            release.set()
        assert future.result()["status"] == "completed"
    assert r.in_flight == 0


def test_native_mcp_images_and_missing_file_keep_original_execution_status(tmp_path):
    image = tmp_path / "image.png"
    Image.new("RGB", (10, 10), "green").save(image)
    value = {
        "status": "completed",
        "post_action": {"images": {"top": {"path": str(image)}}, "errors": {}},
        "diagnostics": ["Completed"],
    }
    result = structured(value)
    assert len([x for x in result.content if x.type == "image"]) == 1
    image.unlink()
    result = structured(value)
    assert not result.is_error
    assert result.structured_content["status"] == "completed"
    assert "camera:top" in result.structured_content["post_action"]["errors"]
