"""Serve per-task recording tools without owning or restarting motors."""

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tyro
from mcp import Client
from mcp.server import MCPServer

from agentic_robots.bridge import RobotError, failure
from agentic_robots.codex import app_call, error_message, locate_app
from agentic_robots.mcp import register_robot_tools, structured
from agentic_robots.recording import RecordingBridge
from agentic_robots.task import TERMINAL, Review, continuation, continuation_message


@dataclass
class Args:
    output_root: Path = Path("outputs/rollouts")
    """Parent directory for per-task recordings, created when the agent starts a task."""
    upstream: str = "http://127.0.0.1:8767/mcp"
    """Motor bridge's MCP endpoint."""
    port: int = 8768
    """Port for the recorder's MCP server."""
    camera_backend: Literal["ffmpeg", "rust"] = "ffmpeg"
    """Select legacy capture or isolated Rust camera workers."""


def recording_server(bridge):
    server = MCPServer(
        "robot",
        instructions=(
            "Local robot observation, action, and session tools. The calling agent supplies "
            "targets and durations. Tool registration does not enable motors."
        ),
    )

    register_robot_tools(server, bridge)

    @server.tool()
    async def recording(
        operation: Literal["start", "status", "note", "return", "finish", "review", "bind"],
        text: str = "",
        review: Review | None = None,
        thread_id: str = "",
        turn_id: str = "",
    ):
        """Record an attempt, declare an agent-planned return, verify neutral, then review.

        Finish checks measured neutral; review records retry/success/blocker evidence.
        Return sends no motion and permits recovery if video fails. Bind is for robot-init.
        The server stays available; none of these operations releases robot torque.
        """
        try:
            result = await asyncio.to_thread(
                bridge.recording, operation, text, review, thread_id, turn_id
            )
            if result["status"] == "failed":
                raise RuntimeError(result["error"])
            return structured(result)
        except Exception as exc:
            return structured(
                failure(
                    RobotError(
                        "recording_error",
                        str(exc),
                        recording=bridge.status(),
                    )
                )
            )

    return server


async def watch_once(bridge, app):
    binding, rollout = bridge.binding, bridge.rollout
    if (
        not binding
        or binding.get("delivery_unknown")
        or not rollout
        or rollout.manifest["task"]["phase"] in TERMINAL
    ):
        return
    thread = binding["thread_id"]
    page = await app_call(
        app,
        thread,
        "read_thread",
        {"threadId": thread, "turnLimit": 1, "includeOutputs": False},
    )
    if page.get("thread", {}).get("id") != thread:
        raise ValueError("Desktop returned a different conversation; no continuation sent")
    turn = continuation(bridge.status(), page, binding["after_turn_id"])
    if not turn or bridge.binding is not binding or bridge.rollout is not rollout:
        return
    feedback = await asyncio.to_thread(bridge.session, "status")
    # Recheck after I/O so a new user turn or completed review is not steered.
    page = await app_call(
        app,
        thread,
        "read_thread",
        {"threadId": thread, "turnLimit": 1, "includeOutputs": False},
    )
    if (
        bridge.binding is not binding
        or bridge.rollout is not rollout
        or page.get("thread", {}).get("id") != thread
        or continuation(bridge.status(), page, binding["after_turn_id"]) != turn
    ):
        return
    binding["after_turn_id"] = turn  # Never duplicate a send whose outcome is uncertain.
    try:
        await app_call(
            app,
            thread,
            "send_message_to_thread",
            {"threadId": thread, "prompt": continuation_message(bridge.status(), feedback)},
        )
    except Exception:
        binding["delivery_unknown"] = True
        raise


async def watch_agent(bridge, stop):
    while not stop.is_set():
        if bridge.binding:
            try:
                async with Client(await locate_app(), read_timeout_seconds=30) as app:
                    while bridge.binding and not stop.is_set():
                        await watch_once(bridge, app)
                        if not bridge.binding.get("delivery_unknown"):
                            bridge.watch_error = None
                        await asyncio.to_thread(stop.wait, 5)
            except Exception as exc:
                bridge.watch_error = error_message(exc)
        await asyncio.to_thread(stop.wait, 5)


def main():
    args = tyro.cli(Args, description=__doc__)
    bridge = RecordingBridge(
        output_root=args.output_root, upstream=args.upstream, camera_backend=args.camera_backend
    )
    stop = threading.Event()
    watcher = threading.Thread(target=lambda: asyncio.run(watch_agent(bridge, stop)), daemon=True)
    watcher.start()
    try:
        recording_server(bridge).run(transport="streamable-http", host="127.0.0.1", port=args.port)
    finally:
        stop.set()
        # File cleanup is independent of task completion and never controls the arms.
        if bridge.rollout:
            bridge.rollout.finish()
        watcher.join(timeout=5)


if __name__ == "__main__":
    main()
