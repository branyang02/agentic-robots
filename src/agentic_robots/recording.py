"""Capture task video and tool events while forwarding actions to a separate controller."""

import asyncio
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from mcp import Client
from PIL import Image

from agentic_robots.bridge import RobotError, failure, json_ready, write_result
from agentic_robots.cameras import auto_exposure, configured_cameras, input_args
from agentic_robots.feedback import ActionFeedback
from agentic_robots.task import TERMINAL, Review, control_unavailable, neutral_feedback

ORDER = ("left", "top", "right")


def capture_command(cameras, output, epoch):
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-y",
        "-copyts",
        "-filter_complex_threads",
        "1",
    ]
    filters = []
    for i, role in enumerate(ORDER):
        camera = cameras[role]
        if camera["format"] == "lavfi":  # Synthetic inputs for tests; never open V4L2.
            command += ["-re", "-f", "lavfi", "-i", camera["device"]]
            origin = "STARTPTS"
        else:
            command += ["-thread_queue_size", "64", "-timestamps", "abs", *input_args(camera)]
            origin = f"{epoch:.6f}/TB"
        filters += [
            f"[{i}:v]setpts=PTS-{origin},"
            f"scale=640:480:force_original_aspect_ratio=decrease,setsar=1,split[v{i}][p{i}]",
            f"[v{i}]pad=640:480:(ow-iw)/2:(oh-ih)/2,"
            f"drawtext=text='{role.upper()}':x=12:y=10:fontsize=24:fontcolor=white:"
            f"box=1:boxcolor=black@0.65[panel{i}]",
            f"[p{i}]fps=5[preview{i}]",
        ]
    filters += [
        "[panel0][panel1][panel2]hstack=inputs=3:shortest=1,fps=30,"
        "pad=iw:ih+36:0:0,"
        "drawtext=text='t=%{pts\\:hms}':x=12:y=h-28:fontsize=20:fontcolor=white,"
        "drawtext=textfile=phase.txt:reload=1:expansion=none:"
        "x=260:y=h-28:fontsize=20:fontcolor=white[video]"
    ]
    command += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[video]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-threads",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-flush_packets",
        "1",
        str(output / "capture.mkv"),
    ]
    for i, role in enumerate(ORDER):
        command += [
            "-map",
            f"[preview{i}]",
            "-c:v",
            "png",
            "-threads",
            "1",
            "-compression_level",
            "1",
            "-f",
            "image2",
            "-update",
            "1",
            "-atomic_writing",
            "1",
            str(output / f"{role}.png"),
        ]
    return command


def upstream_call(url, tool, arguments):
    async def call():
        timeout = 3 if tool == "session" and arguments.get("operation") == "status" else 3600
        async with Client(url, read_timeout_seconds=timeout) as client:
            result = await client.call_tool(tool, arguments)
            if result.structured_content is None:
                raise RuntimeError(f"Upstream returned no structured result for {tool}")
            return result.structured_content

    return asyncio.run(call())


