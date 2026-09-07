"""The command surface is stable even while the stages are still stubs."""

from __future__ import annotations

import pytest

from asterwell_build.cli import COMMAND_HELP, COMMANDS, build_parser, main

# The pipeline's commands, in order. Spelled out here rather than imported so
# that a command silently disappearing from the module is a test failure.
EXPECTED_COMMANDS = [
    "fetch",
    "allowlist",
    "stars",
    "build",
    "qa",
    "specimen",
    "site",
    "package",
    "version",
]


def test_command_surface_is_the_expected_one() -> None:
    assert list(COMMAND_HELP) == EXPECTED_COMMANDS
    assert list(COMMANDS) == EXPECTED_COMMANDS


@pytest.mark.parametrize("name", EXPECTED_COMMANDS)
def test_every_command_is_registered_with_the_parser(name: str) -> None:
    args = build_parser().parse_args([name])
    assert args.command == name
    assert callable(args.handler)


def test_top_level_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in EXPECTED_COMMANDS:
        assert name in out, f"{name} missing from --help"


@pytest.mark.parametrize("name", EXPECTED_COMMANDS)
def test_every_command_has_its_own_help(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([name, "--help"])
    assert exc.value.code == 0
    assert f"asterwell-build {name}" in capsys.readouterr().out


def test_a_missing_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_an_unknown_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["polish"])
    assert exc.value.code == 2


def test_commands_whose_step_has_not_landed_raise_not_implemented() -> None:
    """Shrinks by itself: a command stops being a stub when its step lands."""
    stubs = [name for name, fn in COMMANDS.items() if getattr(fn, "is_stub", False)]
    for name in stubs:
        with pytest.raises(NotImplementedError, match=name):
            main([name])
