"""Three Rust camera workers behind the existing task and robot tools."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentic_robots.bridge import RobotError, failure
from agentic_robots.camera_worker import CameraWorker
from agentic_robots.cameras import auto_exposure
from agentic_robots.recording import ORDER, Rollout


class RustRollout(Rollout):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.workers = {}
        self.worker_status = {}
        self.manifest["camera_backend"] = "rust"
        self.manifest.pop("timestamps", None)  # Legacy FFmpeg clock description.

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
            and len(self.worker_status) == 3
            and all(s.get("ready") for s in self.worker_status.values()),
        }

    def snapshots(self, after=None):
        # `after` serves the legacy backend. Rust always waits for a new frame.
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
