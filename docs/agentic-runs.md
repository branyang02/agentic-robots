# Run an agentic task

Open a local Codex conversation, initialize it once, then send tasks as ordinary
messages. Codex chooses the actions and evaluates their results. There is no Python
`Agent()` object, separate evaluator model, or predefined heart/lettering routine.

```mermaid
flowchart LR
    I[robot-init + conversation ID] -->|setup message / acknowledgment| A[Codex conversation]
    U[User task message] --> A
    A <-->|recording / observe / execute / session| R[Recorder :8768]
    R <-->|actions and feedback| B[Motor bridge :8767]
    B <--> H[i2rt / robot arms]
    C[Cameras] --> R
    R --> F[Video, events, and review per attempt]
    R -->|resume an unfinished idle turn| A
```

## Prepare the robot computer

Complete the [README hardware setup](../README.md): dependencies, adapters, CAN,
cameras, and gripper calibration. Save `ROBOT_ID`, `LEFT_CAN`, `RIGHT_CAN`,
`LEFT_CAMERA`, `RIGHT_CAMERA`, and `TOP_CAMERA` as shell assignments in the ignored
`.env` file. Calibration stays local in `calibration/<ROBOT_ID>.json`.
Camera values are single-quoted JSON objects containing `type`, `path`, `width`,
`height`, and `fps`; either camera type can serve any role. See the README examples.
Run the following commands from the repository root on the robot computer.

Inspect an existing controller first:

```bash
mkdir -p outputs
printf '{"operation":"status"}\n' > outputs/session-status.json
uv run robot-call session --url http://127.0.0.1:8767/mcp \
  --arguments outputs/session-status.json
```

Reuse connected, healthy sessions. Do not kill or restart a bridge holding enabled
arms. Changing source files does not update code already loaded into that process.

## Start the controller and recorder

For a **new controller only**, run this from the repository root in one terminal:

```bash
uv run --env-file .env robot-bridge --port 8767
```

Stop other camera viewers, then run the recorder in a second terminal:

```bash
uv run --env-file .env robot-record --output-root outputs/rollouts --port 8768
```

`robot-record` uses the [Rust camera service](rust-cameras.md) automatically.
Build the worker during installation. Each rollout records separate native-resolution
`left.mp4`, `top.mp4`, and `right.mp4` videos and returns newly delivered frames on
request. There is no backend-selection flag. FFmpeg is used only by standalone
setup/viewing tools and for inspecting or composing saved media.

Keep both terminals running throughout the task.
The controller starts disconnected and enables no motors until the agent starts
an authorized session.

The recorder starts **idle**: it opens no cameras and enables no motors. It creates
a fresh directory and starts capture when the agent calls `recording(start, text)`.
`recording(finish)` verifies neutral, finalizes that attempt's video, and releases camera streams while
leaving the recorder process and motor sessions running. No recorder restart or
manually prepared task prompt file is needed between tasks.
Between tasks, observations return live joint feedback without camera images;
completed recordings are left unchanged.

After a task, finish and review the recording before releasing motor sessions.
For this setup, the user grants standing authorization to release both arms in the
verified near-zero resting pose without asking again. Follow the canonical
[release checks](../src/agentic_robots/robot_agent.md), including fresh stationary
feedback and scene inspection immediately before release. Exact zero is unnecessary;
the existing neutral tolerances apply. Retaining powered hold for the next task is
also allowed. Release each session before stopping the controller; release disables
torque, not the external power supply. An interrupted task or stale neutral result
does not establish readiness to release.

Override the motor endpoint with `--upstream` when needed. During a recording, all
observations and actions go through the recorder; it owns the three camera streams.

## Initialize a Codex conversation

Open a normal local conversation in the Codex desktop app on this computer. Obtain
its conversation ID from the app or ask it: “Tell me your `CODEX_THREAD_ID`, then
wait.” Use that conversation ID, which identifies the exact task, not a shared
root/session ID from a fork. Wait until the conversation is idle, then run:

```bash
uv run robot-init --thread-id YOUR_CONVERSATION_ID
```