class Rollout:
    def __init__(self, output, prompt, cameras, upstream):
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.upstream, self.cameras = upstream, cameras
        self.epoch, self.started = time.time(), time.monotonic()
        self.state, self.error = "starting", None
        self.process = self.capture_log = None
        self.lock, self.activity = threading.Lock(), threading.Lock()
        self.in_flight = 0
        self.stop_poll = threading.Event()
        self.poll_thread = None
        self.manifest = {
            "task": {"phase": "active"},
            "prompt": prompt,
            "started_unix": self.epoch,
            "upstream": upstream,
            "camera_order": ORDER,
            "cameras": cameras,
            "telemetry_requested_hz": 5,
            "timestamps": "Video t and event elapsed_s use the same software wall-clock origin. "
            "Camera timestamps are converted by V4L2; cameras are not hardware synchronized. "
            "Snapshot timestamps are file publication times, not sensor exposure times.",
        }
        (self.output / "prompt.txt").write_text(prompt + "\n")
        self.note(prompt)
        self.save_manifest()

    def event(self, kind, **data):
        with self.lock:
            if self.state in {"finished", "failed"} and kind != "recording_finished":
                return  # Completed task files are closed to subsequent/late client calls.
            record = json_ready(
                {
                    "kind": kind,
                    "unix": time.time(),
                    "elapsed_s": time.time() - self.epoch,
                    "monotonic_elapsed_s": time.monotonic() - self.started,
                    **data,
                }
            )
            try:
                with (self.output / "events.jsonl").open("a") as stream:
                    stream.write(json.dumps(record, allow_nan=False) + "\n")
            except OSError as exc:
                self.error = f"Event log write failed: {exc}"
                raise
        return record

    def save_manifest(self):
        self.manifest.update(state=self.state, error=self.error)
        write_result(self.output / "manifest.json", self.manifest)

    def note(self, text):
        self.event("note", text=text)
        temporary = self.output / f"phase-{uuid.uuid4().hex}.tmp"
        temporary.write_text(" ".join(text.split())[:150])
        temporary.replace(self.output / "phase.txt")

    def start(self):
        try:
            devices = [
                str(Path(c["device"]).resolve())
                for c in self.cameras.values()
                if c["format"] != "lavfi"
            ]
            if len(set(devices)) != len(devices):
                raise ValueError("Each camera role must use a different device")
            for camera in self.cameras.values():
                if camera["format"] == "mjpeg":
                    auto_exposure(camera)
            self.capture_log = (self.output / "ffmpeg.log").open("w")
            self.process = subprocess.Popen(
                capture_command(self.cameras, self.output, self.epoch),
                cwd=self.output,
                stdout=subprocess.DEVNULL,
                stderr=self.capture_log,
                start_new_session=True,
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Camera capture exited; inspect ffmpeg.log")
                if all((self.output / f"{role}.png").exists() for role in ORDER):
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("Timed out waiting for all three camera streams")
            self.state = "recording"
            self.event("recording_started", ffmpeg_pid=self.process.pid)
            self.save_manifest()
            self.poll_thread = threading.Thread(target=self.poll, daemon=True)
            self.poll_thread.start()
        except Exception as exc:
            self.error = str(exc)
            self.finish()
            raise

    def poll(self):
        while not self.stop_poll.is_set():
            start = time.monotonic()
            try:
                state = upstream_call(self.upstream, "session", {"operation": "status"})
                self.event("telemetry", state=state)
            except Exception as exc:
                try:
                    self.event("telemetry_error", message=str(exc))
                except OSError:
                    return
            self.stop_poll.wait(max(0, 0.2 - (time.monotonic() - start)))

    def status(self):
        ages = {}
        for role in ORDER:
            path = self.output / f"{role}.png"
            ages[role] = time.time() - path.stat().st_mtime if path.exists() else None
        alive = self.process is not None and self.process.poll() is None
        return {
            "status": self.state,
            "output": str(self.output),
            "error": self.error,
            "capture_alive": alive,
            "preview_age_s": ages,
            "in_flight": self.in_flight,
            "task": dict(self.manifest["task"]),
            "ready": self.state == "recording"
            and alive
            and self.error is None
            and all(age is not None and 0 <= age < 2 for age in ages.values()),
        }

    def snapshots(self, after=None):
        with self.activity:  # Finish must wait for any snapshot files being published.
            if self.state != "recording":
                return {}, {"recording": "Start recording for images"}
            images, errors = {}, {}
            # Publication timestamps are software timestamps, not sensor exposure times.
            # For action responses, never silently substitute a pre-action preview.
            deadline = time.monotonic() + 2
            if after is not None:
                while time.monotonic() < deadline:
                    if all(
                        (self.output / f"{role}.png").exists()
                        and (self.output / f"{role}.png").stat().st_mtime >= after
                        for role in ORDER
                    ):
                        break
                    if self.process is None or self.process.poll() is not None:
                        break
                    time.sleep(0.02)
            directory = self.output / "observations" / uuid.uuid4().hex
            directory.mkdir(parents=True)
            for role in ORDER:
                try:
                    # Open pins the inode while FFmpeg atomically publishes the next frame.
                    with (self.output / f"{role}.png").open("rb") as source:
                        published = os.fstat(source.fileno()).st_mtime
                        if after is not None and (
                            published < after or not 0 <= time.time() - published < 2
                        ):
                            raise ValueError("No fresh frame published after action response")
                        path = directory / f"{role}.png"
                        path.write_bytes(source.read())
                    with Image.open(path) as frame:
                        frame.load()
                        images[role] = {
                            "path": str(path),
                            "width": frame.width,
                            "height": frame.height,
                            "published_unix": published,
                            "age_s": time.time() - published,
                        }
                except (OSError, ValueError) as exc:
                    errors[f"camera:{role}"] = str(exc)
            return images, errors

    def finish(self):
        with self.activity:
            if self.in_flight:
                return failure(
                    RobotError(
                        "actions_in_flight", "Wait for active actions before finishing recording"
                    )
                )
            if self.state in {"finished", "finishing", "failed"}:
                return self.status()
            if self.state == "recording" and self.process.poll() is not None:
                self.error = self.error or "Camera capture ended before finish was requested"
            self.state = "finishing"
        self.stop_poll.set()
        if self.poll_thread:
            self.poll_thread.join(timeout=5)
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)  # Only our camera encoder, never the robot.
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                self.error = "Camera encoder needed forced termination; video may be incomplete"
        if self.capture_log:
            self.capture_log.close()
        try:
            if not self.process or self.process.returncode not in (0, 255):
                raise RuntimeError("Camera capture failed; inspect ffmpeg.log")
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-copyts",
                    "-i",
                    str(self.output / "capture.mkv"),
                    "-map",
                    "0:v:0",
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(self.output / "rollout.mp4"),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    str(self.output / "rollout.mp4"),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.manifest["video"] = json.loads(probe.stdout)
            self.state = "failed" if self.error else "finished"
        except Exception as exc:
            self.state, self.error = "failed", str(exc)
        self.manifest["finished_unix"] = time.time()
        self.event("recording_finished", state=self.state, error=self.error)
        self.save_manifest()
        return self.status()


