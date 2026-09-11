# Development and robot operation

Keep the implementation minimal. The current Codex conversation supplies decisions;
do not add a second LLM, a task planner, or hidden motion routines to the bridge.

Prefer uv for Python workflows: `uv run <command>` for project tools and tests,
`uv add` / `uv remove` for dependencies, and `uv sync` for environment setup.
Use the project's console commands, such as `uv run robot-init`, by default.
Only add `--no-sync` when there is a concrete need to skip environment synchronization.
Consult the [official uv docs](https://docs.astral.sh/uv/) for uv behavior and options.

For robot operation, initialize the conversation with `robot-init` as described in
`docs/agentic-runs.md`. The canonical initialization prompt is
`src/agentic_robots/robot_agent.md`; keep behavioral instructions there instead of
duplicating them across repository files. A task message in the initialized conversation starts
the observe / reason / execute / evaluate loop and its recording.

Never kill/restart a controller holding enabled arms. Reuse healthy sessions;
release through the session API before a controller changeover. The user grants
standing release authorization for this setup's verified near-zero resting pose,
under the checks in `src/agentic_robots/robot_agent.md`; no repeated approval is needed.
Outside that pose, physical support and user authorization are required. Startup
support must still be established; release authorization does not establish startup
readiness. Existing authorization carries across turns. Do not reset motor protection.

Tests must use simulated hardware or mocks, with HTTP test processes unable to open
CAN sockets. The opt-in Codex E2E test also uses simulated arms and synthetic video.
State whether validation used simulated or real hardware. Software tests do not
validate physical dynamics. Do not merge a PR without the user's authorization.
