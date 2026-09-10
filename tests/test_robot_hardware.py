import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from scripts.robot_bridge import Bridge
from scripts.robot_hardware import Hardware, enable_once


def test_session_requires_support_and_explicit_release():
    b = Bridge()
    device = Mock()
    device.read.return_value = {"joints_rad": [0] * 6}
    factory = Mock(return_value=device)
    b.arm_factory = factory
    assert b.session("start", "left")["status"] == "rejected"
    factory.assert_not_called()
    b.session("start", "left", supported=True)
    assert b.session("start", "left", supported=True)["error"]["code"] == "already_connected"
    b.session("stop")
    device.close.assert_not_called()
    b.session("release", "left", supported=True)
    device.close.assert_called_once()
    assert b.arms == {}


@pytest.mark.parametrize("code", ["0x8", "0x9", "0xa", "0xb", "0xc", "0xe"])
def test_protection_faults_never_cleared(code):
    interface = Mock()
    interface.parse_recv_message.return_value = SimpleNamespace(
        error_code=code, error_message="protection", temperature_mos=30, temperature_rotor=30
    )
    with pytest.raises(RuntimeError):
        enable_once(interface, 1, "DM4340", True)
    interface.bus.send.assert_not_called()


def test_communication_reset_requires_explicit_startup_option():
    interface = Mock()
    bad = SimpleNamespace(
        error_code="0xd", error_message="timeout", temperature_mos=30, temperature_rotor=30
    )
    good = SimpleNamespace(error_code="0x1", error_message="normal")
    interface.parse_recv_message.return_value = bad
    with pytest.raises(RuntimeError):
        enable_once(interface, 1, "DM4340", False)
    interface.bus.send.assert_not_called()
    interface.parse_recv_message.side_effect = [bad, good]
    assert enable_once(interface, 1, "DM4340", True) is good
    assert interface.bus.send.call_count == 1


def make_hardware():
    from i2rt.robots.motor_chain_robot import JointCommands

    h = Hardware.__new__(Hardware)
    h.gripper_gains = (20.0, 0.5)
    h.robot = SimpleNamespace(
        motor_chain=SimpleNamespace(state_lock=threading.Lock(), state=[], running=True),
        _state_lock=threading.Lock(),
        _server_thread=SimpleNamespace(is_alive=lambda: True),
        _joint_state=SimpleNamespace(
            pos=np.zeros(7), vel=np.zeros(7), temp_mos=np.ones(7) * 25, temp_rotor=np.ones(7) * 25
        ),
        _command_lock=threading.Lock(),
        _commands=JointCommands.init_all_zero(7),
        _kp=np.ones(7),
        _kd=np.ones(7),
        remapper=SimpleNamespace(to_robot_joint_pos_space=lambda q: q),
    )
    h.read_lock = threading.Lock()
    h.motor_state = h.joint_state = None
    h.motor_time = h.joint_time = 0
    return h


def test_cached_feedback_age_and_temporary_lock_contention(monkeypatch):
    h = make_hardware()
    monkeypatch.setattr("scripts.robot_hardware.time.monotonic", lambda: 1)
    assert h.read()["feedback_age_s"] == 0
    h.robot._state_lock.acquire()
    monkeypatch.setattr("scripts.robot_hardware.time.monotonic", lambda: 1.02)
    assert h.read()["feedback_age_s"] == pytest.approx(0.02)
    monkeypatch.setattr("scripts.robot_hardware.time.monotonic", lambda: 1.2)
    assert h.read()["feedback_age_s"] == pytest.approx(0.2)


def test_arm_command_keeps_arm_gains_and_releases_gripper_effort():
    h = make_hardware()
    h.command(np.ones(6) * 0.01)
    np.testing.assert_array_equal(h.robot._commands.kp, [1] * 6 + [0])
    np.testing.assert_array_equal(h.robot._commands.kd, [1] * 6 + [0])
    np.testing.assert_array_equal(h.robot._commands.torques, np.zeros(7))


def test_gripper_command_preserves_arm_hold_and_arm_command_preserves_jaws():
    from i2rt.robots.utils import JointMapper

    h = make_hardware()
    h.robot.remapper = JointMapper({6: (0.23, -5.06)}, 7)
    q = np.arange(6) * 0.1
    h.command(q)
    original = h.robot._commands
    h.command_gripper(0.25)
    command = h.robot._commands
    assert command is not original
    np.testing.assert_array_equal(command.pos[:6], q)
    np.testing.assert_array_equal(command.kp[:6], original.kp[:6])
    np.testing.assert_array_equal(command.kd[:6], original.kd[:6])
    assert command.pos[6] == pytest.approx(0.23 + 0.25 * (-5.06 - 0.23))
    h.command(q + 0.01)
    assert h.robot._commands.pos[6] == command.pos[6]
    assert h.robot._commands.kp[6] == 20
    assert h.robot._commands.kd[6] == 0.5


