# agentic-robots

Setup for a dual-YAM robot: CAN, two ultrawide cameras, and a RealSense D435 RGB camera.
Includes an observation/action bridge for Codex. The agent is the Codex conversation.

To run an autonomous task with camera observations, numerical actions, feedback
corrections, and a complete video, follow [Run an agentic task](docs/agentic-runs.md).

Python 3.11, uv, Ruff, pytest. Hardware uses i2rt pinned to
`7ed46f4e4e316133a0c39aa6cf34a73d2718e850`; MCP exposes tools to the agent.
Camera tools use FFmpeg and V4L2.

## Project layout

```text
scripts/                  # Runnable programs: Args, main, workflow, and service lifecycle
src/agentic_robots/        # Reusable control, hardware, camera, recording, and transport code
  robot_agent.md          # Canonical agent instructions, included in the installed package
tests/                    # Unit, integration, and opt-in Codex tests
```

Console commands in `pyproject.toml` point directly to the corresponding modules in
`scripts/`. These modules compose library components and run each program; reusable
code imports from `agentic_robots` and never imports from `scripts`. The initializer
owns prompt delivery and acknowledgment; the library provides the desktop transport.

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

These discovery defaults request 30 FPS. RealSense capture is RGB only through V4L2;
depth is not enabled. Configured capture can use higher-resolution modes below.
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

### High-resolution camera setup

Use a checkout containing the configurable-camera and full-resolution-observation
changes. Finish active recording and close camera viewers before testing cameras.
Camera setup does not require enabling arms, configuring CAN, or calibrating grippers.

With the three identified device paths already saved in `.env`, add or replace
these nine settings in that file, one assignment per line. Preserve its other values.
These use the highest advertised resolutions and frame rates validated together
in a camera-only native-recording test on this USB 2.0 setup:

```dotenv
LEFT_CAMERA_WIDTH=1920
LEFT_CAMERA_HEIGHT=1200
LEFT_CAMERA_FPS=5
RIGHT_CAMERA_WIDTH=1920
RIGHT_CAMERA_HEIGHT=1200
RIGHT_CAMERA_FPS=5
TOP_CAMERA_WIDTH=1920
TOP_CAMERA_HEIGHT=1080
TOP_CAMERA_FPS=8
```

Higher supported frame rates remain configurable, but must be checked through the
complete recorder pipeline on the intended USB connection and host; a capture-only
rate check does not establish that native encoding can keep up.

Use `uv run --env-file .env` for setup, bridge, recorder, and viewer commands, and
remove conflicting camera-mode exports from the shell. Supported modes depend on
the camera and USB connection; inspect them with
`v4l2-ctl -d "$TOP_CAMERA" --list-formats-ext` using the exported device paths above
(repeat for each wrist). `setup-cameras list` still uses discovery defaults;
`preview` and `check` use the configured values.

