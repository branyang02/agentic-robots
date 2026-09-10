"""Record cameras and the agent's MCP rollout without owning or restarting motors."""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from mcp import Client
from PIL import Image

from scripts.cameras import auto_exposure, configured_cameras, input_args
from scripts.robot_bridge import RobotError, failure, json_ready, write_result
from scripts.robot_mcp import make_server, structured

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
            "ready": self.state == "recording"
            and alive
            and self.error is None
            and all(age is not None and 0 <= age < 2 for age in ages.values()),
        }

    def snapshots(self):
        images, errors = {}, {}
        directory = self.output / "observations" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        for role in ORDER:
            try:
                # Open pins the inode while FFmpeg atomically publishes the next frame.
                with (self.output / f"{role}.png").open("rb") as source:
                    published = os.fstat(source.fileno()).st_mtime
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
    def __init__(self, rollout):
        self.rollout = rollout

    def forward(self, tool, arguments):
        request_id = uuid.uuid4().hex
        r = self.rollout
        try:
            r.event("request", request_id=request_id, tool=tool, arguments=arguments)
            result = upstream_call(r.upstream, tool, arguments)
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
            r.event("response", request_id=request_id, tool=tool, result=result)
        except OSError as exc:
            result["recording_error"] = str(exc)  # Preserve the actual controller outcome.
        return result

    def execute(self, action):
        r = self.rollout
        with r.activity:
            if not r.status()["ready"]:
                result = failure(
                    RobotError(
                        "recording_unavailable",
                        "Recording is unavailable; no action forwarded",
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
            r.in_flight += 1
        try:
            return self.forward("execute", {"action": action.model_dump(exclude_none=True)})
        finally:
            with r.activity:
                r.in_flight -= 1

    def session(self, operation, arm=None, supported=False, reset_communication=False):
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
        start = time.time()
        images, errors = self.rollout.snapshots()
        state = self.forward("session", {"operation": "status"})
        observation = {
            "capture_started_unix": start,
            "capture_finished_unix": time.time(),
            "images": images,
            "arms": state.get("arms", {}),
            "faults": state.get("faults", {}),
            "errors": {**state.get("errors", {}), **errors},
            "recording": self.rollout.status(),
            "frame": "arm base; no calibrated world transform",
        }
        if "error" in state:
            observation["errors"]["controller"] = state["error"]
        self.rollout.event("observation", observation=observation)
        return observation


def recording_server(rollout):
    server = make_server(RecordingBridge(rollout))

    @server.tool()
    async def recording(operation: Literal["status", "note", "finish"], text: str = ""):
        """Inspect recording, log a phase note, or finish video without releasing robot torque."""
        try:
            if operation == "note":
                await asyncio.to_thread(rollout.note, text)
            if operation == "finish":
                result = await asyncio.to_thread(rollout.finish)
                if result["status"] == "failed":
                    raise RuntimeError(rollout.error)
                return structured(result)
            return structured(rollout.status())
        except Exception as exc:
            return structured(
                failure(
                    RobotError(
                        "recording_error",
                        str(exc),
                        recording=rollout.status(),
                    )
                )
            )

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New rollout directory")
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:8767/mcp")
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    rollout = Rollout(
        args.output, args.prompt_file.read_text().strip(), configured_cameras(), args.upstream
    )
    try:
        rollout.start()
        recording_server(rollout).run(transport="streamable-http", host="127.0.0.1", port=args.port)
    finally:
        rollout.finish()


if __name__ == "__main__":
    main()