@pytest.mark.parametrize("opening", [-0.01, 1.01, float("nan"), float("inf")])
def test_invalid_gripper_value_does_not_replace_motor_command(opening):
    h = make_hardware()
    original = h.robot._commands
    with pytest.raises(ValueError):
        h.command_gripper(opening)
    assert h.robot._commands is original


def test_startup_has_no_software_temperature_cutoff():
    interface = Mock()
    state = SimpleNamespace(error_code="0x1", temperature_mos=65, temperature_rotor=65)
    interface.parse_recv_message.return_value = state
    assert enable_once(interface, 1, "DM4340", False) is state
    interface.bus.send.assert_not_called()


def test_startup_errors_return_feedback_and_server_can_retry():
    device = Mock()
    device.read.return_value = {"joints_rad": [0] * 6}
    factory = Mock(side_effect=[RuntimeError("CAN adapter missing"), device])
    b = Bridge(arm_factory=factory)
    result = b.session("start", "left", supported=True)
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "session_error"
    assert "CAN adapter missing" in result["reason"]
    assert b.session("status")["arms"] == {}
    assert "left" in b.session("start", "left", supported=True)["arms"]


def test_release_failure_keeps_session_available_for_inspection():
    device = Mock()
    device.close.side_effect = RuntimeError("release failed")
    device.read.return_value = {"joints_rad": [0] * 6}
    b = Bridge()
    b.arms["left"] = device
    result = b.session("release", "left", supported=True)
    assert result["error"]["details"]["operation"] == "release"
    assert b.arms["left"] is device
    assert "left" in b.session("status")["arms"]


def test_concurrent_feedback_reader_returns_aged_cache(monkeypatch):
    h = make_hardware()
    monkeypatch.setattr("scripts.robot_hardware.time.monotonic", lambda: 1)
    h.read()
    h.read_lock.acquire()
    try:
        monkeypatch.setattr("scripts.robot_hardware.time.monotonic", lambda: 1.2)
        assert h.read()["feedback_age_s"] == pytest.approx(0.2)
    finally:
        h.read_lock.release()


def test_hardware_identity_ownership_and_gripper_settings(tmp_path, monkeypatch):
    import json
    from unittest.mock import MagicMock

    import i2rt.robots.get_robot

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROBOT_ID", "test")
    monkeypatch.setenv("LEFT_CAN", "test-adapter")
    serial = f"pytest-{tmp_path.name}"
    row = {"name": "fake-can", "serial": serial}
    monkeypatch.setattr("scripts.robot_hardware.inventory", lambda: [])
    monkeypatch.setattr("scripts.robot_hardware.resolve", lambda *args: row)
    monkeypatch.setattr("scripts.robot_hardware.ready", lambda row: True)
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    file = calibration / "test.json"
    file.write_text(json.dumps({"left": {"adapter_serial": "wrong", "gripper_limits": [0, -5]}}))
    bus = MagicMock()
    bus.return_value.__enter__.return_value.recv.return_value = None
    monkeypatch.setattr("scripts.robot_hardware.can.Bus", bus)
    factory = Mock(return_value=Mock())
    monkeypatch.setattr(i2rt.robots.get_robot, "get_yam_robot", factory)
    with pytest.raises(ValueError, match="mismatch"):
        Hardware("left")
    bus.assert_not_called()
    file.write_text(json.dumps({"left": {"adapter_serial": serial, "gripper_limits": [0, -5]}}))
    bus.return_value.__enter__.return_value.recv.return_value = object()
    with pytest.raises(RuntimeError, match="already active"):
        Hardware("left")
    factory.assert_not_called()
    bus.return_value.__enter__.return_value.recv.return_value = None
    hardware = Hardware("left")
    try:
        assert factory.call_args.kwargs["enable_auto_recovery"] is False
        assert factory.call_args.kwargs["gripper_kp"] == 0
        assert factory.call_args.kwargs["gripper_kd"] == 0
        assert factory.call_args.kwargs["zero_gravity_mode"] is False
        assert hardware.robot._gripper_force_limiter._kp == 20
        with pytest.raises(BlockingIOError):
            Hardware("left")
    finally:
        hardware.lock.close()  # Mock hardware; no physical motor was opened.
