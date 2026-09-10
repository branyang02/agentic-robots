"""CLI compatibility checks without opening cameras or touching CAN."""

import sys

import pytest
import tyro

from scripts import calibrate, cameras, setup_can, view_cameras


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("CLI parsing attempted hardware access")

    for module, name in (
        (calibrate, "inventory"),
        (setup_can, "inventory"),
        (cameras, "discover"),
        (cameras, "configured_cameras"),
        (view_cameras, "require_display"),
    ):
        monkeypatch.setattr(module, name, forbidden)


@pytest.mark.parametrize("module", [calibrate, cameras, setup_can, view_cameras])
@pytest.mark.parametrize("argv,code", [(["--help"], 0), (["--unknown"], 2)])
def test_help_and_unknown_options_exit_before_hardware(module, argv, code, monkeypatch):
    monkeypatch.setattr(sys, "argv", [module.__name__, *argv])
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == code


@pytest.mark.parametrize("module", [calibrate, cameras, setup_can])
@pytest.mark.parametrize("argv", [[], ["invalid"]])
def test_required_positional_choices(module, argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", [module.__name__, *argv])
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 2


@pytest.mark.parametrize(
    "module,field,choices",
    [
        (calibrate, "side", ["left", "right"]),
        (cameras, "command", ["list", "preview", "check"]),
        (setup_can, "command", ["list", "setup"]),
    ],
)
def test_existing_positional_syntax(module, field, choices):
    for choice in choices:
        args = tyro.cli(module.Args, args=[choice])
        assert getattr(args, field) == choice
