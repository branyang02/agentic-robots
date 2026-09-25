"""Reusable cameras discovery and configuration helpers."""

import os
import re
import subprocess
import time
from pathlib import Path


def formats(device):
    result = subprocess.run(
        ["v4l2-ctl", "-d", str(device), "--list-formats-ext"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def resolution():
    value = os.environ.get("CAMERA_RESOLUTION", "current")
    if value not in {"current", "full"}:
        raise ValueError(f"Invalid CAMERA_RESOLUTION={value!r}. Choose 'current' or 'full'.")
    return value


def camera_modes(description):
    """Read discrete RGB modes from v4l2-ctl, ignoring depth/IR formats."""
    modes = []
    fmt, size = None, None
    for line in description.splitlines():
        if match := re.search(r"\[\d+\]: '([^']+)'", line):
            fmt = {"MJPG": "mjpeg", "YUYV": "yuyv422"}.get(match[1])
            size = None
        elif "Size:" in line:
            match = re.search(r"Size: Discrete (\d+)x(\d+)", line)
            size = tuple(map(int, match.groups())) if match else None
        elif fmt and size and (match := re.search(r"\(([\d.]+) fps\)", line)):
            modes.append(dict(format=fmt, width=size[0], height=size[1], fps=float(match[1])))
    return modes


def select_camera(device, modes, mode):
    if not modes:
        raise ValueError(f"{device}: no supported discrete MJPEG/YUYV RGB modes")
    if mode == "full":
        area = max(m["width"] * m["height"] for m in modes)
        candidates = [m for m in modes if m["width"] * m["height"] == area and m["fps"] >= 5]
    else:
        # Preserve the original capture sizes; observations are downscaled by the recorder.
        candidates = [
            m
            for m in modes
            if (m["width"], m["height"]) == ((1280, 720) if m["format"] == "mjpeg" else (640, 480))
        ]
    if not candidates:
        raise ValueError(f"{device}: no supported {mode!r} RGB mode. Run setup-cameras list.")
    # Prefer MJPEG to reduce USB bandwidth. Never request an unadvertised frame rate.
    selected = min(
        candidates,
        key=lambda m: (
            m["format"] != "mjpeg",
            m["fps"] if mode == "full" else abs(m["fps"] - 30),
            m["fps"],
        ),
    )
    return dict(device=str(device), **selected, resolution=mode)


def discover():
    cameras = []
    mode = resolution()
    for device in sorted(Path("/dev/v4l/by-path").glob("*-video-index0")):
        if "-usb-" not in device.name:  # Skip duplicate usbv2 aliases.
            continue
        modes = camera_modes(formats(device))
        if not modes:
            continue  # Depth/IR/metadata nodes are not RGB cameras.
        node = device.resolve().name
        name = (Path("/sys/class/video4linux") / node / "name").read_text().strip()
        cameras.append(dict(select_camera(device, modes, mode), name=name))
    return cameras


def configured_cameras(roles=("left", "right", "top")):
    mode = resolution()
    cameras = {}
    for role in roles:
        device = os.environ[f"{role.upper()}_CAMERA"]
        cameras[role] = select_camera(device, camera_modes(formats(device)), mode)
    return cameras


def input_args(camera):
    return [
        "-f",
        "v4l2",
        "-input_format",
        camera["format"],
        "-framerate",
        f"{camera['fps']:g}",
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
    if not frames or frames[-1] <= 0 or not timestamps or timestamps[-1] <= 0:
        raise RuntimeError(f"{name}: no measurable frames")
    fps = frames[-1] / (timestamps[-1] / 1_000_000)
    return dict(
        camera=name,
        frames=frames[-1],
        fps=round(fps, 2),
        wall_seconds=round(time.monotonic() - start, 2),
        # Occasional dropped frames are acceptable; a dead stream is not.
        passed=frames[-1] > 0,
        warnings=result.stderr.strip(),
    )
