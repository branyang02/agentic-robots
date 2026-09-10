"""Parse robot command lines without connecting to motors, cameras, or MCP."""

import sys
from unittest.mock import Mock

import pytest

from scripts import robot_init, robot_mcp, robot_record


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("CLI parsing attempted external access")

    for module, name in (
        (robot_mcp, "Bridge"),
        (robot_mcp, "Client"),
        (robot_record, "Rollout"),
        (robot_record, "configured_cameras"),
        (robot_init, "Client"),
    ):
        monkeypatch.setattr(module, name, forbidden)


@pytest.mark.parametrize(
    "main", [robot_mcp.main, robot_mcp.call_main, robot_record.main, robot_init.main]
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
        (robot_mcp.main, ["--port", "invalid"]),
        (robot_mcp.call_main, []),
        (robot_mcp.call_main, ["invalid"]),
        (robot_mcp.call_main, ["execute", "--arguments"]),
        (robot_record.main, ["--output-root"]),
        (robot_record.main, ["--prompt-file", "prompt.txt"]),
        (robot_record.main, ["--port", "invalid"]),
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
    monkeypatch.setattr(robot_mcp, "Bridge", lambda: bridge)
    monkeypatch.setattr(robot_mcp, "make_server", factory)
    monkeypatch.setattr(sys, "argv", ["robot-bridge", *argv])
    robot_mcp.main()
    factory.assert_called_once_with(bridge)
    server.run.assert_called_once_with(transport="streamable-http", host="127.0.0.1", port=port)


@pytest.mark.parametrize("custom", [False, True])
def test_recorder_paths_and_server_options(tmp_path, monkeypatch, custom):
    output = tmp_path / "new rollout"
    factory = Mock()
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
    factory.return_value.recording.assert_called_once_with("finish")
