"""Serve per-task recording tools without owning or restarting motors."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tyro
from mcp.server import MCPServer

from agentic_robots.bridge import RobotError, failure
from agentic_robots.mcp import register_robot_tools, structured
from agentic_robots.recording import RecordingBridge


@dataclass
class Args:
    output_root: Path = Path("outputs/rollouts")
    """Parent directory for per-task recordings, created when the agent starts a task."""
    upstream: str = "http://127.0.0.1:8767/mcp"
    """Motor bridge's MCP endpoint."""
    port: int = 8768
    """Port for the recorder's MCP server."""


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
    async def recording(operation: Literal["start", "status", "note", "finish"], text: str = ""):
        """Start a new task video (text=user task), inspect it, add a note, or finish it.

        The server stays available between tasks. Finishing never releases robot torque.
        """
        try:
            result = await asyncio.to_thread(bridge.recording, operation, text)
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


def main():
    args = tyro.cli(Args, description=__doc__)
    bridge = RecordingBridge(output_root=args.output_root, upstream=args.upstream)
    try:
        recording_server(bridge).run(transport="streamable-http", host="127.0.0.1", port=args.port)
    finally:
        bridge.recording("finish")


if __name__ == "__main__":
    main()
