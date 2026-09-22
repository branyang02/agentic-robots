"""Shared native-camera runtime for synthetic tests only."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def rust_binary():
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
    subprocess.run(
        [
            cargo,
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            "rust/camera-service/Cargo.toml",
        ],
        check=True,
        timeout=180,
    )
    return str(Path("rust/camera-service/target/release/robot-camera-service").resolve())


@pytest.fixture
def camera_runtime(monkeypatch, rust_binary):
    monkeypatch.setenv("ROBOT_CAMERA_BINARY", rust_binary)
    monkeypatch.setenv("ROBOT_CAMERA_TEST_ONLY", "1")
