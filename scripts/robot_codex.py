"""Run a fresh Codex CLI robot task with automatic observations before model requests."""

import asyncio
import json
import os
import secrets
import shlex
import shutil
import socket
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx2 as httpx
import tyro
import uvicorn
from mcp import Client

from agentic_robots.model_proxy import create_app
from scripts.robot_init import Args as InitArgs
from scripts.robot_init import bootstrap


@dataclass
class Args:
    task_file: Path
    """UTF-8 file containing the user's complete robot task."""
    workspace: Path
    """Empty working directory for this fresh agent's requests and analysis."""
    output: Path
    """New evidence directory for prompts, CLI events, and injected observations."""
    url: str = "http://127.0.0.1:8768/mcp"
    """Recorder endpoint; use the same endpoint for every robot call."""
    repo: Path = Path.cwd()
    """Robot repository, containing the synchronized Python environment."""
    codex: str = "codex"
    """Codex CLI executable, or its absolute path."""
    model: str = "gpt-6-astra"
    reasoning: str = "high"
    startup_supported: bool = False
    """Authorize supported startup; does not authorize protection resets."""


async def call(url, tool, arguments):
    async with Client(url, read_timeout_seconds=15) as client:
        result = await client.call_tool(tool, arguments)
        if result.is_error or result.structured_content is None:
            raise RuntimeError(f"Recorder {tool} failed: {result.structured_content}")
        return result.structured_content


