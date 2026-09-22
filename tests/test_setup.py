import json

import pytest

from agentic_robots.calibration import save_limits
from agentic_robots.can import ready, resolve


def test_serial_resolution_survives_interface_renumbering():
    rows = [{"name": "can7", "serial": "ABC"}, {"name": "can2", "serial": "DEF"}]
    assert resolve("abc", rows)["name"] == "can7"
    with pytest.raises(ValueError):
        resolve("missing", rows)
    with pytest.raises(ValueError):
        resolve("ABC", rows + [rows[0]])


@pytest.mark.parametrize("state", ["STOPPED", "ERROR-PASSIVE", "BUS-OFF"])
def test_unhealthy_can_is_rejected(state):
    row = dict(up=True, state="ERROR-ACTIVE", bitrate=1_000_000, mtu=16)
    assert ready(row)
    assert not ready(dict(row, state=state))
    assert not ready(dict(row, mtu=72))
    assert not ready(dict(row, bitrate=500_000))


def test_calibration_preserves_other_side_and_rejects_bad_limits(tmp_path):
    path = tmp_path / "dual-yam.json"
    save_limits(path, "left", "ABC", [6.5, 1.2])
    save_limits(path, "right", "DEF", [6.4, 1.1])
    before = path.read_text()
    assert set(json.loads(before)) == {"left", "right"}
    assert list(tmp_path.glob("*.bak"))
    for bad in ([1, 1], [float("nan"), 1], [1]):
        with pytest.raises(ValueError):
            save_limits(path, "left", "ABC", bad)
        assert path.read_text() == before


def test_i2rt_model_loads_without_hardware():
    from i2rt.robots.get_robot import get_yam_robot
    from i2rt.robots.utils import ArmType, GripperType

    robot = get_yam_robot(arm_type=ArmType.YAM, gripper_type=GripperType.LINEAR_4310, sim=True)
    assert robot.num_dofs() == 7
    assert len(robot.get_joint_pos()) == 7


def test_shutdown_waits_for_inflight_can_io():
    import threading
    from types import SimpleNamespace

    from agentic_robots.hardware import close_robot

    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    closed = threading.Event()
    stopping = threading.Event()
    failures = []

    class Chain:
        running = True

        def control(self):
            entered.set()
            assert release.wait(3)
            assert not closed.is_set(), "CAN socket closed during an in-flight operation"
            exited.set()

    chain = Chain()
    motor = threading.Thread(target=chain.control)
    server = threading.Thread(target=stopping.wait)

    def finish():
        assert not server.is_alive()
        assert exited.is_set()
        assert not motor.is_alive()
        closed.set()

    robot = SimpleNamespace(
        motor_chain=chain, _stop_event=stopping, _server_thread=server, close=finish
    )

    def cleanup():
        try:
            close_robot(robot)
        except BaseException as exc:
            failures.append(exc)

    motor.start()
    server.start()
    assert entered.wait(1)
    closer = threading.Thread(target=cleanup)
    closer.start()
    try:
        assert stopping.wait(1)
        assert not closed.is_set()
    finally:
        release.set()
        closer.join(3)
        motor.join(3)
        server.join(3)
    assert not failures
    assert closed.is_set()
