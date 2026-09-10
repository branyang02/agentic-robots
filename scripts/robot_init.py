"""Initialize an existing local Codex app conversation, then wait for its acknowledgment."""

import asyncio
import json
import math
import shlex
import sys
import time
import uuid
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

import tyro
from mcp import Client

from agentic_robots.codex import app_call, error_message, locate_app


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


def bootstrap(args, acknowledgment, acknowledgment_file=None):
    repo = args.repo.resolve()
    command = shlex.join(["uv", "run", "robot-call", "--url", args.url])
    context = (
        f"Robot setup for this conversation\n\nRepository: {repo}\n"
        f"Recorder endpoint: {args.url}\n"
        f"Run tool commands from {shlex.quote(str(repo))}:\n{command}\n"
        f"Physical startup support authorized by the initializer: {args.startup_supported}.\n\n"
    )
    instructions = files("agentic_robots").joinpath("robot_agent.md").read_text(encoding="utf-8")
    confirmation = (
        "Before replying, write the exact acknowledgment below as UTF-8 text to "
        f"{str(acknowledgment_file)!r}. This confirms delivery when the desktop omits message "
        "text from its readback.\n"
        if acknowledgment_file is not None
        else ""
    )
    return (
        context
        + instructions
        + (
            "\nThis message only initializes the conversation. Do not run a robot task, start a "
            "recording, enable motors, or send actions.\n"
            f"{confirmation}Acknowledge by replying exactly:\n"
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
    nonce = uuid.uuid4().hex
    token = "Robot ready [init:" + nonce + "]"
    receipt = args.repo.resolve() / "outputs/robot-init" / (args.thread_id + ".json")
    acknowledgment_file = receipt.with_suffix(f".{nonce}.ack")
    prompt = bootstrap(args, token, acknowledgment_file)
    async with Client(
        await locate_app(app_pipe=args.app_pipe, app_tools=args.app_tools), read_timeout_seconds=30
    ) as app:
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
        previous_turns = {turn["id"] for turn in current.get("turns", [])}
        record = {
            "thread_id": args.thread_id,
            "url": args.url,
            "prompt": prompt,
            "acknowledgment": token,
            "acknowledgment_file": str(acknowledgment_file),
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
                    if turn.get("id") in previous_turns or turn.get("status") != "completed":
                        continue
                    message_ack = any(
                        item.get("type") == "agentMessage"
                        and item.get("phase") in {"final", "final_answer"}
                        and item.get("text", "").strip() == token
                        for item in turn.get("items", [])
                    )
                    # Some desktop versions omit completed message items. Require the
                    # agent's unique receipt AND a new completed turn in that case.
                    file_ack = (
                        not turn.get("items")
                        and acknowledgment_file.is_file()
                        and acknowledgment_file.read_text().strip() == token
                    )
                    if message_ack or file_ack:
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
