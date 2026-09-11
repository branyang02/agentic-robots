"""Check installed imports and commands without relying on the working directory."""

import subprocess
import sys


def test_installed_package_and_commands_outside_checkout(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-I",  # Ignore PYTHONPATH and the checkout; use the installed distribution.
            "-c",
            """
import importlib
import importlib.metadata
import importlib.resources
import pkgutil
import socket
import sys

accesses = []
def forbid_hardware(event, args):
    if event == "socket.__new__" and args[1] == getattr(socket, "AF_CAN", -1):
        accesses.append(event)
        raise AssertionError("Import/help attempted CAN access")
sys.addaudithook(forbid_hardware)
sys.argv = ["import-check", "--invalid-option"]
import agentic_robots
for module in pkgutil.iter_modules(agentic_robots.__path__):
    importlib.import_module("agentic_robots." + module.name)
assert not any(n == "scripts" or n.startswith("scripts.") for n in sys.modules)
prompt = importlib.resources.files("agentic_robots").joinpath("robot_agent.md").read_text()
assert "joint_target" in prompt and "recording" in prompt
commands = importlib.metadata.distribution("agentic-robots").entry_points
assert len(commands) == 8
for command in commands:
    assert command.group == "console_scripts"
    assert command.module.startswith("scripts.") and command.attr == "main"
    sys.argv = [command.name, "--help"]
    try:
        command.load()()
    except SystemExit as exc:
        assert exc.code == 0, (command.name, exc.code)
    else:
        raise AssertionError(command.name + " did not exit after help")
assert not accesses
print("Installed library, packaged prompt, and 8 CLI commands passed")
""",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "8 CLI commands passed" in result.stdout
