"""Opt-in desktop/LLM E2E. Uses only simulated arms and synthetic camera streams.

ROBOT_CODEX_E2E_THREAD_ID must name an idle, disposable local Codex conversation.
This sends visible prompts and consumes the signed-in Codex account's usage.
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from mcp import Client

from agentic_robots.codex import app_call, locate_app
from tests.test_robot_http import http_robot  # noqa: F401
from tests.test_robot_record import frame
from tests.test_robot_record_service import http_recorder  # noqa: F401

pytestmark = pytest.mark.e2e


THREAD = os.environ.get("ROBOT_CODEX_E2E_THREAD_ID")


def assert_task_trace(log, number):
    requests = {
        e["request_id"]: e["arguments"]["action"]
        for e in log
        if e["kind"] == "request" and e["tool"] == "execute"
    }
    completed = [
        (requests[e["request_id"]]["arm"], e["result"]["actual"])
        for e in log
        if e["kind"] == "response"
        and e.get("tool") == "execute"
        and e["result"].get("status") == "completed"
    ]
    assert any(e["kind"] == "observation" for e in log)
    assert not any(e.get("arguments", {}).get("operation") == "release" for e in log)
    for arm in ("left", "right"):
        assert any(
            side == arm
            and (
                any(abs(x) > 0.01 for x in actual["joints_rad"])
                if number == 1
                else actual["gripper_opening"] == 1
            )
            for side, actual in completed
        ), "Require observed effects, not just requests"
    observed_at, final = next(
        (i, e["observation"])
        for i, e in reversed(list(enumerate(log)))
        if e["kind"] == "observation"
    )
    last_action = max(
        i for i, e in enumerate(log) if e["kind"] == "response" and e.get("tool") == "execute"
    )
    finished_at = next(i for i, e in enumerate(log) if e["kind"] == "recording_finished")
    assert last_action < observed_at < finished_at, "Verify neutral after motion, before finishing"
    if number == 1:
        fault_at, fault = next(
            (i, e)
            for i, e in enumerate(log)
            if e.get("result", {}).get("error", {}).get("code") == "tracking_error"
        )
        assert fault["result"]["error"]["recoverable"]
        recovery_at = next(
            i for i, e in enumerate(log) if e.get("result", {}).get("status") == "recovered"
        )
        correction_at, correction = next(
            (i, e["arguments"]["action"])
            for i, e in enumerate(log)
            if i > recovery_at
            and e["kind"] == "request"
            and e.get("tool") == "execute"
            and e["arguments"]["action"]["arm"] == "left"
        )
        assert fault_at < recovery_at < correction_at < observed_at
        assert any(e["kind"] == "observation" for e in log[fault_at:recovery_at])
        assert any(e["kind"] == "observation" for e in log[recovery_at:correction_at])
        assert correction != requests[fault["request_id"]], "Choose a corrected action"
    assert set(final["arms"]) == {"left", "right"}
    assert not final["faults"]
    assert all(
        state["joints_rad"] == pytest.approx([0] * 6, abs=1e-6)
        and state["velocity_rad_s"] == pytest.approx([0] * 6, abs=1e-6)
        and state["healthy"]
        for state in final["arms"].values()
    ), "The agent must observe both arms stationary at neutral before declaring completion"


@pytest.mark.skipif(not THREAD, reason="Opt in with an idle local Codex test conversation ID")
@pytest.mark.parametrize("http_robot", ["TrackingSlipArm"], indirect=True)
def test_desktop_initialization_then_two_agent_tasks(http_recorder, tmp_path, monkeypatch):  # noqa: F811
    call, url, output, recorder, controller, root = http_recorder
    # Exercise the entire flow from an ordinary terminal, including follow-up task delivery.
    monkeypatch.delenv("CODEX_APP_TOOLS_PIPE_PATH", raising=False)
    initialized = subprocess.run(
        [
            "uv",
            "run",
            "robot-init",
            "--thread-id",
            THREAD,
            "--url",
            url,
            "--startup-supported",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=240,
    )
    (tmp_path / "initializer.stdout").write_text(initialized.stdout)
    (tmp_path / "initializer.stderr").write_text(initialized.stderr)
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout)["status"] == "acknowledged"
    assert call("session", {"operation": "status"})["arms"] == {}
    assert not output.exists(), "Initialization must not start capture or move hardware"

    async def task(prompt, number):
        async with Client(await locate_app(), read_timeout_seconds=60) as app:
            previous = await app_call(
                app,
                THREAD,
                "read_thread",
                {"threadId": THREAD, "turnLimit": 1, "includeOutputs": False},
            )
            previous_id = previous["turns"][0]["id"]
            await app_call(
                app, THREAD, "send_message_to_thread", {"threadId": THREAD, "prompt": prompt}
            )
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                # This external client acts in the target's context; the desktop's
                # wait_threads tool forbids waiting on its own calling conversation.
                page = await app_call(
                    app,
                    THREAD,
                    "read_thread",
                    {"threadId": THREAD, "turnLimit": 1, "includeOutputs": False},
                )
                for turn in page.get("turns", []):
                    if turn.get("id") == previous_id:
                        continue
                    if (
                        turn.get("status") == "completed"
                        and call("recording", {"operation": "status"}).get("task", {}).get("phase")
                        == "success"
                    ):
                        (tmp_path / f"agent-task-{number}.json").write_text(
                            json.dumps(page, indent=2)
                        )
                        return
                await asyncio.sleep(3)
            pytest.fail("Codex did not finish the simulated task; inspect the test conversation")

    # Neither task asks for a neutral return: the initialization must supply it.
    prompts = [
        "Close both grippers and move both arms a little away from their starting positions. "
        "Verify the result from the available observations.",
        "Open both grippers, then close them again. Verify the result.",
    ]
    for number, prompt in enumerate(prompts, 1):
        asyncio.run(task(prompt, number))
        status = call("recording", {"operation": "status"})
        assert status["status"] == "finished", status
        assert status["task"]["phase"] == "success", status
        assert status["task"]["review"]["evidence"]
        directory = Path(status["output"])
        assert prompt in (directory / "prompt.txt").read_text()
        assert frame(directory / "rollout.mp4").size == (1920, 516)
        state = call("session", {"operation": "status"})
        assert set(state["arms"]) == {"left", "right"}
        assert all(
            s["joints_rad"] == pytest.approx([0] * 6, abs=1e-6) and s["gripper_opening"] == 0
            for s in state["arms"].values()
        )
        assert controller.poll() is None and recorder.poll() is None
        log = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        assert_task_trace(log, number)
    assert len(list(output.iterdir())) == 2
