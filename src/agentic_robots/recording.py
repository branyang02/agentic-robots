"""Record camera video and tool events while forwarding actions to a separate controller."""

import asyncio
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp import Client

from agentic_robots.bridge import RobotError, failure, json_ready, write_result
from agentic_robots.camera_worker import CameraWorker
from agentic_robots.cameras import auto_exposure, configured_cameras
from agentic_robots.feedback import ActionFeedback
from agentic_robots.task import TERMINAL, Review, control_unavailable, neutral_feedback

ORDER = ("left", "top", "right")


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
        self.workers = {}
        self.worker_status = {}
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

    def _parallel(self, function):
        def one(item):
            role, worker = item
            try:
                return role, function(worker), None
            except Exception as exc:
                return role, None, str(exc)

        with ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(one, tuple(self.workers.items())))

    def start(self):
        try:
            devices = [
                str(Path(c["device"]).resolve())
                for c in self.cameras.values()
                if c["format"] != "synthetic"
            ]
            if len(devices) != len(set(devices)):
                raise ValueError("Each camera must use a different device")
            for role in ORDER:
                camera = self.cameras[role]
                if camera["format"] == "mjpeg":
                    auto_exposure(camera)
                self.workers[role] = CameraWorker(role, camera, self.output)
            starts = self._parallel(lambda w: w.start(test=self.cameras[w.role].get("test")))
            errors = {role: error for role, _, error in starts if error}
            if errors:
                raise RuntimeError(f"Camera startup failed: {errors}")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status = self.status()
                if status["errors"]:
                    raise RuntimeError(f"Camera startup failed: {status['errors']}")
                if all(s.get("ready") for s in self.worker_status.values()):
                    break
                time.sleep(0.02)
            else:
                raise TimeoutError("Timed out waiting for cameras")
            self.state = "recording"
            self.event("recording_started")
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
        if self.state in {"starting", "recording"}:
            self.worker_status = {
                role: result if error is None else {"capture_error": error, "ready": False}
                for role, result, error in self._parallel(lambda w: w.call("status"))
            }
        errors = {
            role: s.get("capture_error") or s.get("recording_error")
            for role, s in self.worker_status.items()
            if s.get("capture_error") or s.get("recording_error")
        }
        return {
            "status": self.state,
            "output": str(self.output),
            "error": self.error,
            "errors": errors,
            "videos": self.manifest.get("native_videos", {}),
            "in_flight": self.in_flight,
            "task": dict(self.manifest["task"]),
            # Encoder failure does not prevent requesting images or returning the arms.
            "ready": self.state == "recording"
            and self.error is None
            and len(self.worker_status) == 3
            and all(s.get("ready") for s in self.worker_status.values()),
        }

    def snapshots(self):
        with self.activity:
            if self.state != "recording":
                return {}, {"recording": "Start recording for images"}
            directory = self.output / "observations" / uuid.uuid4().hex
            directory.mkdir(parents=True)
            images, errors = {}, {}
            for role, result, error in self._parallel(lambda w: w.snapshot(directory)):
                if error:
                    errors[f"camera:{role}"] = error
                else:
                    images[role] = result
            return images, errors

    def finish(self):
        with self.activity:
            if self.in_flight:
                return failure(RobotError("actions_in_flight", "Wait for active actions"))
            if self.state in {"finished", "finishing", "failed"}:
                return self.status()
            self.state = "finishing"
        self.stop_poll.set()
        if self.poll_thread:
            self.poll_thread.join(timeout=5)
        videos, errors = {}, {}
        for role, result, error in self._parallel(lambda w: w.close()):
            error = (
                error
                or (result or {}).get("capture_error")
                or (result or {}).get("recording_error")
            )
            path = self.output / f"{role}.mp4"
            if path.exists() and path.stat().st_size:
                videos[role] = str(path)
            else:
                error = error or "Video file missing or empty"
            if error:
                errors[role] = error
        self.manifest["native_videos"] = videos
        if errors:
            self.error = self.error or f"Camera recording failed: {errors}"
        self.state = "failed" if self.error else "finished"
        self.manifest.update(duration_s=time.time() - self.epoch, stopped_unix=time.time())
        self.save_manifest()
        self.event("recording_finished", state=self.state, error=self.error)
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
            try:
                observation = self.observe()
            except Exception as exc:
                observation = {"images": {}, "arms": {}, "errors": {"observation": str(exc)}}
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

    def observe(self):
        r = self.rollout
        # Obtain potentially slow feedback/status before requesting the newest images.
        state = self.forward("session", {"operation": "status"})
        recording = self.status()
        try:
            images, errors = (
                r.snapshots() if r else ({}, {"recording": "Start recording for images"})
            )
        except Exception as exc:
            images, errors = {}, {"cameras": str(exc)}
        observation = {
            "images": images,
            "arms": state.get("arms", {}),
            "faults": state.get("faults", {}),
            "errors": {**state.get("errors", {}), **errors},
            "recording": recording,
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