The command checks recorder availability, sends the
[canonical setup prompt](../src/agentic_robots/robot_agent.md) plus the repository path and
endpoint to that existing conversation, and waits for an exact acknowledgment.
It then binds that conversation to the recorder's continuation watcher. The watcher
uses the same desktop conversation and sends no robot actions. It only continues an
idle, normally completed turn whose task lacks a final reviewed outcome; active,
interrupted, failed, and explicitly paused turns are left alone. A controller `stop`
also suppresses continuation until the agent explicitly executes again. Status exposes
adapter errors. Keep Codex and the recorder open; this is not an independent LLM or
a guarantee of recovery if either application is unavailable.
The agent replies `Robot ready [init:…]` and waits. Initialization does not start
capture, enable motors, or move the arms. Its receipt, including the exact prompt,
acknowledgment, and turn ID, is saved in `outputs/robot-init/<conversation-id>.json`.
The agent also writes a unique acknowledgment file there; when desktop readback
omits message text, the initializer checks this file and a new completed turn.
After updating the agent instructions, initialize an idle conversation again to
load them; the hardware processes do not need a restart.

If new motor sessions are needed and the arms are physically supported, add
`--startup-supported` to authorize their later startup. This flag enables no motors
during initialization. Existing sessions need no new startup authorization. The
initializer does not authorize communication-timeout or motor-protection resets.

This delivers a setup message through the app's `send_message_to_thread` tool; it
does not replace Codex's built-in system instructions. The message remains in the
conversation context for later tasks. You do not need to paste robot instructions
or manually refresh its tools. The agent uses native MCP tools when configured for
the supplied endpoint, or the same tools through `uv run robot-call`.

The desktop adapter reuses the installed `codex-app-tools` MCP plugin. It uses
`CODEX_APP_TOOLS_PIPE_PATH` when available, otherwise discovers the app-tools socket
by its tool catalog. This integration depends on the desktop installation and its
bundled plugin; it is not a stable public session-injection API. It has been tested
on the Linux desktop installation used for this repository. It does not fall back
to `codex exec resume`, which would run a separate CLI session. If discovery fails,
`--app-pipe /path/to/socket --app-tools /path/to/codex-app-tools` supplies the local
installation explicitly. The app must remain open. Remote/cloud conversations and
busy target conversations are rejected. Codex subagents use the collaboration
messaging channel for initialization and continuation; the desktop adapter cannot
send messages to them by conversation ID. Their parent applies the same initialization
prompt and observes the recorder's task state. The automatic desktop watcher applies
to ordinary initialized app conversations.

If acknowledgment times out, inspect the conversation and receipt before retrying.
The initializer sends only one message and does not automatically resend after an
uncertain delivery. Client permissions still apply; initialization does not change
Codex's sandbox or approval settings.

## Send a task

In that initialized conversation, send:

> Close the grippers and draw a heart shape with both arms.

The agent automatically starts recording with the task text, then repeats:

1. Observe camera images and useful joint feedback.
2. Choose a goal or correction, numerical targets, and durations; report concise intent.
3. Execute the action and inspect the result and subsequent observations.
4. Evaluate progress and recover in place while useful corrections remain available.

Returning both arms to neutral (six zero joint targets per arm) is part of every
task, including the example above which does not request a return. The agent verifies
fresh measured joints and images after motion ends, then finishes and reviews the video.

Every recorder action response contains `post_action` images and measured feedback,
each connected arm's base-frame `ee_pose` at `grasp_site`, `gripper` state, and concise
`diagnostics`. This is automatic for completed, rejected, and stopped commands.
The recorder waits up to two seconds for newly published camera frames after the
motor response. Unavailable images or valid measured poses are reported explicitly
in `post_action.errors`; the original action outcome is preserved. Native MCP returns
the images inline, while `robot-call` exposes their absolute paths. Separate `observe`
and `session(status)` requests remain available whenever further inspection helps.
Measured FK is model-based, not calibrated object/world localization. An interrupted
jaw action does not become a completed closure through recovery; inspect the requested
and measured opening before assuming the grasp is secure.

The recorder verifies both arms before normal finalization; it never executes a hidden
return routine. The agent chooses whether a last-resort neutral reset could help
unfinished work. It records a review with success, a correction for a retry, or evidence
of a specific blocker. Recoverable return faults require inspection and correction;
they do not themselves justify giving up. Physical inability to return is recorded
separately as `needs_intervention`, preserving available hold and video without claiming
neutral. A raw encoder shutdown or interrupted recording never establishes neutral. Send
another task in the same conversation to create another recording. The output root
contains a unique timestamp/ID directory for every attempt. Previous attempts remain
available for comparison. Task evaluation is done by this same Codex conversation.