class RecordingBridge:
    def __init__(
        self,
        rollout=None,
        *,
        output_root=Path("outputs/rollouts"),
        cameras=None,
        upstream="http://127.0.0.1:8767/mcp",
    ):
        self.rollout = rollout
        self.output_root = Path(output_root).resolve()
        self.cameras = cameras or configured_cameras
        self.upstream = rollout.upstream if rollout else upstream
        self.lifecycle = threading.Lock()
        self.binding = None
        self.watch_error = None
        self.action_feedback = ActionFeedback()

    def status(self):
        result = self.rollout.status() if self.rollout else {"status": "idle", "ready": False}
        if self.binding:
            result["agent"] = {**self.binding, "error": self.watch_error}
        return result

    def recording(self, operation, text="", review=None, thread_id="", turn_id=""):
        # Serialize starts/finishes; status and controller stop remain available during I/O.
        if operation == "status":
            return self.status()
        if not self.lifecycle.acquire(blocking=False):
            return failure(RobotError("recording_busy", "A recording is starting or finishing"))
        try:
            r = self.rollout
            task = r.manifest["task"] if r else {}
            if operation == "start":
                if not text.strip():
                    raise RobotError("task_required", "Pass the user's task prompt in text")
                if r and r.state not in {"finished", "failed"}:
                    raise RobotError(
                        "recording_active",
                        "Finish the current recording first",
                        recording=r.status(),
                    )
                capture_start_failed = (
                    r
                    and r.state == "failed"
                    and not task.get("motion_started")
                    and task["phase"] == "active"
                )
                if r and task["phase"] not in TERMINAL | {"retry"} and not capture_start_failed:
                    raise RobotError(
                        "attempt_unreviewed",
                        "Finish and review the previous attempt first",
                        task=task,
                    )
                output = self.output_root / (
                    time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
                )
                self.rollout = Rollout(output, text, self.cameras(), self.upstream)
                if r and task["phase"] == "retry":
                    self.rollout.manifest["task"].update(
                        previous_attempt=str(r.output), correction=task["review"]["correction"]
                    )
                self.rollout.start()
            elif operation == "finish":
                if r:
                    if task["phase"] in TERMINAL | {"review", "retry"}:
                        return self.status()
                    with r.activity:
                        if r.in_flight:
                            raise RobotError("actions_in_flight", "Wait for active actions")
                        neutral = neutral_feedback(self.session("status"))
                        if neutral["verified"]:
                            time.sleep(0.15)
                            neutral = neutral_feedback(self.session("status"))
                        task["neutral"] = neutral
                        if not neutral["verified"]:
                            raise RobotError(
                                "neutral_unverified",
                                "Continue from measured state; recover/revise the return. "
                                "A failed return action does not end the task.",
                                **neutral,
                            )
                        # Block new actions before releasing the lock to finalize the video.
                        task["phase"] = "review"
                    return r.finish()
            elif operation == "return":
                if not r or task.get("phase") not in {"active", "returning", "review"}:
                    raise RobotError(
                        "no_active_attempt", "There is no active attempt to return from"
                    )
                if not text.strip():
                    raise RobotError(
                        "reason_required", "Explain completion or the last-resort reset"
                    )
                task.update(phase="returning", return_reason=text)
                try:
                    r.save_manifest()
                except OSError as exc:
                    # A failed disk must not trap powered arms away from neutral.
                    return {**self.status(), "recording_error": str(exc)}
            elif operation == "review":
                if not r:
                    raise RobotError("no_attempt", "Start an attempt first")
                decision = review if isinstance(review, Review) else Review.model_validate(review)
                if task["phase"] in TERMINAL | {"retry"}:
                    if task.get("review") == decision.model_dump():
                        return self.status()
                    raise RobotError("already_reviewed", "This attempt already has a decision")
                if decision.outcome in {"needs_intervention", "paused"}:
                    state = self.session("status")
                    if decision.intervention == "control_unavailable" and not control_unavailable(
                        state
                    ):
                        raise RobotError(
                            "control_still_available",
                            "A recoverable tracking fault alone does not justify abandoning return",
                            feedback=state,
                        )
                    with r.activity:
                        if r.in_flight:
                            raise RobotError(
                                "actions_in_flight", "Inspect/stop active motion first"
                            )
                        task.update(phase="review", last_feedback=state)
                    r.finish()  # Preserve the video even when physical return is impossible.
                elif task["phase"] != "review":
                    raise RobotError(
                        "review_not_ready", "Verify neutral and finish the video first"
                    )
                else:
                    neutral = neutral_feedback(self.session("status"))
                    if not neutral["verified"]:
                        raise RobotError(
                            "neutral_unverified", "Neutral is no longer verified", **neutral
                        )
                task.update(phase=decision.outcome, review=decision.model_dump())
                r.save_manifest()
            elif operation == "bind":
                if r and task["phase"] not in TERMINAL:
                    raise RobotError(
                        "task_active", "Finish the current task before binding an agent"
                    )
                uuid.UUID(thread_id)
                if not turn_id:
                    raise RobotError("turn_required", "Supply the acknowledged initialization turn")
                self.binding = {"thread_id": thread_id, "after_turn_id": turn_id}
                self.watch_error = None
            elif operation == "note":
                if not r or r.state != "recording":
                    raise RobotError(
                        "recording_not_started", "Start recording before adding a note"
                    )
                r.note(text)
            else:
                raise RobotError(
                    "invalid_operation", "Use start, status, note, return, finish, review, or bind"
                )
            return self.status()
        except Exception as exc:
            return failure(
                exc
                if isinstance(exc, RobotError)
                else RobotError("recording_error", str(exc), recording=self.status())
            )
        finally:
            self.lifecycle.release()

    def forward(self, tool, arguments):
        request_id = uuid.uuid4().hex
        r = self.rollout
        logging_error = None
        try:
            if r:
                try:
                    r.event("request", request_id=request_id, tool=tool, arguments=arguments)
                except OSError as exc:
                    returning = r.manifest["task"]["phase"] == "returning"
                    if not returning and (
                        tool != "session"
                        or arguments.get("operation") not in {"status", "stop", "recover"}
                    ):
                        raise
                    logging_error = str(exc)
            result = upstream_call(self.upstream, tool, arguments)
        except Exception as exc:
            result = failure(
                RobotError(
                    "recording_transport_error",
                    str(exc),
                    request_id=request_id,
                    action_outcome=(
                        "unknown if the request reached the controller; "
                        "inspect status before retrying"
                    ),
                ),
                arguments,
            )
            result["error"]["retryable"] = False
            result["error"]["next_step"] = (
                "Inspect controller status and the recording log; do not blindly replay."
            )
        try:
            if r:
                r.event("response", request_id=request_id, tool=tool, result=result)
        except OSError as exc:
            logging_error = str(exc)
        if logging_error:
            result["recording_error"] = logging_error  # Preserve the actual controller outcome.
        return result

    def execute(self, action):
        r = self.rollout
        if r:
            with r.activity:
                r.in_flight += 1  # Finish also waits for this action's observation.
        try:
            result = self._execute(action)
            completed = time.time()
            try:
                observation = self.observe(after=completed)
            except Exception as exc:
                observation = {"images": {}, "arms": {}, "errors": {"observation": str(exc)}}
            observation["action_response_unix"] = completed
            try:
                result = self.action_feedback.enrich(observation, action, result)
            except Exception as exc:
                observation.setdefault("errors", {})["feedback"] = str(exc)
                result = {
                    **result,
                    "post_action": observation,
                    "diagnostics": [
                        f"Execution {result.get('status', 'unknown')}; "
                        f"post-action feedback failed: {exc}"
                    ],
                }
            if r:
                try:
                    r.event("post_action", action=action.model_dump(), result=result)
                except OSError as exc:
                    result["diagnostics"].append(f"Post-action log unavailable: {exc}")
            return result
        finally:
            if r:
                with r.activity:
                    r.in_flight -= 1

    def _execute(self, action):
        r = self.rollout
        if r is None:
            return failure(
                RobotError(
                    "recording_not_started",
                    "Call recording(start, text=<user task>) before executing",
                ),
                action.model_dump(),
            )
        with r.activity:
            phase = r.manifest["task"]["phase"]
            if phase != "returning" and (phase != "active" or not r.status()["ready"]):
                result = failure(
                    RobotError(
                        "recording_unavailable",
                        "Recording is unavailable; no action forwarded. Restore recording, or "
                        "declare recording(return, text=<reason>) for an agent-planned return. "
                        "Return actions retain controller checks and use best-effort recording.",
                        recording=r.status(),
                    ),
                    action.model_dump(),
                )
                request_id = uuid.uuid4().hex
                try:
                    r.event(
                        "request",
                        request_id=request_id,
                        tool="execute",
                        arguments={"action": action.model_dump(exclude_none=True)},
                        forwarded=False,
                    )
                    r.event("response", request_id=request_id, tool="execute", result=result)
                except OSError as exc:
                    result["recording_error"] = str(exc)
                return result
            r.manifest["task"]["motion_started"] = True
            r.manifest["task"].pop("watch_paused", None)
        return self.forward("execute", {"action": action.model_dump(exclude_none=True)})

    def session(self, operation, arm=None, supported=False, reset_communication=False):
        if operation == "stop" and self.rollout:
            self.rollout.manifest["task"]["watch_paused"] = True
        return self.forward(
            "session",
            dict(
                operation=operation,
                arm=arm,
                supported=supported,
                reset_communication=reset_communication,
            ),
        )

    def observe(self, after=None):
        start = time.time()
        r = self.rollout
        try:
            images, errors = (
                r.snapshots(after=after) if r else ({}, {"recording": "Start recording for images"})
            )
        except Exception as exc:
            images, errors = {}, {"cameras": str(exc)}
        state = self.forward("session", {"operation": "status"})
        observation = {
            "capture_started_unix": start,
            "capture_finished_unix": time.time(),
            "images": images,
            "arms": state.get("arms", {}),
            "faults": state.get("faults", {}),
            "errors": {**state.get("errors", {}), **errors},
            "recording": self.status(),
            "frame": "arm base; no calibrated world transform",
        }
        if "error" in state:
            observation["errors"]["controller"] = state["error"]
        if r:
            try:
                r.event("observation", observation=observation)
            except OSError as exc:
                observation["errors"]["recording"] = str(exc)
        return observation
