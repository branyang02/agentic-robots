"""Shared MCP tool definitions and structured robot responses."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Literal

from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from agentic_robots.bridge import Action, json_ready


def structured(value):
    value = json_ready(value)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(value))],
        structuredContent=value,
        isError=value.get("status") in ("rejected", "stopped"),
    )


def register_robot_tools(server, bridge):
    """Expose a controller or recording bridge through the same MCP action contract."""

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def observe() -> CallToolResult:
        """Return available camera images, current joints, timestamps, and per-device errors.

        The motor bridge captures available cameras on request. The recorder returns
        snapshots from an active recording; start recording first to get its images.
        The agent decides whether another observation is needed.
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
        Through the recorder, execution requires an active recording with all three
        camera streams fresh. Call recording(start, text=<user task>) first.
        Checks command validity, joint limits, sampled self-collision, and control health.
        completed means commands were sent, not that the task succeeded; inspect actual and
        joint_error_rad. rejected can be revised; stopped with fault_latched blocks new actions.
        Only errors marked recoverable permit session recover after inspection.
        Errors include a code, details, last feedback, retryable, recoverable, and next_step.
        The service stays up after errors.
        Different arms may execute concurrently. No automatic return or torque release.
        A client disconnect may let an action finish; session stop explicitly interrupts it.
        """
        return structured(await asyncio.to_thread(bridge.execute, action))

    @server.tool()
    async def session(
        operation: Literal["start", "status", "stop", "release", "recover"],
        arm: Literal["left", "right"] | None = None,
        supported: bool = False,
        reset_communication: bool = False,
    ) -> CallToolResult:
        """Explicit lifecycle. Start/release requires physical support.

        Start enables an arm, keeping gripper effort zero; stop retains powered hold.
        Stop affects the specified arm, or both if arm is omitted. An explicit stop does
        not latch a hardware fault. Status reports feedback errors independently per arm.
        Recover requires an arm and a latched software tracking fault. It verifies fresh
        feedback and powered hold at the measured pose before clearing that fault, preserving
        the gripper command. It never resumes an action, reconnects, or resets protection.
        Recovery failure retains the latch and returns diagnostics; inspect and correct them.
        Release removes torque. reset_communication permits one timeout reset at startup
        only, never a temperature/current/protection reset. Registration enables no motors.
        """
        return structured(
            await asyncio.to_thread(bridge.session, operation, arm, supported, reset_communication)
        )
