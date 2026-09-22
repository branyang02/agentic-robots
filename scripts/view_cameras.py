"""Show left, top, and right cameras side by side on a graphical desktop."""

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import tyro

from agentic_robots.cameras import auto_exposure, configured_cameras, input_args

ORDER = ("left", "top", "right")


@dataclass
class Args:
    """The viewer uses the configured cameras and takes no options."""


def require_display():
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("No display. Run view-cameras in a terminal on the robot's desktop.")
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame  # Included by the pinned i2rt dependency.

    try:
        pygame.display.init()
        if pygame.display.get_driver() not in {"x11", "wayland"}:
            raise RuntimeError(
                "No usable X11/Wayland display; headless video drivers are unsupported."
            )
    except pygame.error as exc:
        raise RuntimeError(f"Cannot connect to the display: {exc}") from exc
    finally:
        pygame.display.quit()


def layout():
    panels = [
        f"[{i}:v]setpts=PTS-STARTPTS,"
        "scale=640:360:force_original_aspect_ratio=decrease,"
        f"pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]"
        for i in range(3)
    ]
    return ";".join([*panels, "[v0][v1][v2]hstack=inputs=3:shortest=1[out]"])


def capture_command(cameras):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    for role in ORDER:
        command += ["-thread_queue_size", "8", *input_args(cameras[role])]
    return command + [
        "-filter_complex",
        layout(),
        "-map",
        "[out]",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "rawvideo",
        "-f",
        "nut",
        "pipe:1",
    ]


def stop(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def view(cameras):
    paths = [Path(cameras[role]["device"]).resolve() for role in ORDER]
    if len(set(paths)) != 3:
        raise ValueError("Each camera role must use a different device")
    for path in paths:
        if not path.exists():
            raise ValueError(f"Camera missing: {path}. Run setup-cameras list.")
    for camera in cameras.values():
        if camera["format"] == "mjpeg":
            auto_exposure(camera)

    capture = player = None
    with tempfile.TemporaryFile() as capture_log, tempfile.TemporaryFile() as player_log:
        try:
            capture = subprocess.Popen(
                capture_command(cameras),
                stdout=subprocess.PIPE,
                stderr=capture_log,
            )
            player = subprocess.Popen(
                [
                    "ffplay",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-autoexit",
                    "-window_title",
                    "Left | Top | Right — Q/Esc to quit",
                    "-x",
                    "1440",
                    "-y",
                    "270",
                    "-f",
                    "nut",
                    "pipe:0",
                ],
                stdin=capture.stdout,
                stdout=subprocess.DEVNULL,
                stderr=player_log,
            )
            capture.stdout.close()
            while player.poll() is None and capture.poll() is None:
                time.sleep(0.1)
            if player.returncode == 0:
                return
            if capture.poll() is not None:
                capture_log.seek(0)
                raise RuntimeError("Camera capture stopped: " + capture_log.read().decode().strip())
            if player.returncode:
                player_log.seek(0)
                raise RuntimeError("Viewer failed: " + player_log.read().decode().strip())
        finally:
            stop(player)
            stop(capture)
            if capture and capture.stdout:
                capture.stdout.close()


def main():
    tyro.cli(Args, description=__doc__)
    try:
        require_display()  # Fail before opening cameras or changing camera controls.
        view(configured_cameras())
    except KeyboardInterrupt:
        pass
    except (RuntimeError, ValueError, OSError, KeyError) as exc:
        raise SystemExit(f"view-cameras: {exc}") from exc


if __name__ == "__main__":
    main()
