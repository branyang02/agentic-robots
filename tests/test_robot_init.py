"""Initializer contract tests: local MCP and a fake desktop, without motors or an LLM."""

import asyncio
import json
import socket
import uuid
from importlib.resources import files
from pathlib import Path
from unittest.mock import Mock

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from agentic_robots import codex
from agentic_robots.mcp import structured
from scripts import robot_init


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/robot_call.py").touch()
    args = robot_init.Args(str(uuid.uuid4()), repo=tmp_path)
    state = {
        "thread": {
            "id": args.thread_id,
            "kind": "codex",
            "hostId": "local",
            "status": {"type": "idle"},
        },
        "turns": [],
    }
    sent, calls = [], []
    app, recorder = MCPServer("fake-desktop"), MCPServer("fake-recorder")

    @recorder.tool()
    def recording(operation: str, thread_id: str = "", turn_id: str = ""):
        calls.append(operation)
        if operation == "bind":
            assert thread_id == args.thread_id and turn_id == "ack-turn"
            return structured({"status": "idle", "agent": {"thread_id": thread_id}})
        return structured({"status": "idle", "ready": False})

    @app.tool()
    def read_thread(threadId: str, turnLimit: int, includeOutputs: bool):
        assert threadId == args.thread_id
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(state))])

    @app.tool()
    def send_message_to_thread(threadId: str, prompt: str):
        assert threadId == args.thread_id
        sent.append(prompt)
        token = prompt.split("replying exactly:\n")[1].splitlines()[0]
        state["turns"] = [
            {
                "id": "ack-turn",
                "status": "completed",
                "items": [{"type": "agentMessage", "phase": "final", "text": token}],
            }
        ]
        return CallToolResult(content=[TextContent(type="text", text='{"status":"sent"}')])

    async def locate_app(**kwargs):
        return app

    monkeypatch.setattr(robot_init, "locate_app", locate_app)
    monkeypatch.setattr(
        robot_init,
        "Client",
        lambda target, **kw: Client(recorder if target == args.url else target, **kw),
    )
    return args, state, sent, calls


@pytest.mark.parametrize("phase", ["final", "final_answer"])
def test_initializer_sends_one_prompt_and_confirms_agent_ack_without_motion(
    setup, phase, monkeypatch
):
    args, _, sent, calls = setup
    real_call = robot_init.app_call

    async def versioned_reply(client, thread, tool, arguments):
        result = await real_call(client, thread, tool, arguments)
        if tool == "read_thread" and result.get("turns"):
            result["turns"][0]["items"][0]["phase"] = phase
        return result

    monkeypatch.setattr(robot_init, "app_call", versioned_reply)
    result = asyncio.run(robot_init.initialize(args))
    assert result["status"] == "acknowledged"
    assert len(sent) == 1
    assert calls == ["status", "bind"]
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["prompt"] == sent[0]
    assert files("agentic_robots").joinpath("robot_agent.md").read_text() in sent[0]
    assert receipt["turn_id"] == "ack-turn"
    assert "Do not run a robot task" in sent[0]
    assert str(args.repo) in sent[0] and args.url in sent[0]
    assert "authorized by the initializer: False" in sent[0]


@pytest.mark.parametrize("change", ["active", "remote", "chatgpt"])
def test_rejects_busy_or_nonlocal_conversations_without_sending(setup, change):
    args, state, sent, _ = setup
    if change == "active":
        state["thread"]["status"]["type"] = change
    elif change == "remote":
        state["thread"]["hostId"] = "remote-machine"
    else:
        state["thread"]["kind"] = change
    with pytest.RaisesGroup(ValueError, flatten_subgroups=True):
        asyncio.run(robot_init.initialize(args))
    assert sent == []


def test_timeout_keeps_sent_receipt_without_resending(setup, monkeypatch):
    args, state, sent, _ = setup
    args.timeout_s = 0.001
    real_call = robot_init.app_call

    async def missing_ack(client, thread, tool, arguments):
        result = await real_call(client, thread, tool, arguments)
        if tool == "send_message_to_thread":
            state["turns"][0]["items"][0]["text"] = "A different reply"
        return result

    monkeypatch.setattr(robot_init, "app_call", missing_ack)
    with pytest.RaisesGroup(
        pytest.RaisesExc(TimeoutError, match="Inspect the conversation"), flatten_subgroups=True
    ):
        asyncio.run(robot_init.initialize(args))
    assert len(sent) == 1
    receipt = json.loads(
        (args.repo / "outputs/robot-init" / (args.thread_id + ".json")).read_text()
    )
    assert receipt["status"] == "sent"
    assert receipt["error"]


