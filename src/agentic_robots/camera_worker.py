"""Small, versioned JSON-lines boundary to one isolated Rust camera process."""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path


def worker_binary():
    override = os.environ.get("ROBOT_CAMERA_BINARY")
    candidate = override or shutil.which("robot-camera-service")
    if not candidate:
        candidate = (
            Path(__file__).resolve().parents[2]
            / "rust/camera-service/target/release/robot-camera-service"
        )
    path = Path(candidate).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(
            "Build the camera worker with cargo build --release --locked "
            "--manifest-path rust/camera-service/Cargo.toml, or set ROBOT_CAMERA_BINARY."
        )
    return str(path)


class CameraWorker:
    def __init__(self, role, config, directory):
        self.role = role
        self.process = self.log = None
        self.reader = None
        self.lock = threading.Lock()
        self.replies = queue.Queue()
        self.request_id = 0
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        config = {
            "type": config.get("type", "usb"),
            **{k: config[k] for k in ("device", "format", "width", "height", "fps")},
        }
        self.config = {
            **config,
            "output": str(self.directory / f"{role}.mp4"),
        }

    def start(self, test=None):
        if self.process is not None:
            raise RuntimeError("Camera worker already started")
        config = {**self.config, **({"test": test} if test is not None else {})}
        path = self.directory / f"{self.role}-worker.json"
        path.write_text(json.dumps(config) + "\n")
        self.log = (self.directory / f"{self.role}-worker.log").open("w")
        try:
            self.process = subprocess.Popen(
                [worker_binary(), str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.log,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            hello = self.replies.get(timeout=5)
            if not isinstance(hello, dict) or hello.get("protocol_version") != 1:
                raise RuntimeError(f"Camera worker failed to initialize: {hello}")
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.replies.put(json.loads(line))
                except ValueError:
                    self.replies.put(RuntimeError("Invalid camera protocol response"))
        finally:
            self.replies.put(RuntimeError("Camera worker exited; inspect its log"))

    def call(self, operation, timeout=3, **arguments):
        deadline = time.monotonic() + timeout
        if not self.lock.acquire(timeout=timeout):
            raise TimeoutError("Camera request queue timeout")
        try:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError("Camera worker is not running")
            self.request_id += 1
            request_id = self.request_id
            request = dict(id=request_id, operation=operation, **arguments)
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Camera response timeout")
                try:
                    result = self.replies.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError("Camera response timeout") from exc
                if isinstance(result, Exception):
                    raise result
                if result.get("id", -1) < request_id:
                    continue  # A previous timed-out request may finish later.
                if result.get("id") != request_id:
                    raise RuntimeError("Camera response ID mismatch")
                if not result.get("ok"):
                    raise RuntimeError(result.get("error", "Camera request failed"))
                return result["result"]
        finally:
            self.lock.release()

    def snapshot(self, directory):
        path = Path(directory).resolve() / f"{self.role}.png"
        return self.call("snapshot", path=str(path))

    def close(self):
        result = None
        try:
            if self.process is not None and self.process.poll() is None:
                result = self.call("stop", timeout=8)
        finally:
            if self.process is not None:
                if self.process.stdin:
                    self.process.stdin.close()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
                if self.reader:
                    self.reader.join(timeout=1)
                if self.process.stdout:
                    self.process.stdout.close()
            if self.log:
                self.log.close()
        return result
