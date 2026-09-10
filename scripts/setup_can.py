"""List or configure Classic CAN without sending motor commands."""

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Literal

import tyro

from agentic_robots.can import inventory, ready, resolve


@dataclass
class Args:
    command: tyro.conf.Positional[Literal["list", "setup"]]
    """List CAN adapters or configure the two selected adapters."""


def main():
    args = tyro.cli(Args, description=__doc__)
    rows = inventory()
    if args.command == "list":
        print(json.dumps(rows, indent=2))
        return
    config = {side: os.environ[f"{side.upper()}_CAN"] for side in ("left", "right")}
    if config["left"].lower() == config["right"].lower():
        raise ValueError("Left and right must use different CAN adapters")
    selected = [(side, resolve(config[side], rows)) for side in ("left", "right")]
    subprocess.run(["sudo", "-v"], check=True)
    for side, row in selected:
        name = row["name"]
        for tail in (["down"], ["type", "can", "bitrate", "1000000", "fd", "off"], ["up"]):
            subprocess.run(["sudo", "ip", "link", "set", name, *tail], check=True)
        current = resolve(row["serial"], inventory())
        if not ready(current):
            raise RuntimeError(f"CAN setup failed: {current}")
        print(f"{side}: {name}, {row['serial']}, ERROR-ACTIVE, 1000000 bit/s")
    print("No CAN frames or motor commands sent.")


if __name__ == "__main__":
    main()