@pytest.mark.parametrize("case", ["confirmed", "missing", "wrong", "active", "stale"])
def test_omitted_desktop_messages_require_unique_receipt_and_new_completed_turn(
    setup, monkeypatch, case
):
    args, state, sent, _ = setup
    args.timeout_s = 0.001
    if case == "stale":
        state["turns"] = [{"id": "ack-turn", "status": "completed", "items": []}]
    real_call = robot_init.app_call

    async def omitted_reply(client, thread, tool, arguments):
        result = await real_call(client, thread, tool, arguments)
        if tool == "send_message_to_thread":
            receipt = json.loads(
                (args.repo / "outputs/robot-init" / (args.thread_id + ".json")).read_text()
            )
            if case != "missing":
                Path(receipt["acknowledgment_file"]).write_text(
                    "old acknowledgment" if case == "wrong" else receipt["acknowledgment"]
                )
            state["turns"][0]["items"] = []
            if case == "active":
                state["turns"][0]["status"] = "inProgress"
        return result

    monkeypatch.setattr(robot_init, "app_call", omitted_reply)
    if case == "confirmed":
        result = asyncio.run(robot_init.initialize(args))
        assert result["status"] == "acknowledged"
    else:
        with pytest.RaisesGroup(TimeoutError, flatten_subgroups=True):
            asyncio.run(robot_init.initialize(args))
    assert len(sent) == 1
    assert "write the exact acknowledgment" in sent[0]


@pytest.mark.parametrize(
    "field,value", [("thread_id", "bad-id"), ("timeout_s", 0), ("url", "https://example.com/mcp")]
)
def test_bad_input_is_rejected_before_connecting(tmp_path, monkeypatch, field, value):
    args = robot_init.Args(str(uuid.uuid4()), repo=tmp_path)
    setattr(args, field, value)
    client = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(robot_init, "Client", client)
    with pytest.raises(ValueError):
        asyncio.run(robot_init.initialize(args))
    client.assert_not_called()


def test_app_errors_are_not_reported_as_success():
    server = MCPServer("failed-desktop")

    @server.tool()
    def read_thread():
        return CallToolResult(isError=True, content=[TextContent(type="text", text="No task")])

    async def run():
        async with Client(server) as client:
            await robot_init.app_call(client, "task", "read_thread", {})

    with pytest.RaisesGroup(
        pytest.RaisesExc(RuntimeError, match="No task"), flatten_subgroups=True
    ):
        asyncio.run(run())


def test_cli_errors_include_underlying_transport_details():
    wrapped = ExceptionGroup("task group", [ExceptionGroup("nested", [ValueError("No task")])])
    assert robot_init.error_message(wrapped) == "No task"


def test_explicit_desktop_adapter_and_pipe(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/launch_codex_app_tools_mcp").touch()
    (tmp_path / "server.mjs").touch()
    args = robot_init.Args("unused", app_tools=tmp_path, app_pipe=tmp_path / "desktop.sock")
    transport = codex.app_transport(args.app_pipe, args.app_tools)
    assert transport.command == str(tmp_path / "scripts/launch_codex_app_tools_mcp")
    assert transport.env["CODEX_APP_TOOLS_PIPE_PATH"] == str(args.app_pipe)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_discovery_distinguishes_browser_sockets_and_rejects_ambiguity(
    tmp_path, monkeypatch, ambiguous
):
    monkeypatch.delenv("CODEX_APP_TOOLS_PIPE_PATH", raising=False)
    monkeypatch.setattr(codex.tempfile, "gettempdir", lambda: str(tmp_path))
    directory = tmp_path / "codex-browser-use"
    directory.mkdir()
    app, browser = MCPServer("app"), MCPServer("browser")

    @app.tool()
    def read_thread():
        pass

    @app.tool()
    def send_message_to_thread():
        pass

    monkeypatch.setattr(
        codex,
        "app_transport",
        lambda pipe, tools: app if pipe.name == "app.sock" or ambiguous else browser,
    )
    with socket.socket(socket.AF_UNIX) as a, socket.socket(socket.AF_UNIX) as b:
        a.bind(str(directory / "app.sock"))
        b.bind(str(directory / "browser.sock"))
        a.listen()
        b.listen()
        if ambiguous:
            with pytest.raises(RuntimeError, match="Could not select one"):
                asyncio.run(codex.locate_app())
        else:
            assert asyncio.run(codex.locate_app()) is app


def test_prompt_is_packaged_and_quotes_shell_paths(tmp_path):
    args = robot_init.Args("unused", repo=tmp_path / "a path; $()", startup_supported=True)
    prompt = robot_init.bootstrap(args, "TEST-ACK")
    assert "authorized by the initializer: True" in prompt
    assert "'" + str(args.repo) + "'" in prompt
    assert "recording" in prompt and "joint_target" in prompt
    assert "TEST-ACK" in prompt
