"""Parse robot command lines without connecting to motors, cameras, or MCP."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agentic_robots import recording
from scripts import robot_bridge, robot_call, robot_init, robot_record


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("CLI parsing attempted external access")

    for module, name in (
        (robot_bridge, "Bridge"),
        (robot_call, "Client"),
        (recording, "Rollout"),
        (recording, "configured_cameras"),
        (robot_init, "Client"),
    ):
        monkeypatch.setattr(module, name, forbidden)


@pytest.mark.parametrize(
    "main", [robot_bridge.main, robot_call.main, robot_record.main, robot_init.main]
)
@pytest.mark.parametrize("argv,code", [(["--help"], 0), (["--unknown"], 2)])
def test_help_and_unknown_options(main, argv, code, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["robot-cli", *argv])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == code


@pytest.mark.parametrize(
    "main,argv",
    [
        (robot_bridge.main, ["--port", "invalid"]),
        (robot_call.main, []),
        (robot_call.main, ["invalid"]),
        (robot_call.main, ["execute", "--arguments"]),
        (robot_record.main, ["--output-root"]),
        (robot_record.main, ["--prompt-file", "prompt.txt"]),
        (robot_record.main, ["--port", "invalid"]),
        (robot_record.main, ["--camera-backend", "ffmpeg"]),
        (robot_init.main, []),
        (robot_init.main, ["--thread-id", "test", "--timeout-s", "invalid"]),
    ],
)
def test_invalid_arguments_exit_before_external_access(main, argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["robot-cli", *argv])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2


@pytest.mark.parametrize("argv,port", [([], 8767), (["--port", "9876"], 9876)])
def test_bridge_port_reaches_server(argv, port, monkeypatch):
    bridge = object()
    server = Mock()
    factory = Mock(return_value=server)
    monkeypatch.setattr(robot_bridge, "Bridge", lambda: bridge)
    monkeypatch.setattr(robot_bridge, "make_server", factory)
    monkeypatch.setattr(sys, "argv", ["robot-bridge", *argv])
    robot_bridge.main()
    factory.assert_called_once_with(bridge)
    server.run.assert_called_once_with(transport="streamable-http", host="127.0.0.1", port=port)


@pytest.mark.parametrize("custom", [False, True])
def test_recorder_paths_and_server_options(tmp_path, monkeypatch, custom):
    output = tmp_path / "new rollout"
    factory = Mock()
    factory.return_value.binding = None
    server = Mock()
    monkeypatch.setattr(robot_record, "RecordingBridge", factory)
    monkeypatch.setattr(robot_record, "recording_server", lambda rollout: server)
    argv = ["robot-record", "--output-root", str(output)]
    upstream = "http://127.0.0.1:8767/mcp"
    port = 8768
    if custom:
        upstream, port = "http://127.0.0.1:9876/mcp", 9877
        argv += ["--upstream", upstream, "--port", str(port)]
    monkeypatch.setattr(sys, "argv", argv)
    robot_record.main()
    factory.assert_called_once_with(output_root=output, upstream=upstream)
    factory.return_value.start.assert_not_called()
    server.run.assert_called_once_with(transport="streamable-http", host="127.0.0.1", port=port)
    factory.return_value.rollout.finish.assert_called_once_with()
    factory.return_value.recording.assert_not_called()


@pytest.mark.parametrize("is_error", [False, True])
def test_call_forwards_json_and_preserves_feedback(tmp_path, monkeypatch, capsys, is_error):
    arguments = {"action": {"arm": "left", "kind": "joint_target", "joints_rad": [0] * 6}}
    value = {"status": "rejected" if is_error else "completed", "details": "controller feedback"}
    request, output = tmp_path / "action.json", tmp_path / "result.json"
    request.write_text(json.dumps(arguments))
    client = Mock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.call_tool = AsyncMock(
        return_value=SimpleNamespace(structured_content=value, is_error=is_error)
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(robot_call, "Client", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "robot-call",
            "execute",
            "--url",
            "http://127.0.0.1:9999/mcp",
            "--arguments",
            str(request),
            "--output",
            str(output),
        ],
    )
    if is_error:
        with pytest.raises(SystemExit) as result:
            robot_call.main()
        assert result.value.code == 1
    else:
        robot_call.main()
    factory.assert_called_once_with("http://127.0.0.1:9999/mcp", read_timeout_seconds=3600)
    client.call_tool.assert_awaited_once_with("execute", arguments)
    assert json.loads(output.read_text()) == value
    assert json.loads(capsys.readouterr().out) == value
