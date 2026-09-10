"""Calibrate one YAM gripper under supervision."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tyro

from agentic_robots.calibration import save_limits
from agentic_robots.can import inventory, ready, resolve
from agentic_robots.hardware import close_robot


@dataclass
class Args:
    side: tyro.conf.Positional[Literal["left", "right"]]
    """Arm whose gripper will be calibrated."""


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
