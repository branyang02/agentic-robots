from unittest.mock import Mock

import pytest
from PIL import Image

from agentic_robots.bridge import Bridge, camera_snapshot


@pytest.fixture
def images(tmp_path):
    paths = {}
    for name in ("left", "right", "top"):
        path = tmp_path / f"{name}.png"
        Image.new("RGB", (20, 10), "white").save(path)
        paths[name] = str(path)
    return paths


def test_observe_does_not_enable_arms(images):
    factory = Mock()
    obs = Bridge(lambda: (images, {}), arm_factory=factory).observe()
    assert obs["arms"] == {}
    assert set(obs["images"]) == {"left", "right", "top"}
    assert obs["images"]["top"]["width"] == 20
    assert obs["capture_started_unix"] <= obs["capture_finished_unix"]
    factory.assert_not_called()


def test_available_images_and_feedback_survive_independent_errors(images):
    images["top"] = "/missing.png"
    b = Bridge(lambda: (images, {"camera:right": "camera offline"}))
    del images["right"]
    left, right = Mock(), Mock()
    left.read.return_value = {"joints_rad": [0] * 6}
    right.read.side_effect = RuntimeError("feedback offline")
    b.arms.update(left=left, right=right)
    obs = b.observe()
    assert set(obs["images"]) == {"left"}
    assert obs["arms"]["left"]["joints_rad"] == [0] * 6
    assert set(obs["errors"]) == {"camera:right", "camera:top", "arm:right"}


def test_dark_and_zero_camera_observations_are_returned(images):
    Image.new("RGB", (20, 10), 0).save(images["top"])
    assert "top" in Bridge(lambda: (images, {})).observe()["images"]
    obs = Bridge(lambda: ({}, {"camera:top": "unavailable"})).observe()
    assert obs["images"] == {}
    assert obs["errors"]["camera:top"] == "unavailable"


def test_capture_exception_still_returns_joint_state():
    b = Bridge(Mock(side_effect=RuntimeError("capture failed")))
    arm = Mock()
    arm.read.return_value = {"joints_rad": [0] * 6}
    b.arms["left"] = arm
    obs = b.observe()
    assert obs["errors"]["cameras"] == "capture failed"
    assert "left" in obs["arms"]


def test_camera_snapshot_tolerates_missing_configuration_and_capture_failure(monkeypatch):
    for side in ("LEFT", "RIGHT", "TOP"):
        monkeypatch.delenv(f"{side}_CAMERA", raising=False)
    monkeypatch.setenv("LEFT_CAMERA", "/left")
    monkeypatch.setenv("RIGHT_CAMERA", "/right")

    def capture(name, camera, seconds):
        if camera["device"] == "/right":
            raise RuntimeError("device busy")
        return {"preview": "/available.png"}

    monkeypatch.setattr("agentic_robots.bridge.capture", capture)
    paths, errors = camera_snapshot()
    assert paths == {"left": "/available.png"}
    assert errors["camera:right"] == "device busy"
    assert "camera:top" in errors