async def turn(command, prompt, output, stage, environment):
    (output / f"{stage}-prompt.txt").write_text(prompt)
    with (
        (output / f"{stage}-events.jsonl").open("w") as log,
        (output / f"{stage}-stderr.log").open("w") as err,
    ):
        process = await asyncio.create_subprocess_exec(
            *command,
            "-o",
            str(output / f"{stage}-final.txt"),
            "-",
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=log,
            stderr=err,
        )
        try:
            await process.communicate(prompt.encode())
        finally:
            if process.returncode is None:
                process.terminate()  # CLI only; never a controller or motor session.
                await process.wait()
    events = [
        json.loads(line)
        for line in (output / f"{stage}-events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    if process.returncode or not any(e.get("type") == "turn.completed" for e in events):
        raise RuntimeError(
            f"Codex {stage} did not complete; inspect {output}. Motor sessions are unchanged."
        )
    return events


async def run(args):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("Set OPENAI_API_KEY, for example with uv run --env-file .env")
    executable = shutil.which(args.codex)
    if not executable:
        raise ValueError(f"Codex executable not found: {args.codex}")
    task = args.task_file.read_text().strip()
    if not task:
        raise ValueError("The task file is empty")
    repo, workspace, output = args.repo.resolve(), args.workspace.resolve(), args.output.resolve()
    if not (repo / "scripts/robot_call.py").is_file():
        raise ValueError("--repo must contain the robot tools")
    if output == workspace or workspace in output.parents:
        raise ValueError("Keep evidence outside the agent workspace")
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("Use an empty workspace for this fresh agent")
    if output.exists():
        raise ValueError("Use a new output directory to preserve previous evidence")
    status = await call(args.url, "recording", {"operation": "status"})
    if status["status"] not in {"idle", "finished", "failed"} or status.get("task", {}).get(
        "phase"
    ) not in {
        None,
        "success",
        "blocked",
        "needs_intervention",
        "paused",
        "retry",
    }:
        raise ValueError("Finish and review the existing recording before launching a fresh task")
    workspace.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True)
    token = secrets.token_urlsafe(32)
    environment = os.environ.copy()
    for name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_THREAD_ID",
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
    ):
        environment.pop(name, None)
    environment["ROBOT_MODEL_TOKEN"] = token  # Local relay credential, never the OpenAI key.
    environment["UV_CACHE_DIR"] = str(workspace / ".uv-cache")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    async def observe():
        return await call(args.url, "observe", {})

    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20), trust_env=False) as client:
        app = create_app(
            observe=observe,
            client=client,
            upstream="https://api.openai.com/v1",
            api_key=key,
            token=token,
            audit_dir=output / "observations",
        )
        app.state.inject = False  # Initialization must not start capture or enable hardware.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                while not server.started:
                    if serving.done():
                        await serving
                        raise RuntimeError("Local model adapter did not start")
                    await asyncio.sleep(0.01)
                config = [
                    "-c",
                    'model_provider="robot_observations"',
                    "-c",
                    'model_providers.robot_observations={name="Robot observations",'
                    f'base_url="http://127.0.0.1:{port}/v1",env_key="ROBOT_MODEL_TOKEN",'
                    'wire_api="responses",supports_websockets=false}',
                    "-c",
                    'forced_login_method="api"',
                    "-c",
                    f"model_reasoning_effort={json.dumps(args.reasoning)}",
                    "-c",
                    'approval_policy="never"',
                    "-c",
                    'sandbox_mode="workspace-write"',
                    "-c",
                    "sandbox_workspace_write.network_access=true",
                    "-c",
                    'shell_environment_policy.exclude=["OPENAI_API_KEY","CODEX_API_KEY","ROBOT_MODEL_TOKEN"]',
                ]
                base = [
                    executable,
                    "exec",
                    "--ignore-user-config",
                    "--skip-git-repo-check",
                    "-C",
                    str(workspace),
                    "-m",
                    args.model,
                    *config,
                    "--json",
                ]
                acknowledgment = f"Robot ready [init:{uuid.uuid4().hex}]"
                initial = bootstrap(
                    InitArgs(
                        thread_id="",
                        repo=repo,
                        url=args.url,
                        startup_supported=args.startup_supported,
                    ),
                    acknowledgment,
                )
                cli = shlex.join(
                    [
                        "uv",
                        "run",
                        "--directory",
                        str(repo),
                        "--no-sync",
                        "robot-call",
                        "--url",
                        args.url,
                    ]
                )
                initial += (
                    f"\nCLI execution transport: use {cli} for every robot tool. The environment "
                    "is synchronized; --no-sync avoids modifying it during hardware operation. "
                    f"Keep requests, results, and analysis in {workspace}. Do not edit the robot "
                    "repository, access credentials, restart services, or launch another agent. "
                    "Automatic model observations are enabled for the task after this "
                    "acknowledgment."
                )
                events = await turn(base, initial, output, "init", environment)
                if (output / "init-final.txt").read_text().strip() != acknowledgment:
                    raise RuntimeError(
                        "Initialization acknowledgment was not exact; no task started"
                    )
                thread = next(e["thread_id"] for e in events if e.get("type") == "thread.started")
                started = await call(args.url, "recording", {"operation": "start", "text": task})
                (output / "recording-start.json").write_text(json.dumps(started, indent=2) + "\n")
                if not started.get("ready"):
                    raise RuntimeError("Recording is not ready; no task sent to the agent")
                metadata = {
                    "thread_id": thread,
                    "model": args.model,
                    "reasoning": args.reasoning,
                    "workspace": str(workspace),
                    "recorder_url": args.url,
                    "recording": started["output"],
                    "automatic_observations": True,
                }
                (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
                app.state.inject = True
                print(json.dumps({"status": "running", **metadata}), flush=True)
                prompt = (
                    task + "\n\nThe launcher already started recording this exact task in "
                    f"{started['output']}. Reuse that recording. "
                    "Current measured state and camera images are automatically "
                    "attached before each model request during capture. Additional observe/status "
                    "calls are available. Inspect the injected observation before choosing "
                    "an action."
                )
                await turn(base + ["resume", thread], prompt, output, "task", environment)
                final = await call(args.url, "recording", {"operation": "status"})
                (output / "recording-final.json").write_text(json.dumps(final, indent=2) + "\n")
                return {
                    "status": "agent_completed",
                    "output": str(output),
                    "task": final.get("task"),
                }
            finally:
                server.should_exit = True
                await serving


def main():
    args = tyro.cli(Args, description=__doc__)
    try:
        print(json.dumps(asyncio.run(run(args))))
    except (Exception, KeyboardInterrupt) as exc:
        print(
            json.dumps(
                {"status": "error", "message": str(exc) or "Interrupted; inspect robot status"}
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
