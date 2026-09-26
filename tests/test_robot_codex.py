"""Launcher lifecycle with mocked CLI; opt-in real CLI/model uses only simulated arms."""

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

import httpx2 as httpx
import pytest

from scripts import robot_codex
from tests.test_robot_http import http_robot  # noqa: F401
from tests.test_robot_record_service import http_recorder  # noqa: F401


@pytest.mark.parametrize("failure", [None, "active", "acknowledgment", "task"])
def test_launcher_initializes_then_records_and_never_owns_motors(tmp_path, monkeypatch, failure):
    task = tmp_path / "task.txt"
    task.write_text("Test this task")
    args = robot_codex.Args(
        task_file=task, workspace=tmp_path / "work", output=tmp_path / "evidence"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "upstream-secret")
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/codex")
    calls, stages = [], []

    async def call(url, tool, arguments):
        calls.append((tool, arguments))
        assert tool in {"recording", "observe"}, "Launcher never starts/moves/releases motors"
        if tool == "observe":
            return {"images": {}, "arms": {}, "errors": {"test": "no hardware"}}
        if arguments["operation"] == "start":
            return {"status": "recording", "ready": True, "output": "/fake/recording"}
        return {"status": "recording" if failure == "active" else "idle"}

    async def turn(command, prompt, output, stage, environment):
        stages.append(stage)
        assert "OPENAI_API_KEY" not in environment and "CODEX_API_KEY" not in environment
        if stage == "init":
            assert calls == [("recording", {"operation": "status"})]
            token = prompt.split("replying exactly:\n")[1].splitlines()[0]
            (output / "init-final.txt").write_text(
                "wrong" if failure == "acknowledgment" else token
            )
            return [{"type": "thread.started", "thread_id": "thread_test"}]
        assert "resume" in command and "thread_test" in command
        assert "Reuse that recording" in prompt
        if failure == "task":
            raise RuntimeError("Model stream failed")
        url = re.search(r'base_url="([^"]+)"', " ".join(command))[1]
        # Invalid JSON is rejected locally, never sent to a real model in this test.
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url + "/responses",
                content=b"{",
                headers={"authorization": "Bearer " + environment["ROBOT_MODEL_TOKEN"]},
            )
            assert response.status_code == 400
        return []

    monkeypatch.setattr(robot_codex, "call", call)
    monkeypatch.setattr(robot_codex, "turn", turn)
    if failure:
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(robot_codex.run(args))
    else:
        result = asyncio.run(robot_codex.run(args))
        assert result["status"] == "agent_completed"
        assert stages == ["init", "task"]
    if failure in {"active", "acknowledgment"}:
        assert not any(args.get("operation") == "start" for _, args in calls)
    for path in (tmp_path / "evidence").rglob("*.json"):
        assert "upstream-secret" not in path.read_text()


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get("ROBOT_CODEX_CLI_E2E") != "1", reason="Opt in to paid Codex CLI E2E"
)
@pytest.mark.parametrize("http_recorder", ["full-colors"], indirect=True)
def test_real_codex_model_with_automatic_observations_and_simulated_robot(http_recorder, tmp_path):  # noqa: F811
    call, url, _, _, _, root = http_recorder
    task = tmp_path / "task.txt"
    task.write_text(
        "This is a short integration test with simulated arms and three synthetic cameras; "
        "no real hardware is connected. First describe the dominant color of each camera "
        "using the automatically injected images, before calling any tools. "
        "Start the two simulated arm sessions if disconnected; startup support is authorized. "
        "Move left joint 1 to 0.05 rad and right joint 1 to -0.05 rad, leaving other joints zero, "
        "then return both arms to all-zero joints. Also command each gripper to 0.6 and then "
        "fully open. Use 0.2-second durations. Put each action in its own tool call. "
        "Between movements run pwd in a separate tool call; the next model response must "
        "briefly report current joint and gripper readings from the injected observation. "
        "Do not request live images using observe or view_image for this test: use the "
        "automatic images. You may inspect saved video frames after finishing. "
        "Complete the normal neutral verification, recording finish, video review, and "
        "success report. Retain both simulated sessions for test assertions."
    )
    args = robot_codex.Args(
        task_file=task,
        workspace=tmp_path / "agent",
        output=tmp_path / "evidence",
        url=url,
        repo=root,
        startup_supported=True,
        codex=shutil.which("codex") or "/usr/lib/chatgpt/resources/codex",
    )
    result = asyncio.run(robot_codex.run(args))
    assert result["task"]["phase"] == "success"
    metadata = json.loads((args.output / "metadata.json").read_text())
    recording = Path(metadata["recording"])
    trajectory = [
        json.loads(line) for line in (recording / "events.jsonl").read_text().splitlines()
    ]
    actions = [
        e["arguments"]["action"]
        for e in trajectory
        if e["kind"] == "request" and e.get("tool") == "execute"
    ]
    for side in ("left", "right"):
        assert any(
            a["arm"] == side and any(abs(q) > 0.04 for q in a.get("joints_rad", []))
            for a in actions
        )
        assert any(a["arm"] == side and a.get("gripper_opening") == 0.6 for a in actions)
        state = call("session", {"operation": "status"})["arms"][side]
        assert state["joints_rad"] == pytest.approx([0] * 6)
        assert state["gripper_opening"] == pytest.approx(1)
    injected = [
        json.loads(p.read_text()) for p in sorted((args.output / "observations").glob("*.json"))
    ]
    active = [o for o in injected if o["state"]["recording_phase"] in {"active", "returning"}]
    assert len(active) >= 5
    assert all(set(o["images"]) == {"left", "top", "right"} for o in active)
    assert all(o["images"]["left"]["width"] == 1920 for o in active)
    assert any(
        o["state"]["arms"]["left"] and abs(o["state"]["arms"]["left"]["joints_rad"][0]) > 0.04
        for o in active
    )
    assert any(
        o["state"]["arms"]["right"] and o["state"]["arms"]["right"]["gripper_opening"] == 0.6
        for o in active
    )
    events = [
        json.loads(line) for line in (args.output / "task-events.jsonl").read_text().splitlines()
    ]
    first_message = next(
        e["item"]["text"] for e in events if e.get("item", {}).get("type") == "agent_message"
    )
    assert all(color in first_message.lower() for color in ("red", "green", "blue"))
    assert any("pwd" in e.get("item", {}).get("command", "") for e in events)
