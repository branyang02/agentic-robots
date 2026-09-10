"""Local MCP tools for the current Codex agent; no LLM API or second agent."""

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tyro
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from scripts.robot_bridge import Action, Bridge, json_ready, write_result


@dataclass
class Args:
    port: int = 8767
    """Port for the local motor bridge's MCP server."""


@dataclass
class CallArgs:
    tool: tyro.conf.Positional[Literal["observe", "execute", "session", "recording"]]
    """MCP tool to call."""
    arguments: Path | None = None
    """JSON file containing tool arguments."""
    output: Path | None = None
    """Write the structured result to this JSON file."""
    url: str = "http://127.0.0.1:8767/mcp"
    """MCP endpoint for the bridge or recorder."""


def structured(value):
    value = json_ready(value)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(value))],
        structuredContent=value,
        isError=value.get("status") in ("rejected", "stopped"),
    )


def make_server(bridge):
    server = MCPServer(
        "robot",
        instructions=(
            "The current agent chooses when to observe, targets, durations, and task completion. "
            "Show useful images and concise action explanations. Revise rejected commands; halt "
            "on latched control faults. No per-action human approval or fixed action budget. "
            "Actions have no size, speed, acceleration, or temperature cap. Respect physical "
            "clearance: the model excludes the table and the other arm. This service starts "
            "disconnected; only explicit release removes torque."
        ),
    )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def observe() -> CallToolResult:
        """Return available camera images, current joints, timestamps, and per-device errors.

        Images are optional. The agent decides whether another observation is needed.
        Does not enable motors; can run while actions are executing.
        """
        observation = json_ready(await asyncio.to_thread(bridge.observe))
        content = [TextContent(type="text", text=json.dumps(observation))]
        for name, record in observation["images"].items():
            content.append(TextContent(type="text", text=f"{name} camera"))
            content.append(
                ImageContent(
                    type="image",
                    mimeType="image/png",
                    data=base64.b64encode(Path(record["path"]).read_bytes()).decode(),
                )
            )
        return CallToolResult(content=content, structuredContent=observation)

    @server.tool()
    async def execute(action: Action) -> CallToolResult:
        """Execute a joint/EE target, delta, or gripper target. Units m/rad; base/tool frames.

        gripper_target takes gripper_opening from 0 (closed) to 1 (open) and duration_s.
        Jaw actions preserve arm hold; arm actions preserve the last jaw command.

        Deltas use the measured state at execution. World calibration is unavailable.
        duration_s is the requested positive duration of linear joint interpolation (default 5).
        No action size, speed/acceleration cap, time stretching, or observation prerequisite.
        Checks command validity, joint limits, sampled self-collision, and control health.
        completed means commands were sent, not that the task succeeded; inspect actual and
        joint_error_rad. rejected can be revised; stopped with fault_latched requires attention.
        Errors include a code, details, last feedback, retryable, and next_step.
        The service stays up after errors.
        Different arms may execute concurrently. No automatic return or torque release.
        A client disconnect may let an action finish; session stop explicitly interrupts it.
        """
        return structured(await asyncio.to_thread(bridge.execute, action))

    @server.tool()
    async def session(
        operation: Literal["start", "status", "stop", "release"],
        arm: Literal["left", "right"] | None = None,
        supported: bool = False,
        reset_communication: bool = False,
    ) -> CallToolResult:
        """Explicit lifecycle. Start/release requires physical support.

        Start enables an arm, keeping gripper effort zero; stop retains powered hold.
        Stop affects the specified arm, or both if arm is omitted. An explicit stop does
        not latch a hardware fault. Status reports feedback errors independently per arm.
        Release removes torque. reset_communication permits one timeout reset at startup
        only, never a temperature/current/protection reset. Registration enables no motors.
        """
        return structured(
            await asyncio.to_thread(bridge.session, operation, arm, supported, reset_communication)
        )

    return server


def main():
    args = tyro.cli(Args, description=__doc__)
    make_server(Bridge()).run(transport="streamable-http", host="127.0.0.1", port=args.port)


def call_main():
    """Same MCP tools via CLI when the current Codex turn cannot reload its tool catalog."""
    args = tyro.cli(CallArgs, description=call_main.__doc__)

    async def call():
        arguments = json.loads(args.arguments.read_text()) if args.arguments else {}
        async with Client(args.url, read_timeout_seconds=3600) as client:
            result = await client.call_tool(args.tool, arguments)
            value = result.structured_content
            if value is None:
                value = {"error": [item.text for item in result.content if hasattr(item, "text")]}
            if args.output:
                write_result(args.output, value)
            print(json.dumps(value))
            if result.is_error:
                raise SystemExit(1)

    asyncio.run(call())


if __name__ == "__main__":
    main()
