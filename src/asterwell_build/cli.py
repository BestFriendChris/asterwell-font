"""Command-line entry point: ``asterwell-build <command>``.

Every stage of the pipeline is registered here from the start so that the
command surface — and the mise tasks that call it — is stable while the
stages are implemented one at a time. A stage that has not landed yet is
bound to a stub that raises :class:`NotImplementedError`; implementing it
means replacing that binding in :data:`COMMANDS`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence

from asterwell_build import allowlist, assemble, qa, specimen, stars, upstream

# Ordered command surface: name -> one-line help. The order is the pipeline
# order and is what ``asterwell-build --help`` lists.
COMMAND_HELP: dict[str, str] = {
    "fetch": "Download and verify the pinned upstream archives into build/upstream",
    "allowlist": "Regenerate sources/allowlist.tsv from the pinned upstream fonts",
    "stars": "Build the six-petal star family from sources/stars.toml",
    "build": "Assemble the family into fonts/variable, fonts/ttf and fonts/webfonts",
    "qa": "Check coverage, metrics, names, shaping, licensing and fontbakery",
    "specimen": "Render fonts/specimen/index.html",
    "package": "Write fonts/dist: release zip, SHA256SUMS.txt and RELEASE-NOTES.md",
    "version": "Report the family version from sources/family.toml",
}


def _stub(name: str) -> Callable[[argparse.Namespace], int]:
    """Placeholder handler for a command whose build step has not landed."""

    def run(args: argparse.Namespace) -> int:
        raise NotImplementedError(f"asterwell-build {name} is not implemented yet")

    # Lets the tests tell a stub from a real handler without a second list.
    run.is_stub = True  # type: ignore[attr-defined]
    return run


# name -> handler. Each build step replaces its own entry with a real callable
# taking the parsed namespace and returning a process exit code.
COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    name: _stub(name) for name in COMMAND_HELP
}
COMMANDS["fetch"] = upstream.run
COMMANDS["allowlist"] = allowlist.run
COMMANDS["stars"] = stars.run
COMMANDS["build"] = assemble.run
COMMANDS["qa"] = qa.run
COMMANDS["specimen"] = specimen.run

# name -> function adding that command's own options to its subparser. A command
# with no options of its own is simply absent.
COMMAND_ARGUMENTS: dict[str, Callable[[argparse.ArgumentParser], None]] = {
    "fetch": upstream.add_arguments,
    "allowlist": allowlist.add_arguments,
    "stars": stars.add_arguments,
    "build": assemble.add_arguments,
    "qa": qa.add_arguments,
    "specimen": specimen.add_arguments,
}


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser (used by ``main`` and by the tests)."""
    parser = argparse.ArgumentParser(
        prog="asterwell-build",
        description="Build pipeline for the Asterwell Text font family.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    for name, help_text in COMMAND_HELP.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        add_arguments = COMMAND_ARGUMENTS.get(name)
        if add_arguments is not None:
            add_arguments(sub)
        sub.set_defaults(handler=COMMANDS[name])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``asterwell-build`` console script."""
    args = build_parser().parse_args(argv)
    return args.handler(args) or 0


if __name__ == "__main__":  # pragma: no cover - module-as-script convenience
    raise SystemExit(main())
