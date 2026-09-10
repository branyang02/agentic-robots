"""Persistent recorder over real HTTP, FFmpeg, and simulated CAN-free hardware."""

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from tests.test_robot_http import http_robot  # noqa: F401
from tests.test_robot_record import cameras, frame


@pytest.fixture
def http_recorder(http_robot, tmp_path):  # noqa: F811
    _, command, controller, root = http_robot
    cmd, _ = command("session", {"operation": "status"})
    upstream = cmd[cmd.index("--url") + 1]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    output = tmp_path / "tasks"
    program = tmp_path / "recorder_service.py"
    program.write_text(
        "import socket\noriginal=socket.socket\n"
        "class NoCAN(original):\n"
        " def __init__(self,family=socket.AF_INET,*a,**kw):\n"
        "  if family==socket.AF_CAN: raise RuntimeError('CAN forbidden in test')\n"
        "  super().__init__(family,*a,**kw)\n"
        "socket.socket=NoCAN\n"
        "from scripts import robot_record\n"
        f"robot_record.configured_cameras=lambda:{cameras()!r}\n"
        "robot_record.main()\n"
    )
    log = (tmp_path / "recorder-service.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            str(program),
            "--output-root",
            str(output),
            "--upstream",
            upstream,
            "--port",
            str(port),
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        stdout=log,
        stderr=log,
    )
    url = f"http://127.0.0.1:{port}/mcp"

    def call(tool, arguments=None, error=False):
        cmd, result_path = command(tool, arguments)
        cmd[cmd.index("--url") + 1] = url
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=30)
        assert result.returncode == (1 if error else 0), result.stderr + result.stdout
        return json.loads(result_path.read_text())

    try:
        for _ in range(150):
            assert process.poll() is None, (tmp_path / "recorder-service.log").read_text()
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("Recorder service did not start")
        yield call, url, output, process, controller, root
    finally:
        if process.poll() is None:
            call("recording", {"operation": "finish"})
            process.terminate()  # Only this test's recorder; never the live controller.
            process.wait(timeout=10)
        log.close()


def test_two_tasks_reuse_http_service_and_preserve_hold(http_recorder):
    call, _, output, recorder, controller, _ = http_recorder
    assert call("recording", {"operation": "status"})["status"] == "idle"
    assert not output.exists()
    assert call("observe")["images"] == {}
    assert call("session", {"operation": "status"})["arms"] == {}
    directories = []
    for task in ("Close, move both arms, then neutral", "Repeat without restarting"):
        start = call("recording", {"operation": "start", "text": task})
        assert start["ready"]
        directories.append(start["output"])
        for arm in ("left", "right"):
            if not call("session", {"operation": "status"})["arms"].get(arm):
                call("session", {"operation": "start", "arm": arm, "supported": True})
            for target in ([0.15, 0, 0, 0, 0, 0], [0] * 6):
                result = call(
                    "execute",
                    {
                        "action": {
                            "arm": arm,
                            "kind": "joint_target",
                            "joints_rad": target,
                            "duration_s": 0.1,
                        }
                    },
                )
                assert result["status"] == "completed"
        assert len(call("observe")["images"]) == 3
        assert call("recording", {"operation": "finish"})["status"] == "finished"
        result = call(
            "execute",
            {"action": {"arm": "left", "kind": "joint_target", "joints_rad": [0] * 6}},
            error=True,
        )
        assert result["error"]["code"] == "recording_unavailable"
        assert recorder.poll() is None and controller.poll() is None
        assert all(
            s["joints_rad"] == [0] * 6
            for s in call("session", {"operation": "status"})["arms"].values()
        )
    assert len(set(directories)) == 2
    for directory in output.iterdir():
        assert frame(directory / "rollout.mp4").size == (1920, 516)
        assert (directory / "prompt.txt").read_text().strip()
        events = [
            json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()
        ]
        assert (
            len(
                [
                    e
                    for e in events
                    if e["kind"] == "request"
                    and e["tool"] == "execute"
                    and e.get("forwarded", True)
                ]
            )
            == 4
        )
