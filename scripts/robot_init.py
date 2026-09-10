"""Initialize an existing local Codex app conversation, then wait for its acknowledgment."""

import asyncio
import json
import math
import os
import shlex
import shutil
import socket
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import tyro
from mcp import Client, StdioServerParameters
from mcp.types import RequestParamsMeta


@dataclass
class Args:
    thread_id: str
    """ID of an idle local conversation in the open Codex desktop app."""
    url: str = "http://127.0.0.1:8768/mcp"
    """Persistent recorder's MCP endpoint."""
    repo: Path = Path.cwd()
    """Repository containing the robot tools."""
    startup_supported: bool = False
    """Authorize supported motor startup for this conversation. Does not start motors now."""
    app_pipe: Path | None = None
    """Desktop app socket; defaults to its environment value or discovered app-tools socket."""
    app_tools: Path | None = None
    """Installed Codex app-tools plugin directory, if automatic discovery fails."""
    timeout_s: float = 180
    """Maximum wait for the initialization acknowledgment."""


def app_transport(args):
    """Reuse the desktop's bundled MCP adapter; never resume a parallel CLI session."""
    pipe = args.app_pipe or os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
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
        [args.app_tools]
        if args.app_tools
        else [p / "plugins/openai-bundled/plugins/codex-app-tools" for p in resources]
    )
    if not args.app_tools:
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


async def locate_app(args):
    if args.app_pipe or os.environ.get("CODEX_APP_TOOLS_PIPE_PATH"):
        return app_transport(args)
    matches = []
    for path in (Path(tempfile.gettempdir()) / "codex-browser-use").glob("*.sock"):
        try:
            info = path.stat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                continue
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(0.2)
                connection.connect(str(path))
            transport = app_transport(replace(args, app_pipe=path))
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


def bootstrap(args, acknowledgment):
    repo = args.repo.resolve()
    command = shlex.join(["uv", "run", "robot-call", "--url", args.url])
    context = (
        f"Robot setup for this conversation\n\nRepository: {repo}\n"
        f"Recorder endpoint: {args.url}\n"
        f"Run tool commands from {shlex.quote(str(repo))}:\n{command}\n"
        f"Physical startup support authorized by the initializer: {args.startup_supported}.\n\n"
    )
    instructions = Path(__file__).with_name("robot_agent.md").read_text()
    return (
        context
        + instructions
        + (
            "\nThis message only initializes the conversation. Do not run a robot task, start a "
            "recording, enable motors, or send actions. Acknowledge by replying exactly:\n"
            f"{acknowledgment}\nThen wait for the user's task message.\n"
        )
    )


async def initialize(args):
    if not math.isfinite(args.timeout_s) or args.timeout_s <= 0:
        raise ValueError("--timeout-s must be finite and positive")
    uuid.UUID(args.thread_id)  # Validate before accessing the app or writing a receipt.
    endpoint = urlsplit(args.url)
    if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("--url must be a local HTTP recorder endpoint")
    if not (args.repo / "scripts/robot_call.py").is_file():
        raise ValueError("--repo must contain the robot tools")
    # Read-only readiness check; this never enables motors or starts recording.
    async with Client(args.url, read_timeout_seconds=10) as robot:
        result = await robot.call_tool("recording", {"operation": "status"})
        if result.is_error or result.structured_content is None:
            raise RuntimeError("Recorder is unavailable; start robot-record first")
    token = "Robot ready [init:" + uuid.uuid4().hex + "]"
    prompt = bootstrap(args, token)
    receipt = args.repo.resolve() / "outputs/robot-init" / (args.thread_id + ".json")
    async with Client(await locate_app(args), read_timeout_seconds=30) as app:
        current = await app_call(
            app,
            args.thread_id,
            "read_thread",
            {"threadId": args.thread_id, "turnLimit": 1, "includeOutputs": False},
        )
        thread = current["thread"]
        if thread["id"] != args.thread_id:
            raise ValueError("Codex returned a different conversation ID; no prompt was sent")
        if thread["kind"] != "codex" or thread.get("hostId") != "local":
            raise ValueError("Choose a local Codex conversation on this computer")
        if thread["status"]["type"] not in {"idle", "notLoaded"}:
            raise ValueError("Wait for the target conversation to become idle before initializing")
        record = {
            "thread_id": args.thread_id,
            "url": args.url,
            "prompt": prompt,
            "acknowledgment": token,
            "status": "sending",
            "created_unix": time.time(),
        }
        receipt.parent.mkdir(parents=True, exist_ok=True)

        def save():
            receipt.write_text(json.dumps(record, indent=2) + "\n")

        save()
        try:
            await app_call(
                app,
                args.thread_id,
                "send_message_to_thread",
                {"threadId": args.thread_id, "prompt": prompt},
            )
            record["status"] = "sent"
            save()
            deadline = time.monotonic() + args.timeout_s
            while time.monotonic() < deadline:
                page = await app_call(
                    app,
                    args.thread_id,
                    "read_thread",
                    {"threadId": args.thread_id, "turnLimit": 1, "includeOutputs": False},
                )
                for turn in page.get("turns", []):
                    if turn.get("status") == "completed" and any(
                        item.get("type") == "agentMessage"
                        and item.get("phase") in {"final", "final_answer"}
                        and item.get("text", "").strip() == token
                        for item in turn.get("items", [])
                    ):
                        record.update(status="acknowledged", turn_id=turn["id"])
                        save()
                        return {
                            "status": "acknowledged",
                            "thread_id": args.thread_id,
                            "receipt": str(receipt),
                            "message": "Send your task in Codex.",
                        }
                await asyncio.sleep(2)
            raise TimeoutError(
                "Prompt sent, but acknowledgment was not confirmed. Inspect the "
                "conversation before retrying; no second prompt was sent."
            )
        except Exception as exc:
            if record["status"] == "sending":
                record["status"] = "delivery_unknown"
            record["error"] = error_message(exc)
            save()
            raise


def main():
    args = tyro.cli(Args, description=__doc__)
    try:
        print(json.dumps(asyncio.run(initialize(args))))
    except Exception as exc:
        print(json.dumps({"status": "error", "message": error_message(exc)}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
