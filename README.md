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

## Live camera view

From a terminal on the robot's graphical desktop, with the hardware values exported:

```bash
uv run view-cameras
```

One window shows **left | top (RealSense) | right** side by side at 30 FPS.
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
On this setup, verified neutral is the resting pose where power can be cut; an
interrupted or failed task does not establish that state. Power removal remains an
explicit user action. See the [agent instructions](src/agentic_robots/robot_agent.md).

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
observation, call `finish`; it refuses while an action through the recorder is
still in flight. It keeps serving and accepts another `start` for the next task.
Between tasks, `observe` returns live joint feedback without camera images, and
controller calls leave completed recordings unchanged.
The resulting directory contains:

- `rollout.mp4`: the continuous left / top / right camera video, with elapsed time
  and phase notes. It preserves the full run, including pauses between actions.
- `capture.mkv`: the capture container retained for recovery if finalization fails.
- `events.jsonl`: the prompt/notes, timestamped requests and responses, observations,
  and joint/velocity/gripper/temperature/health feedback sampled at a requested 5 Hz.
- `observations/`: immutable images actually returned to the agent.
- `manifest.json` and `ffmpeg.log`: configuration, timestamps, video metadata, and
  recording errors. Check the manifest's final state before treating a run as complete.

Video and event timestamps share a software wall-clock origin. Camera timestamps
are converted by V4L2; the cameras are not hardware synchronized. Snapshot timestamps
are file publication times, and telemetry rate is best effort. This is a record of
what was commanded and observed, not an independent measurement of Cartesian accuracy.

If recording becomes unavailable, its endpoint rejects new motion with detailed
recording status. Explicit session stop remains available. A recorder failure does
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
