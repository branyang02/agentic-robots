"""Connect to the installed local Codex desktop adapter and decode its responses."""

import json
import os
import shutil
import socket
import stat
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.types import RequestParamsMeta


def app_transport(app_pipe=None, app_tools=None):
    """Reuse the desktop's bundled MCP adapter; never resume a parallel CLI session."""
    pipe = app_pipe or os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
    if pipe is None:
        raise ValueError("An app socket is required")
    resources = []
    if binary := shutil.which("codex"):
        resources.append(Path(binary).resolve().parent)
    resources += [
        Path("/usr/lib/chatgpt/resources"),
        Path("/Applications/Codex.app/Contents/Resources"),
    ]
    plugins = (
        [app_tools]
        if app_tools
        else [p / "plugins/openai-bundled/plugins/codex-app-tools" for p in resources]
    )
    if not app_tools:
        plugins += sorted(
            (Path.home() / ".codex/plugins/cache/openai-bundled/codex-app-tools").glob("*"),
            reverse=True,
        )
    for plugin in plugins:
        launcher = plugin / "scripts/launch_codex_app_tools_mcp"
        server = plugin / "server.mjs"
        if launcher.is_file() and server.is_file():
            env = {
                key: value
                for key, value in os.environ.items()
                if key in {"CODEX_MCP_NODE_PATH", "CODEX_CLI_PATH", "CODEX_ELECTRON_RESOURCES_PATH"}
            }
            env["CODEX_APP_TOOLS_PIPE_PATH"] = str(pipe)
            return StdioServerParameters(command=str(launcher), args=[str(server)], env=env)
    raise RuntimeError("Codex app tools were not found; supply --app-tools <plugin directory>")


async def locate_app(app_pipe=None, app_tools=None):
    if app_pipe or os.environ.get("CODEX_APP_TOOLS_PIPE_PATH"):
        return app_transport(app_pipe, app_tools)
    matches = []
    for path in (Path(tempfile.gettempdir()) / "codex-browser-use").glob("*.sock"):
        try:
            info = path.stat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                continue
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(0.2)
                connection.connect(str(path))
            transport = app_transport(path, app_tools)
            async with Client(transport, read_timeout_seconds=2) as client:
                catalog = await client.list_tools()
                if {"read_thread", "send_message_to_thread"} <= {t.name for t in catalog.tools}:
                    matches.append(transport)
        except Exception:
            # This directory also contains browser sockets, not just Codex app tools.
            continue
    if len(matches) != 1:
        raise RuntimeError(
            "Could not select one Codex app-tools socket. Open Codex desktop or "
            "supply --app-pipe and --app-tools for your installation."
        )
    return matches[0]


async def app_call(client, thread_id, tool, arguments):
    result = await client.call_tool(
        tool, arguments, meta=RequestParamsMeta(**{"openai/threadId": thread_id})
    )
    messages = [item.text for item in result.content if hasattr(item, "text")]
    if result.is_error:
        raise RuntimeError("Codex app rejected the request: " + "\n".join(messages))
    if len(messages) != 1:
        raise RuntimeError("Unexpected Codex app response; check the desktop adapter version")
    return json.loads(messages[0])


def error_message(exc):
    # MCP transports may wrap a useful error in several AnyIO task groups.
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(error_message(child) for child in exc.exceptions)
    return str(exc)
