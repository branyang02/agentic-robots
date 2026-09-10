"""Calibrate one YAM gripper under supervision."""

import json
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tyro

from scripts.setup_can import inventory, ready, resolve


@dataclass
class Args:
    side: tyro.conf.Positional[Literal["left", "right"]]
    """Arm whose gripper will be calibrated."""


def save_limits(path, side, serial, limits):
    values = [float(value) for value in limits]
    if len(values) != 2 or not all(math.isfinite(v) for v in values) or values[0] == values[1]:
        raise ValueError("Invalid gripper limits; calibration was not saved")
    data = json.loads(path.read_text()) if path.exists() else {}
    data[side] = {"adapter_serial": serial, "gripper_limits": values}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(f".{time.time_ns()}.bak"))
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def close_robot(robot):
    """Work around the pinned i2rt version closing CAN before its motor thread exits."""
    chain = robot.motor_chain
    motor_threads = [
        thread
        for thread in threading.enumerate()
        if getattr(getattr(thread, "_target", None), "__self__", None) is chain
    ]
    deadline = time.monotonic() + 5

    def join(thread):
        thread.join(timeout=max(0, deadline - time.monotonic()))
        if thread.is_alive():
            raise RuntimeError(f"Timed out stopping {thread.name}; CAN socket was left open")

    robot._stop_event.set()
    join(robot._server_thread)
    chain.running = False
    for thread in motor_threads:
        join(thread)
    robot.close()


def main():
    args = tyro.cli(Args, description=__doc__)
    robot_id = os.environ.get("ROBOT_ID", "dual-yam")
    serial = os.environ[f"{args.side.upper()}_CAN"]
    row = resolve(serial, inventory())
    if not ready(row):
        raise RuntimeError(
            "CAN not ready. Run setup-can setup; check motor power and emergency stop."
        )
    print(f"This enables the {args.side} arm and moves its gripper. Keep clear of the gripper.")
    print("Support the arm: motor torque is released when this script finishes.")
    if input(f"Type {args.side} to start: ").strip() != args.side:
        raise SystemExit("Cancelled")
    from i2rt.robots.get_robot import get_yam_robot
    from i2rt.robots.utils import ArmType, GripperType

    robot = get_yam_robot(
        channel=row["name"],
        arm_type=ArmType.YAM,
        gripper_type=GripperType.LINEAR_4310,
        zero_gravity_mode=True,
        enable_auto_recovery=False,
    )
    try:
        path = Path("calibration") / f"{robot_id}.json"
        save_limits(path, args.side, serial, robot.get_robot_info()["gripper_limits"])
        print(f"Saved {args.side} gripper limits to {path}")
    finally:
        close_robot(robot)


if __name__ == "__main__":
    main()
