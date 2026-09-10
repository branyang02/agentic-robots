"""Discover cameras, save previews, or test all configured RGB streams at 30 FPS."""

import concurrent.futures
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tyro


@dataclass
class Args:
    command: tyro.conf.Positional[Literal["list", "preview", "check"]]
    """Discover cameras, save previews, or check configured frame rates."""


def formats(device):
    result = subprocess.run(
        ["v4l2-ctl", "-d", str(device), "--list-formats-ext"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def discover():
    cameras = []
    for device in sorted(Path("/dev/v4l/by-path").glob("*-video-index0")):
        if "-usb-" not in device.name:  # Skip duplicate usbv2 aliases.
            continue
        supported = formats(device)
        if "'MJPG'" in supported:
            fmt, width, height = "mjpeg", 1280, 720
        elif "'YUYV'" in supported:
            fmt, width, height = "yuyv422", 640, 480
        else:
            continue  # Depth/IR/metadata nodes are not RGB cameras.
        node = device.resolve().name
        name = (Path("/sys/class/video4linux") / node / "name").read_text().strip()
        cameras.append(dict(device=str(device), format=fmt, width=width, height=height, name=name))
    return cameras


def configured_cameras(roles=("left", "right", "top")):
    return {
        role: dict(
            device=os.environ[f"{role.upper()}_CAMERA"],
            format="yuyv422" if role == "top" else "mjpeg",
            width=640 if role == "top" else 1280,
            height=480 if role == "top" else 720,
        )
        for role in roles
    }


def input_args(camera):
    return [
        "-f",
        "v4l2",
        "-input_format",
        camera["format"],
        "-framerate",
        "30",
        "-video_size",
        f"{camera['width']}x{camera['height']}",
        "-i",
        camera["device"],
    ]


def auto_exposure(camera):
    controls = subprocess.run(
        ["v4l2-ctl", "-d", camera["device"], "--list-ctrls"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "exposure_auto " in controls or "auto_exposure " in controls:
        control = "auto_exposure" if "auto_exposure " in controls else "exposure_auto"
        subprocess.run(
            ["v4l2-ctl", "-d", camera["device"], f"--set-ctrl={control}=3"],
            check=True,
        )


def capture(name, camera, seconds):
    device = Path(camera["device"])
    if not device.exists():
        raise ValueError(f"Missing camera: {device}. Rediscover with setup-cameras list.")
    # Apply to the ultrawides only. Leave the RealSense controls unchanged.
    if camera["format"] == "mjpeg":
        auto_exposure(camera)
    output = Path("outputs") / name
    output.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if seconds:
        # Count actual input frames, with no output frame duplication or rate conversion.
        command += [
            "-progress",
            "pipe:1",
            *input_args(camera),
            "-t",
            str(seconds),
            "-fps_mode",
            "passthrough",
            "-f",
            "null",
            "-",
        ]
    else:
        command += [
            *input_args(camera),
            "-vf",
            "select=gte(t\\,3)",
            "-frames:v",
            "1",
            "-update",
            "1",
            str(output / "preview.png"),
        ]
    start = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True, timeout=(seconds or 4) + 20)
    if result.returncode:
        raise RuntimeError(f"{name}: {result.stderr.strip()}")
    if not seconds:
        return {"camera": name, "preview": str(output / "preview.png")}
    frames = [int(value) for value in re.findall(r"^frame=(\d+)$", result.stdout, re.MULTILINE)]
    timestamps = [
        int(value) for value in re.findall(r"^out_time_us=(\d+)$", result.stdout, re.MULTILINE)
    ]
    if not frames or not timestamps or timestamps[-1] <= 0:
        raise RuntimeError(f"{name}: no measurable frames")
    fps = frames[-1] / (timestamps[-1] / 1_000_000)
    return dict(
        camera=name,
        frames=frames[-1],
        fps=round(fps, 2),
        wall_seconds=round(time.monotonic() - start, 2),
        passed=29 <= fps <= 31,
        warnings=result.stderr.strip(),
    )


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
