"""Reusable can discovery and configuration helpers."""

import json
import subprocess
from pathlib import Path


def inventory():
    result = subprocess.run(
        ["ip", "-json", "-details", "link", "show", "type", "can"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = []
    for link in json.loads(result.stdout):
        device = (Path("/sys/class/net") / link["ifname"] / "device").resolve()
        serial = next(
            (
                p.joinpath("serial").read_text().strip()
                for p in [device, *device.parents]
                if p.joinpath("serial").is_file()
            ),
            "unknown",
        )
        data = link.get("linkinfo", {}).get("info_data", {})
        rows.append(
            {
                "name": link["ifname"],
                "serial": serial,
                "up": "UP" in link.get("flags", []),
                "state": data.get("state"),
                "bitrate": data.get("bittiming", {}).get("bitrate"),
                "mtu": link["mtu"],
            }
        )
    return rows


def resolve(serial, rows):
    found = [row for row in rows if row["serial"].lower() == serial.strip().lower()]
    if len(found) != 1:
        raise ValueError(f"Expected one CAN adapter with serial {serial!r}; found {len(found)}")
    return found[0]


def ready(row):
    return (
        row["up"]
        and row["state"] == "ERROR-ACTIVE"
        and row["bitrate"] == 1_000_000
        and row["mtu"] == 16
    )
