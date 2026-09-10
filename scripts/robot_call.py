"""Call robot MCP tools when the current Codex turn cannot reload its tool catalog."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tyro
from mcp import Client

from agentic_robots.bridge import write_result


@dataclass
class Args:
    tool: tyro.conf.Positional[Literal["observe", "execute", "session", "recording"]]
    """MCP tool to call."""
    arguments: Path | None = None
    """JSON file containing tool arguments."""
    output: Path | None = None
    """Write the structured result to this JSON file."""
    url: str = "http://127.0.0.1:8767/mcp"
    """MCP endpoint for the bridge or recorder."""


def main():
    args = tyro.cli(Args, description=__doc__)

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