The agent chooses action sizes and timing. EE targets use each arm's base frame;
there is no calibrated shared world frame. EE commands specify endpoints and
interpolate in joint space. The collision model excludes the table and other arm.
See the [action reference](../README.md#agent-observationaction-loop) for fields,
checks, and feedback. `completed` only means execution finished; observations and
actual feedback determine whether the task succeeded. Rejections provide correction
guidance and keep the process alive. For a tracking fault marked `recoverable`, the
agent inspects the failure, calls `session recover`, then observes and chooses a
corrected action. Recovery verifies powered hold before clearing the software latch;
other latched faults stay blocked. See the action reference for recovery feedback.

The project's [.codex/config.toml](../.codex/config.toml) configures optional native
MCP tools. The initializer supplies a CLI command with the correct endpoint, so an
existing conversation can work even when its tool catalog cannot reload. CLI results
contain absolute image paths for the agent to inspect; native MCP returns image
blocks. Numerical action parameters are JSON arguments, not command-line flags.
All command-line configuration uses tyro `Args` dataclasses in the corresponding
files under `scripts/`.

## Recordings and failures

The recorder's additional tool accepts:

```json
{"operation":"start","text":"The full task message"}
{"operation":"status"}
{"operation":"note","text":"Trace the upper lobes"}
{"operation":"return","text":"Task completed; returning both arms"}
{"operation":"finish"}
{"operation":"review","review":{"outcome":"success","summary":"Observed task result","evidence":["top.mp4 at 00:18 and final joint observation"]}}
```

`start` refuses to replace an active or unreviewed attempt. `finish` checks two fresh
readings 150 ms apart: both arms within 3° of zero, joint speeds at most 0.05 rad/s,
no latched faults, and no action in flight. These are completion tolerances, not new
motion limits. Rejections include measured feedback, tolerances, and per-arm reasons.
`review` persists the agent's decision in `manifest.json`; a retry requires a `correction`,
and a blocked decision requires a `constraint`. Evidence is the agent's assessment;
the harness does not independently judge the video or prove task impossibility.

New task motion requires a ready recording with all three cameras available. `return`
declares intent without moving and permits agent-chosen return actions with best-effort
logging if recording fails. Controller checks still apply. Status, recovery, and explicit
controller stop remain available on logging failures. An exceptional review with outcome
`needs_intervention` requires a constraint, evidence, and `intervention` of
`control_unavailable` or `physical_obstruction`; a software tracking latch alone is not
accepted as unavailable control. Outcome `paused` records an explicit user stop.
Finalizing files during process cleanup is separate from completing a task.
No recorder operation releases arm torque.

Each directory contains native-resolution `left.mp4`, `top.mp4`, `right.mp4`,
`events.jsonl`, `observations/`, `manifest.json`, `prompt.txt`, and capture logs.
Each camera has a `*-worker.log` file. Videos include pauses between actions and
preserve the camera's input cadence without overlays or resizing; dropped frames
are acceptable. Events include tool requests/results, immutable observations, and
telemetry. Check the final manifest before claiming a complete recording. Cameras
are not hardware synchronized; video is not an independent measurement of Cartesian accuracy.

The agent should inspect the recorded movement and state whether it reviewed video
or sampled frames. It should report uncertainty rather than infer success from a
planned trajectory. A failure does not justify killing the motor bridge. `session
stop` interrupts motion while retaining hold; only an explicitly authorized,
supported `session release` removes torque. Closing a client is not a motion stop.

## Tests

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

To defer end-to-end workflows, use `uv run pytest -m 'not e2e'`.

The default suite uses mocks or simulated arms and a fake desktop. HTTP subprocesses
forbid CAN sockets; video tests use real Rust workers and synthetic camera streams. Tests
cover initialization acknowledgment and failure, idle recording, repeated tasks,
rejection/correction, tracking-fault recovery, neutral/review gates, an early-ended fake
agent resuming after a return fault, persistent hold, and final MP4 generation.

To test the real desktop/LLM round trip, open an **idle disposable local conversation**
and run the following. It sends visible messages and consumes Codex usage. This test
still uses simulated arms, synthetic video, and temporary HTTP endpoints; it never
starts the real hardware bridge. Reserve the conversation for the test until it ends.

```bash
ROBOT_CODEX_E2E_THREAD_ID=YOUR_TEST_CONVERSATION_ID \
  uv run pytest -q -s tests/test_robot_agent_e2e.py
```

The test discovers the desktop app from an ordinary terminal, runs the initializer
CLI, checks that initialization caused no recording or motion, sends two tasks in
successive turns, and verifies both recordings, actions,
observations, neutral joints, closed jaws, and retained sessions. Neither task asks
for a neutral return; the test checks that initialization supplies this behavior and
that the agent observes both arms at neutral after motion and before finishing.
The first task injects one simulated tracking error and checks that the agent
observes, recovers, and chooses a corrected action before completing the task.
It is skipped in ordinary CI because CI has no signed-in desktop app. Software and
synthetic video tests do not validate physical dynamics or visual robot accuracy.