Run the [camera checks](#check-cameras) below. Existing services must then be
restarted to load the changed code and environment; follow the
[session checks and service startup instructions](docs/agentic-runs.md#start-the-controller-and-recorder).
Never restart a controller holding enabled arms. Retain existing ports and recorder
output paths when reloading an existing setup.

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

From the repository root, with no other process capturing from these devices:

```bash
uv run --env-file .env setup-cameras preview
uv run --env-file .env setup-cameras check
```

Open `outputs/left/preview.png`, `outputs/right/preview.png`, and `outputs/top/preview.png`.
Check side assignments, exposure, focus, and visibility of the work area. For the
high-resolution settings, image dimensions must be 1920×1200 for both wrists and
1920×1080 overhead; all three rate checks must report `passed: true` (5/5/8 FPS
within tolerance). These files are overwritten by later checks/previews.
The scripts enable automatic exposure on MJPEG ultrawides to avoid stale manual settings.

The check runs all three cameras concurrently for 15 seconds, prints measured rates,
and saves `outputs/report.json`. It requires each measured frame rate to be within
5% of its configured rate. This tests capture, not inference.

Snapshots and agent observations retain the configured camera resolution as PNGs.
The recorded overview keeps its compact three-panel layout; only that video and
the live viewer are scaled. Each recording also saves `left.mp4`, `top.mp4`, and
`right.mp4` at the configured resolution and actual input frame cadence (5/8/5 FPS
with the settings above). These use H.264 CRF 18 with no resizing or frame-rate
upsampling; they add CPU and disk usage. Higher-resolution observations also
increase image payload sizes for model requests.

After service reload, call the motor bridge's `observe` while recording is idle
and confirm its returned image dimensions and empty camera errors. During the next
recording, also confirm the saved observations retain those dimensions and inspect
that recording's `ffmpeg.log` for capture errors: the rate check alone does not
exercise the complete recorder pipeline. Use the existing endpoint ports from your
setup; `uv run robot-call observe --url http://127.0.0.1:8767/mcp` targets the default bridge.

To restore the previous camera modes, remove the nine `*_CAMERA_WIDTH`,
`*_CAMERA_HEIGHT`, and `*_CAMERA_FPS` settings from `.env` and any shell exports,
then safely reload services and repeat the checks. Defaults remain 1280×720 at
30 FPS for both wrists and 640×480 at 30 FPS overhead. This keeps full-resolution
observations at those sizes. To restore the exact previous behavior, including
640-pixel-wide recorded observations and no native camera videos, also revert this
entire camera PR, including its recorder changes. Removing configuration alone
does not restore the previous recorder behavior.

## Live camera view

From a terminal on the robot's graphical desktop, with the hardware values exported:

```bash
uv run view-cameras
```

One window shows **left | top (RealSense) | right** side by side using the configured rates.
The views keep their original aspect ratios. Press **Q**, **Esc**, or close the window to quit.
Stop other camera programs first. A headless terminal fails before opening the cameras.

## Calibrate grippers

This enables one arm and moves its gripper. Keep the workspace clear and supervise it.
Support the arm before the script finishes: shutdown releases motor torque.

```bash
uv run calibrate-gripper left
uv run calibrate-gripper right
```

Each command asks you to type the side before starting. Limits are saved to
`calibration/<ROBOT_ID>.json` (`dual-yam` by default); previous values are backed up.
Calibrate once per physical setup, then again only when the gripper or calibration changes.

## Agent observation/action loop

Use [the run guide](docs/agentic-runs.md) to start the controller and recorder,
initialize Codex, and send tasks. All calls for an agentic task use the recorder
(port **8768** by default). `robot-init` supplies its address and the CLI commands; optional
native MCP connections are configured in `.codex/config.toml`.

The motor bridge on port **8767** provides `observe`, `execute`, and `session`.
It starts disconnected, with no motors enabled. The following direct bridge calls
are for manual inspection and control outside a recorded task. During a recording,
use port 8768 so the recorder owns camera access and logs the actions.

```bash
uv run robot-call observe --url http://127.0.0.1:8767/mcp --output outputs/observation.json
uv run robot-call session --url http://127.0.0.1:8767/mcp --arguments session.json
uv run robot-call execute --url http://127.0.0.1:8767/mcp --arguments action.json
```

Example `session.json`:

```json
{"operation":"start","arm":"left","supported":true}
```

`supported` asserts physical startup support. Add `reset_communication: true` only
when a communication-timeout reset during startup is explicitly authorized. It
never permits a motor protection reset.
Startup failure may leave some motors enabled; maintain support. The bridge does
not calibrate grippers. They start with zero effort until an explicit
`gripper_target` action enables jaw position control.

`observe` returns available image blocks, capture time bounds, current joints and
temperatures, and per-device errors. Direct bridge actions do not require camera
images. The recorder requires fresh frames from all three streams before forwarding
motion; neither endpoint checks image brightness.
Images are not hardware-synchronized; world-to-camera calibration is absent.
Observation does not enable motors and can run while an action is executing.

Example `action.json`:

```json
{"action":{"arm":"left","kind":"joint_delta","joints_rad":[0.05,0,0,0,0,0],"duration_s":6}}
```

The five action kinds are `joint_target`, `joint_delta`, `ee_target`, `ee_delta`,
and `gripper_target`. Gripper actions take `gripper_opening` from 0 (closed) to 1 (open)
and `duration_s`. Jaw actions preserve arm hold; arm actions preserve the last jaw
target. Jaw control uses the pinned driver's gripper gains and force limiter.
Check the returned `gripper_error` and images to assess whether closure succeeded.
Through the recorder, **every execute response** also includes `post_action` camera
images and measured arm feedback, each arm's `ee_pose` (FK at `grasp_site`, metres
and XYZW quaternion in its own base frame), `gripper` state, and concise `diagnostics`.
Jaw-action state distinguishes requested from measured opening and flags interruption;
it does not infer grasp success. Native MCP includes image blocks; `robot-call` JSON
contains absolute image paths. The recorder waits up to two seconds for frames
published after execution; stale/missing evidence is reported in `post_action.errors`
without replacing the original completed/rejected/stopped outcome. Publication
timestamps are not sensor exposure timestamps. Additional `observe` and session
status requests remain available. The internal motor bridge stays numeric and does
not access cameras on execute, so it cannot compete with the recorder for streams.
Units are metres/radians. EE targets require base-frame `position_m` and
`quaternion_xyzw`. EE deltas accept `position_m` and/or `rotation_vector_rad`,
with `frame: base` or `tool`. World commands fail with an explanation until a
world-to-base calibration exists. Deltas use measured state when execution begins.

The agent selects duration (positive seconds, default 5). The executor interpolates
joint angles linearly over that duration, with no action-size, speed, acceleration,
or temperature cap and no automatic time stretching. There is no observation ID,
observation age requirement, action count limit, or mandatory endpoint settling delay.
Timing can overrun due to operating-system/driver delays. Each arm accepts one action
at a time; different arms can execute concurrently. This does not coordinate their
trajectories or check collisions between the arms.

Four responsibilities remain in the bridge:

- Validate command structure, finite numbers, frames, and IK solvability. The IK
  solver and endpoint check both use 1 mm / 0.5° tolerances.
- Respect model joint limits and sampled self-collision. Collision checks sample
  at most 0.5° apart in joint space and reject penetration over 1 mm between
  nonadjacent bodies. This is a sampled model check, not continuous collision
  detection; furniture and the other arm are absent from the model.
- During execution, stop advancing if feedback is unavailable/older than 150 ms
  or joint tracking differs from the previous command by over 3°. Temperature and
  velocity remain telemetry, with no independent numerical cutoff. Motor firmware
  protections and the pinned driver's checks remain unchanged.
- Keep exclusive hardware ownership and persistent sessions, with explicit stop
  and release. The bridge stays running when a request fails.

`execute` returns one of:

| Status | Meaning and next action |
|---|---|
| `completed` | Commands finished; inspect `actual` and signed `joint_error_rad`, then decide whether the task succeeded. |
| `rejected` | No movement from this request; inspect the error and revise the action/configuration. |
| `stopped` | Execution interrupted or a prior control fault blocks it; inspect `fault_latched`, `hold`, and the error. |

Error feedback includes `error.code`, `error.message`, `error.details`,
`error.retryable`, `error.recoverable`, and `error.next_step`, plus the original `request` and
`last_feedback` when available. Joint-limit errors include allowed ranges and
violating joint numbers. IK failures include residuals and tolerances; collisions
identify bodies and a sampled pose; tracking failures include the previous command
and error. Invalid schema fields are also returned as tool errors. A rejected action
can be revised without restarting the session. The agent receives tool errors;
`robot-call` additionally exits with status 1 for rejected/stopped results.

A control fault blocks only the affected arm and requests holding measured joints
when feedback remains usable. Feedback may be unavailable, so the reported hold
request is not a guarantee. An explicit session `stop` interrupts the selected arm
(or both when `arm` is omitted) without releasing torque or latching a hardware fault.
The bridge does not clear motor protection faults or automatically reconnect.

A latched `tracking_error` is eligible for `session` with
`{"operation":"recover","arm":"right"}` (or left). Inspect the scene and failure
first. Recovery holds the measured joints, preserves the gripper command, and checks
fresh feedback after 150 ms against the existing 3° tracking tolerance. Success
returns `recovered` with measured state and clears the software latch; it sends no
trajectory. The agent then observes and chooses a corrected action. A failed check
keeps the original fault and returns its cause. Other fault types cannot be cleared
this way. `recoverable` means recovery may be attempted; `retryable` remains false
for actions while the fault is latched. Recovery needs the existing arm session and
uses the same per-arm lock as execution; it does not require release or startup.

The current Codex conversation is the agent. `src/agentic_robots/robot_agent.md` describes
the loop: choose when to observe, reason about a target and duration, execute, and assess the returned
feedback. There is no separate reasoning model, fixed task routine, human-assessment
step, or per-action approval. For example, start with the prompt:

> Move both arms to the middle of the table and back to neutral position.

The agent interprets the scene and explicitly selects each target and the return.
Every task includes returning both arms to neutral, even if the task message omits
it. The agent verifies fresh measured joints and images before declaring completion.
On this setup, the user authorizes torque release without repeated approval once
both arms are verified in the near-zero resting pose. Exact zero is unnecessary;
the existing neutral tolerances apply. Finish and review an active recording before
release, then recheck fresh feedback and the scene. See the canonical
[release checks and procedure](src/agentic_robots/robot_agent.md). An interrupted task
or stale feedback does not establish this condition. Session release disables motor
torque; it does not switch off the external power supply.

Client disconnect does not release torque and may let an action finish. Use session
stop to interrupt it. Only `{"operation":"release","arm":"left","supported":true}`
releases an arm. Release supported arms explicitly before stopping/restarting the
server; source changes are not hot-loaded into a running hardware session. The
project's MCP configuration and `robot-call` request timeout are 1 hour
(a transport setting, not an action-duration limit); other clients may impose their
own timeouts. No bridge health monitor runs between actions. Holding generates heat,
and power/driver/CAN failure can defeat
holding. This controller is not safety-rated.

## Record a complete rollout

Keep the existing motor bridge running. Start a persistent recording endpoint with
the camera environment variables exported, then initialize a local Codex conversation:

```bash
uv run robot-record --output-root outputs/rollouts
# In another terminal, while the target conversation is idle:
uv run robot-init --thread-id YOUR_CONVERSATION_ID
```

The recorder starts idle. After initialization acknowledges, send the task as an
ordinary Codex message. If new motor sessions need supported startup, initialize
with `--startup-supported` as described in the run guide. The agent starts a fresh
recording, executes and evaluates the task, verifies both arms have returned to
neutral, and finishes the video.
The same conversation and service support the next task. See
[the run guide](docs/agentic-runs.md) for setup and testing.

The recorder connects to the existing bridge at
`http://127.0.0.1:8767/mcp` and exposes its recording endpoint on port **8768**.
Override these with `--upstream` and `--port`. Starting or finishing recording does
not start, release, reset, or restart an arm. The recorder owns the cameras while
active, so use its endpoint for observations and all actions in the recorded run:

```bash
uv run robot-call observe --url http://127.0.0.1:8768/mcp --output outputs/observation.json
uv run robot-call execute --url http://127.0.0.1:8768/mcp --arguments action.json
uv run robot-call recording --url http://127.0.0.1:8768/mcp --arguments phase.json
```

The existing `observe`, `execute`, and `session` interfaces are preserved.
Observations copy immutable snapshots from the continuously captured streams and
include current joint feedback. The recorder forwards numerical actions unchanged.
Only calls through this endpoint are logged; direct actions on port 8767 bypass
the action log. The recorder does not contain an agent or task planner.

The additional `recording` tool takes one of these argument objects:

```json
{"operation":"status"}
{"operation":"start","text":"Close the grippers, draw a heart, and return to neutral"}
{"operation":"note","text":"Trace the upper lobes of the heart"}
{"operation":"finish"}
```

Phase notes are logged and displayed in the video. After the task and its final
observation, `finish` verifies measured neutral and refuses while an action is active.
The agent then reviews the video and records its decision with `recording(review)`.
The service accepts another `start` after that review; retries retain the previous
attempt's path and correction. See [the run guide](docs/agentic-runs.md) for the
completion, review, and idle-agent continuation protocol.
Between tasks, `observe` returns live joint feedback without camera images, and
controller calls leave completed recordings unchanged.
The resulting directory contains:

- `rollout.mp4`: the continuous left / top / right camera video, with elapsed time
  and phase notes. It preserves the full run, including pauses between actions.
- `left.mp4`, `top.mp4`, `right.mp4`: full-resolution individual camera videos at
  their input cadence, without overlays. `manifest.json` records measured video
  dimensions, frame rates, and duration under `native_videos`.
- `capture.mkv`: the overview and three native-resolution video streams, retained
  for recovery if MP4 finalization fails. Stream indices are 0=overview, 1=left,
  2=top, 3=right; select one explicitly when inspecting this container. The MP4
  files are remuxed without re-encoding.
- `events.jsonl`: the prompt/notes, timestamped requests and responses, observations,
  and joint/velocity/gripper/temperature/health feedback sampled at a requested 5 Hz.
- `observations/`: immutable images actually returned to the agent.
- `manifest.json` and `ffmpeg.log`: configuration, timestamps, video metadata, and
  recording errors. Check the manifest's final state before treating a run as complete.

Video and event timestamps share a software wall-clock origin. Camera timestamps
are converted by V4L2; the cameras are not hardware synchronized. Snapshot timestamps
are file publication times, and telemetry rate is best effort. This is a record of
what was commanded and observed, not an independent measurement of Cartesian accuracy.

If recording becomes unavailable, its endpoint rejects new task motion with detailed
recording status. An explicit `recording(return, text=<reason>)` declaration enables
agent-chosen return actions with best-effort logging and unchanged controller checks.
Session status, stop, and recovery remain available. A recorder failure does
not release torque or cancel an action already running in the motor bridge; inspect
upstream status before deciding whether a command needs correction. Finishing or
stopping the recorder leaves the independent motor bridge and its holds running.

## Development checks

```bash
uv run ruff format .
uv run ruff check .
uv run pytest
```

Tests cover observation failures, all action kinds, geometry, execution and fault
feedback, hardware ownership, MCP, and a complete HTTP/CLI loop with simulated arms.
Recording tests use synthetic FFmpeg streams and simulated HTTP robot hardware;
they verify video decoding, camera layout, immutable observations, event logging,
transport/capture/log-write failures, and preservation of hold after recorder shutdown.
HTTP test servers explicitly forbid CAN sockets. These validate software behavior,
not physical dynamics or task success on the real robot.

### Custom finger geometry

The pinned i2rt fork includes the supplied UMI Flow finger CAD and separate visual
and convex collision meshes. Joint frames, FK/IK, the 220 mm grasp site, and inertial
properties are unchanged. The registered CAD ends about 10–11 mm before that
intentional control reference; this is not a calibrated contact point. See the
[model alignment and limitations](https://github.com/AfterQuery-Research/i2rt/blob/d527fe853daaf663f2d5fc44ad1643da9ca5edbb/i2rt/robot_models/gripper/linear_4310/README.md).
