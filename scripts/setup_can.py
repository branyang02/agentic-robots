"""List or configure Classic CAN without sending motor commands."""

import argparse
import json
import os
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["list", "setup"])
    args = parser.parse_args()
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
