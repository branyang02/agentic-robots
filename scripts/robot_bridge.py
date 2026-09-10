"""Serve local robot tools for the current Codex agent; no LLM API or second agent."""

from dataclasses import dataclass

import tyro
from mcp.server import MCPServer

from agentic_robots.bridge import Bridge
from agentic_robots.mcp import register_robot_tools


@dataclass
class Args:
    port: int = 8767
    """Port for the local motor bridge's MCP server."""


def make_server(bridge):
    server = MCPServer(
        "robot",
        instructions=(
            "Local robot observation, action, and session tools. The calling agent supplies "
            "targets and durations. Tool registration does not enable motors."
        ),
    )

    register_robot_tools(server, bridge)
    return server


def main():
    args = tyro.cli(Args, description=__doc__)
    make_server(Bridge()).run(transport="streamable-http", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
