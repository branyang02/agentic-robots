"""Validate and persist gripper calibration without changing the other arm."""

import json
import math
import shutil
import time


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
