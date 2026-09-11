"""Discover cameras, save previews, or test all configured RGB streams at 30 FPS."""

import concurrent.futures
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tyro

from agentic_robots.cameras import capture, configured_cameras, discover


@dataclass
class Args:
    command: tyro.conf.Positional[Literal["list", "preview", "check"]]
    """Discover cameras, save previews, or check configured frame rates."""


def main():
    args = tyro.cli(Args, description=__doc__)
    if args.command == "list":
        for index, camera in enumerate(discover()):
            print(json.dumps(camera, indent=2))
            print(capture(f"discovered-{index}", camera, 0))
        return
    cameras = configured_cameras()
    devices = [Path(c["device"]).resolve() for c in cameras.values()]
    if len(set(devices)) != len(devices):
        raise ValueError("Two camera roles point to the same device")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cameras)) as pool:
        jobs = [
            pool.submit(capture, name, camera, 15 if args.command == "check" else 0)
            for name, camera in cameras.items()
        ]
        results = [job.result() for job in jobs]
    print(json.dumps(results, indent=2))
    Path("outputs/report.json").write_text(json.dumps(results, indent=2) + "\n")
    if any(not row.get("passed", True) for row in results):
        raise SystemExit("Camera rate check failed; see outputs/report.json")


if __name__ == "__main__":
    main()
