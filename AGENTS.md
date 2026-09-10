# Robot agent loop

The agent is the current Codex conversation. The local MCP bridge contains no LLM,
task planner, or fixed movement routine. Use `observe`, `execute`, and `session`;
`robot-call` invokes those same tools when this turn cannot reload its tool catalog.
Read `docs/agentic-runs.md` for process setup, recording, and a starter task prompt.

The agent chooses targets, action sizes, durations, when observations are needed,
and when the task is complete. There is no fixed action budget or per-action human
approval. Show useful camera images and concise descriptions of the proposed action
and observed result. Camera failures and timestamps are information for the agent;
no camera count, brightness, age, or observation-ID gate blocks execution.

Actions use metres and radians. Arm actions preserve the last jaw target.
A separate `gripper_target` action takes `gripper_opening` (0 closed, 1 open) and
duration; it preserves the
arm's hold command. Grippers start passive until their first explicit jaw action.
Joint targets are absolute; joint and EE deltas start from measured state at
execution. EE targets use base-frame position and XYZW
quaternion; EE deltas support base/tool translation and rotation vectors. World
calibration is absent. Duration specifies linear joint interpolation, without size,
speed, acceleration, or temperature caps and without automatic time stretching.
The agent must choose appropriate timing. EE commands specify endpoints, not straight
Cartesian paths or fixed-tip trajectories. The model omits the table and the other
arm, so the agent assesses scene clearance and separates arms around shared goals.

`completed` means the command sequence finished. Compare returned actual joints,
error, and useful camera observations to decide whether the task succeeded.
`rejected` includes an error code, details, request, last feedback, retryability, and
next-step guidance. Use that feedback to revise an action; rejection does not kill
or require restarting the bridge. A `stopped` result with `fault_latched: true`
means a control failure blocks that arm. Inspect the reported failure; do not clear
motor protection faults or repeatedly retry a failed hardware session automatically.
An explicit session stop interrupts an action without latching a hardware fault.

Hardware sessions persist independently of the agent. Inspect session status and reuse
connected arms. For a new session, physical startup support and any communication-timeout
reset must be authorized by the current user; existing authorization carries across
turns. This file does not itself establish physical readiness. Motor protection resets
are not allowed. Use explicit session start; only explicit, user-authorized release
removes torque. Do not kill/restart a bridge owning enabled arms or drop torque to
install a code update. Return to neutral
when the task requests it; returning is an agent action, not a hidden executor routine.

For a recorded rollout, keep the motor bridge running and launch `robot-record`
separately. Send the run's observations, actions, and session calls through its MCP
endpoint (default port 8768). It owns the camera streams and returns shared snapshots;
do not open the same cameras through the original bridge during recording. Use the
`recording` tool to log concise phase/intent notes and inspect recording status.
The project config names this endpoint `robot_recording`; use its tools for the run.
Finish recording only after actions and final observations complete. Recorder
shutdown does not release the arms; preserve their powered hold. Calls made directly
to the upstream bridge are not included in the recorder's action log.

Keep the implementation minimal. Tests must use simulated hardware or mocks, with
HTTP tests unable to open CAN sockets. State clearly whether validation used simulated
or real hardware. Passing software tests does not validate physical dynamics.
