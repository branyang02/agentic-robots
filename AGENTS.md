# Development and robot operation

Keep the implementation minimal. The current Codex conversation supplies decisions;
do not add a second LLM, a task planner, or hidden motion routines to the bridge.

For robot operation, initialize the conversation with `robot-init` as described in
`docs/agentic-runs.md`. The canonical initialization prompt is
`scripts/robot_agent.md`; keep behavioral instructions there instead of duplicating
them across repository files. A task message in the initialized conversation starts
the observe / reason / execute / evaluate loop and its recording.

Never kill/restart a controller holding enabled arms or drop torque to install a
code update. Reuse healthy sessions. Physical startup support and release must be
authorized by the current user; existing authorization carries across turns.
This file does not establish physical readiness. Do not reset motor protection.

Tests must use simulated hardware or mocks, with HTTP test processes unable to open
CAN sockets. The opt-in Codex E2E test also uses simulated arms and synthetic video.
State whether validation used simulated or real hardware. Software tests do not
validate physical dynamics. Do not merge a PR without the user's authorization.
