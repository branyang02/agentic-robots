import asyncio

import numpy as np
from mcp import Client
from PIL import Image

from scripts.robot_bridge import Bridge
from scripts.robot_mcp import make_server
from tests.robot_fakes import FakeArm, ManualClock


def test_mcp_session_observe_reject_revise_execute_return_and_reconnect(tmp_path):
    image = tmp_path / "top.png"
    Image.new("RGB", (20, 20), "black").save(image)
    clock = ManualClock()
    bridge = Bridge(
        lambda: ({"top": str(image)}, {"camera:left": "offline"}),
        clock=clock,
        sleep=clock.sleep,
        arm_factory=FakeArm,
    )
    server = make_server(bridge)

    async def check():
        async with Client(server) as client:
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == {"observe", "execute", "session"}
            tool = next(t for t in listing.tools if t.name == "execute")
            assert "observation_id" not in str(tool.input_schema)
            assert "Max 10" not in tool.description
            for arm in ("left", "right"):
                result = await client.call_tool(
                    "session", {"operation": "start", "arm": arm, "supported": True}
                )
                assert not result.is_error
            obs = await client.call_tool("observe", {})
            assert len([x for x in obs.content if x.type == "image"]) == 1
            assert obs.structured_content["errors"]["camera:left"] == "offline"
            bad = await client.call_tool(
                "execute",
                {
                    "action": {
                        "arm": "left",
                        "kind": "joint_target",
                        "joints_rad": [4, 0, 0, 0, 0, 0],
                    }
                },
            )
            assert bad.is_error
            assert bad.structured_content["status"] == "rejected"
            for arm in ("left", "right"):
                for degrees in (100, 0):
                    result = await client.call_tool(
                        "execute",
                        {
                            "action": {
                                "arm": arm,
                                "kind": "joint_target",
                                "duration_s": 0.02,
                                "joints_rad": np.deg2rad([degrees, 0, 0, 0, 0, 0]).tolist(),
                            }
                        },
                    )
                    assert not result.is_error, result.content
                    assert result.structured_content["status"] == "completed"
            assert not any(arm.closed for arm in bridge.arms.values())
        async with Client(server) as client:  # Closing a client did not release hardware.
            after = await client.call_tool("observe", {})
            assert all(
                arm["joints_rad"] == [0] * 6 for arm in after.structured_content["arms"].values()
            )
            for side in ("left", "right"):
                arm = bridge.arms[side]
                result = await client.call_tool(
                    "session", {"operation": "release", "arm": side, "supported": True}
                )
                assert not result.is_error
                assert arm.closed

    asyncio.run(check())
