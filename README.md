# agentic-robots

Setup for a dual-YAM robot: CAN, two ultrawide cameras, and a RealSense D435 RGB camera.
No policy server, inference, or teleoperation yet.

Python 3.11, uv, Ruff, pytest. The only application dependency is i2rt, pinned to
`7ed46f4e4e316133a0c39aa6cf34a73d2718e850`. Camera tools use FFmpeg and V4L2.

## Install (Ubuntu)

Run from this repository:

```bash
sudo apt-get update
sudo apt-get install -y build-essential git linux-libc-dev pkg-config python3-dev ffmpeg v4l-utils
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv sync --locked
uv run ruff check .
uv run pytest
```

If camera access is denied, run `sudo usermod -aG video "$USER"`, then log out and back in.

## Identify the hardware

Stop other robot and camera programs. Power down before changing robot wiring.
Connect one CAN adapter at a time and trace its cable to the arm:

```bash
uv run setup-can list
```

Do not assign sides from `can0`/`can1`. Record the confirmed USB serials.

Find color cameras and capture previews:

```bash
uv run setup-cameras list
```

Open `outputs/discovered-*/preview.png`. Cover one lens if needed to identify its side.
Use each camera's printed stable device path:

- `left` and `right`: ultrawides, MJPEG, 1280×720.
- `top`: RealSense color stream, YUYV, 640×480.

All request 30 FPS. RealSense capture is RGB only through V4L2; depth is not enabled.
Paths depend on the USB port. Rediscover after changing ports.

## Set hardware values

Replace the placeholders below with the values printed during discovery.
Run these exports in each terminal before using the setup scripts.

```bash
export ROBOT_ID=dual-yam
export LEFT_CAN=LEFT_ADAPTER_SERIAL
export RIGHT_CAN=RIGHT_ADAPTER_SERIAL
export LEFT_CAMERA=/dev/v4l/by-path/LEFT_CAMERA_PATH
export RIGHT_CAMERA=/dev/v4l/by-path/RIGHT_CAMERA_PATH
export TOP_CAMERA=/dev/v4l/by-path/REALSENSE_RGB_PATH
```

`TOP_CAMERA` is the RealSense RGB device path printed by discovery.

## Configure CAN

Turn on motor power and release the emergency stop. USB power alone is not enough.

```bash
uv run setup-can setup
ip -details link show type can
```

Both links must be UP, ERROR-ACTIVE, Classic CAN at 1000000 bit/s.
Repeat after a reboot or adapter replug. Setup sends no motor commands.
If calibration causes ERROR-PASSIVE, check motor power, emergency stop, wiring,
and termination. Repeating setup only clears the error state.

## Check cameras

```bash
uv run setup-cameras preview
uv run setup-cameras check
```

Open `outputs/left/preview.png`, `outputs/right/preview.png`, and `outputs/top/preview.png`.
Check side assignments, exposure, focus, and visibility of the work area.
The scripts enable automatic exposure on MJPEG ultrawides to avoid stale manual settings.

The check runs all three cameras concurrently for 15 seconds, prints measured rates,
and saves `outputs/report.json`. It fails outside 29–31 FPS. This tests capture, not inference.

## Calibrate grippers

This enables one arm and moves its gripper. Keep the workspace clear and supervise it.
Support the arm before the script finishes: shutdown releases motor torque.

```bash
uv run calibrate-gripper left
uv run calibrate-gripper right
```

Each command asks you to type the side before starting. Limits are saved to
`calibration/dual-yam.json`; previous values are backed up. Calibrate once per physical setup,
then again only when the gripper or calibration changes.

## Development

```bash
uv run ruff format .
uv run ruff check .
uv run pytest
```

Tests use simulation and temporary files. They do not connect to motors.
