"""Real HTTP/MCP/CLI with simulated arms; the subprocess cannot open CAN sockets."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

pytestmark = pytest.mark.e2e


@pytest.fixture
def http_robot(tmp_path, request):
    root = Path(__file__).resolve().parents[1]
    image = tmp_path / "top.png"
    arm_type = getattr(request, "param", "FakeArm")
    assert arm_type in {"FakeArm", "TrackingSlipArm", "ReturnSlipArm"}
    Image.new("RGB", (20, 20), "white").save(image)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    program = tmp_path / "server.py"
    program.write_text(
        "import socket\noriginal=socket.socket\n"
        "class NoCAN(original):\n"
        " def __init__(self, family=socket.AF_INET,*a,**kw):\n"
        "  if family==socket.AF_CAN: raise RuntimeError('CAN forbidden in test')\n"
        "  super().__init__(family,*a,**kw)\n"
        "socket.socket=NoCAN\n"
        "from agentic_robots.bridge import Bridge\n"
        "from scripts.robot_bridge import make_server\n"
        f"from tests.robot_fakes import {arm_type}\n"
        f"bridge=Bridge(lambda:({{'top':{str(image)!r}}},{{'camera:left':'offline'}}),arm_factory={arm_type})\n"
        f"make_server(bridge).run(transport='streamable-http',host='127.0.0.1',port={port})\n"
    )
    log = (tmp_path / "server.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, str(program)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        stdout=log,
        stderr=log,
    )
    number = 0

    def command(tool, arguments=None):
        nonlocal number
        number += 1
        file = tmp_path / f"request-{number}.json"
        file.write_text(json.dumps(arguments or {}))
        output = tmp_path / f"response-{number}.json"
        cmd = [
            "uv",
            "run",
            "robot-call",
            tool,
            "--url",
            f"http://127.0.0.1:{port}/mcp",
            "--arguments",
            str(file),
            "--output",
            str(output),
        ]
        return cmd, output

    def call(tool, arguments=None, error=False):
        cmd, output = command(tool, arguments)
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=15)
        assert result.returncode == (1 if error else 0), result.stderr + result.stdout
        return json.loads(output.read_text())

    try:
        for _ in range(100):
            assert proc.poll() is None, (tmp_path / "server.log").read_text()
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("Test server did not start")
        yield call, command, proc, root
    finally:
        proc.terminate()  # Only simulated arms; this server is forbidden from using CAN.
        proc.wait(timeout=5)
        log.close()


def test_http_end_to_end_rejection_correction_both_arms_and_return(http_robot):
    call, _, proc, _ = http_robot
    initial = call("observe")
    assert initial["arms"] == {}
    assert set(initial["images"]) == {"top"}
    for side in ("left", "right"):
        call("session", {"operation": "start", "arm": side, "supported": True})
        result = call(
            "execute",
            {
                "action": {
                    "arm": side,
                    "kind": "gripper_target",
                    "gripper_opening": 0,
                    "duration_s": 0.02,
                }
            },
        )
        assert result["gripper_error"] == 0
    bad = call(
        "execute",
        {"action": {"arm": "left", "kind": "joint_target", "joints_rad": [4, 0, 0, 0, 0, 0]}},
        error=True,
    )
    assert bad["error"]["code"] == "joint_limits"
    assert bad["error"]["retryable"]
    # Use the returned numerical limits to construct a corrected request.
    correction = bad["error"]["details"]["joint_limits_rad"][0][1] / 2
    for side in ("left", "right"):
        result = call(
            "execute",
            {
                "action": {
                    "arm": side,
                    "kind": "joint_target",
                    "joints_rad": [correction, 0, 0, 0, 0, 0],
                    "duration_s": 0.02,
                }
            },
        )
        assert result["status"] == "completed"
    middle = call("observe")
    assert all(
        state["joints_rad"][0] == pytest.approx(correction) for state in middle["arms"].values()
    )
    for side in ("left", "right"):
        result = call(
            "execute",
            {
                "action": {
                    "arm": side,
                    "kind": "joint_delta",
                    "joints_rad": [-correction, 0, 0, 0, 0, 0],
                    "duration_s": 0.02,
                }
            },
        )
        assert result["status"] == "completed"
    final = call("observe")
    assert all(state["joints_rad"] == [0] * 6 for state in final["arms"].values())
    assert all(state["gripper_opening"] == 0 for state in final["arms"].values())
    assert proc.poll() is None
    for side in ("left", "right"):
        call("session", {"operation": "release", "arm": side, "supported": True})
    assert call("session", {"operation": "status"})["arms"] == {}


def test_http_client_disconnect_retains_session_and_explicit_stop_interrupts(http_robot):
    call, command, server, root = http_robot
    call("session", {"operation": "start", "arm": "left", "supported": True})
    cmd, _ = command(
        "execute",
        {
            "action": {
                "arm": "left",
                "kind": "joint_target",
                "joints_rad": [0.3, 0, 0, 0, 0, 0],
                "duration_s": 10,
            }
        },
    )
    client = subprocess.Popen(cmd, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(5):
            state = call("session", {"operation": "status"})["arms"]["left"]
            if state["command_count"] > 0:
                break
        assert state["command_count"] > 0
        client.terminate()  # Disconnect the MCP client, not the independently running bridge.
        client.wait(timeout=5)
        assert server.poll() is None
        assert "left" in call("observe")["arms"]
        call("session", {"operation": "stop", "arm": "left"})
        state = call("session", {"operation": "status"})
        assert not state["faults"]
        assert "left" in state["arms"]
        assert 0 < state["arms"]["left"]["joints_rad"][0] < 0.3
        result = call(
            "execute",
            {
                "action": {
                    "arm": "left",
                    "kind": "joint_target",
                    "joints_rad": [0] * 6,
                    "duration_s": 0.02,
                }
            },
        )
        assert result["status"] == "completed"
        assert call("observe")["arms"]["left"]["joints_rad"] == [0] * 6
        call("session", {"operation": "release", "arm": "left", "supported": True})
    finally:
        if client.poll() is None:
            client.terminate()
            client.wait(timeout=5)
