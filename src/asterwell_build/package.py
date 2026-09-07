"""Collect the built family into a release payload (§10.2, Step 9).

``asterwell-build package`` builds nothing. It is the last stage of the chain
(`build` → `qa` → `specimen` → `package`) and its whole job is to *gather*
what those stages left behind into three files under ``fonts/dist``:

``AsterwellText-<version>.zip``
    everything a person downloads: the two variable fonts, the sixteen
    statics, the two web fonts, the specimen, the two fontbakery reports,
    the licences and provenance files, ``allowlist.tsv`` and
    ``BUILD-INFO.json``.
``SHA256SUMS.txt``
    a digest per shipped file **and** for the zip, in ``sha256sum`` format,
    with paths relative to ``fonts/`` so that ``cd fonts && sha256sum -c
    dist/SHA256SUMS.txt`` checks a whole build tree in one command.
``RELEASE-NOTES.md``
    the release body: version, the upstream pins, the coverage counts and
    the checksums that matter, written from the same data the other two
    files are built from rather than by hand.

Three properties are worth knowing about, because each is a decision:

**The zip is byte-reproducible, not just its contents.** Every entry is
stamped with one fixed timestamp taken from ``SOURCE_DATE_EPOCH`` (the same
value the fonts' ``head.modified`` came from), stored with a fixed mode and
a fixed creator, and written in sorted order. Two runs over the same tree
give the same bytes, which is what makes CI's "build twice and diff
``SHA256SUMS.txt``" step a real check rather than a coin toss.

**The manifest is derived, not listed.** The twenty font files come out of
``fonts/BUILD-INFO.json``'s ``outputs`` map, and every one of them is
re-hashed here and compared against the digest the build recorded. A stale
or hand-edited ``fonts/`` tree is caught at the moment it would otherwise be
shipped, and the release cannot contain a file the build did not write.

**The fontbakery reports ship, but only the Markdown pair.** A release that
claims to be checked should carry the check's own report, so
``qa/fontbakery-variable.md`` and ``qa/fontbakery-static.md`` are members
like any other. Shipping them at all depends on
:func:`asterwell_build.qa.fontbakery_env`: unseeded, fontbakery orders its
messages differently on every run, and a report that changes byte for byte
between two builds of the same commit would break §10.1's reproducibility
gate — the archive is only reproducible because those reports now are. The
``.html`` twin (the same findings, twice the bytes) and the ``.json`` one
(3.7 MB, nearly doubling the archive, for a machine format no downloader
reads) stay out; both still travel as §10.1's CI run artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from asterwell_build import allowlist, assemble, qa, specimen, upstream
from asterwell_build.assemble import Family, Log

__all__ = [
    "Entry",
    "Member",
    "PackageError",
    "Resolved",
    "archive_name",
    "checksums_text",
    "dist_dir_for",
    "manifest",
    "package",
    "parse_changelog",
    "parse_version",
    "pending_release",
    "release_notes",
    "resolve_version",
    "write_zip",
]


class PackageError(Exception):
    """The release payload cannot be assembled from what is on disk."""


# --------------------------------------------------------------------------- #
# What ships
# --------------------------------------------------------------------------- #

#: Where the three release files are written, relative to ``fonts/``.
DIST_DIR = "dist"

CHECKSUMS_NAME = "SHA256SUMS.txt"
RELEASE_NOTES_NAME = "RELEASE-NOTES.md"

#: Repository files that ship beside the fonts: path from the repository root →
#: the name it is given at the root of the zip. ``sources/allowlist.tsv`` is
#: flattened to ``allowlist.tsv`` because in a release it is not a source, it is
#: the manifest of what the fonts cover (§10.2).
DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("OFL.txt", "OFL.txt"),
    ("DEJAVU-LICENSE.txt", "DEJAVU-LICENSE.txt"),
    ("FONTLOG.txt", "FONTLOG.txt"),
    ("AUTHORS.txt", "AUTHORS.txt"),
    ("README.md", "README.md"),
    (str(Path("sources") / "allowlist.tsv"), "allowlist.tsv"),
)

#: The fontbakery reports that ship, as paths under ``fonts/``. Derived from
#: :mod:`asterwell_build.qa`'s own constants rather than spelled out, so a
#: change to the file classes it runs cannot leave the release listing a report
#: nobody writes. Only the Markdown pair — see the module docstring for why the
#: ``.html`` and ``.json`` twins stay out.
QA_REPORTS: tuple[str, ...] = tuple(
    f"{qa.QA_DIR}/fontbakery-{group}.md" for group in qa.FONTBAKERY_GROUPS
)

#: The oldest and newest instants the ZIP format can record (1980-01-01 and
#: 2107-12-31, UTC): the DOS date field has four bits of month and seven of
#: year. An epoch outside that is clamped rather than raising, because the
#: build's fallback when git is unavailable is ``SOURCE_DATE_EPOCH=0``
#: (mise.toml) and a packaging failure is not the right answer to that.
ZIP_EPOCH_FLOOR = 315532800
ZIP_EPOCH_CEILING = 4354819199

#: Mode every entry is stored with: a regular file, ``rw-r--r--``. Taken from a
#: constant rather than from the file on disk so that a checkout with a
#: different umask still produces the same archive.
ZIP_FILE_MODE = (stat.S_IFREG | 0o644) << 16

#: ZIP "created on a Unix system" — the field is otherwise the running host's,
#: which would make the archive differ between a Linux CI runner and a Mac.
ZIP_CREATE_SYSTEM = 3

#: One gloss per allowlist source, for the coverage table in the notes.
SOURCE_GLOSS: Mapping[str, str] = {
    allowlist.SOURCE_CUSTOM: "original Asterwell outlines",
    allowlist.SOURCE_LITERATA: "preserved from Literata, neither imported nor redrawn",
    allowlist.SOURCE_DEJAVU: "imported from DejaVu Sans, scaled to Literata's cap height",
}


@dataclass(frozen=True)
class Member:
    """One file the release ships, and where it lives in each of the two views."""

    source: Path
    """The file on disk."""

    arcname: str
    """Its path inside the zip."""

    remedy: str
    """What to run when it is missing — the message a person needs, not a trace."""

    def checksum_path(self, fonts_dir: Path) -> str:
        """This file's path as ``SHA256SUMS.txt`` records it: relative to
        ``fonts/``, so a build tree checks with one ``sha256sum -c``.

        The repository documents are not under ``fonts/`` and come out as
        ``../OFL.txt`` and friends. That is deliberate: the alternative is to
        copy them into the build tree, and a checksum of a copy is a checksum
        of the wrong file.
        """
        return Path(os.path.relpath(self.source, fonts_dir)).as_posix()


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def dist_dir_for(root: Path) -> Path:
    return assemble.fonts_dir_for(root) / DIST_DIR


def archive_name(family: Family) -> str:
    """``AsterwellText-0.002.zip`` — the PostScript family name, then the version."""
    return f"{family.ps_family}-{family.version}.zip"


def archive_path_for(root: Path, family: Family) -> Path:
    return dist_dir_for(root) / archive_name(family)


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #


def read_build_info(root: Path) -> Mapping[str, object]:
    """Load ``fonts/BUILD-INFO.json`` — the record of what the build wrote."""
    path = assemble.fonts_dir_for(root) / assemble.BUILD_INFO_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackageError(f"missing {path} — run `mise run build`") from exc
    except json.JSONDecodeError as exc:
        raise PackageError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PackageError(f"{path}: expected an object")
    outputs = raw.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        raise PackageError(f"{path} lists no outputs — run `mise run build`")
    return raw


def build_outputs(build_info: Mapping[str, object]) -> Mapping[str, str]:
    """``BUILD-INFO.json``'s ``outputs``: ``fonts/``-relative path → sha256."""
    outputs = build_info.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        raise PackageError(
            f"{assemble.BUILD_INFO_NAME} lists no outputs — run `mise run build`"
        )
    return {str(path): str(digest) for path, digest in outputs.items()}


def manifest(root: Path, build_info: Mapping[str, object]) -> list[Member]:
    """Every file the zip carries, sorted by the name it has inside it.

    The fonts come from the build's own record rather than from a glob, so a
    file the build did not write cannot ride along and a file it did write
    cannot be silently left out.
    """
    fonts_dir = assemble.fonts_dir_for(root)
    members = [
        Member(fonts_dir / relative, relative, "run `mise run build`")
        for relative in sorted(build_outputs(build_info))
    ]
    page = f"{specimen.SPECIMEN_DIR}/{specimen.SPECIMEN_NAME}"
    members.append(Member(fonts_dir / page, page, "run `mise run specimen`"))
    members += [
        Member(fonts_dir / report, report, "run `mise run qa`")
        for report in QA_REPORTS
    ]
    members.append(
        Member(
            fonts_dir / assemble.BUILD_INFO_NAME,
            assemble.BUILD_INFO_NAME,
            "run `mise run build`",
        )
    )
    members += [
        Member(root / source, arcname, f"{source} is checked in — restore it")
        for source, arcname in DOCUMENTS
    ]

    missing = [member for member in members if not member.source.is_file()]
    if missing:
        listed = "; ".join(f"{member.arcname} ({member.remedy})" for member in missing)
        raise PackageError(f"the release is missing {len(missing)} file(s): {listed}")
    return sorted(members, key=lambda member: member.arcname)


def digest_members(members: Iterable[Member]) -> dict[str, str]:
    """arcname → sha256 of the bytes that will go into the zip."""
    return {member.arcname: upstream.sha256_file(member.source) for member in members}


def build_info_problems(
    digests: Mapping[str, str], build_info: Mapping[str, object]
) -> list[str]:
    """Where the tree disagrees with what the build recorded, one line each.

    Cheap (the digests are computed for ``SHA256SUMS.txt`` anyway) and worth
    doing: it is the difference between shipping the family that was checked
    and shipping whatever happens to be in ``fonts/``.
    """
    return [
        f"{path}: sha256 {digests[path][:12]}… does not match "
        f"BUILD-INFO.json's {recorded[:12]}… — rebuild with `mise run build`"
        for path, recorded in sorted(build_outputs(build_info).items())
        if path in digests and digests[path] != recorded
    ]


# --------------------------------------------------------------------------- #
# The zip
# --------------------------------------------------------------------------- #


def release_epoch(build_info: Mapping[str, object]) -> int:
    """The instant every zip entry is stamped with.

    ``SOURCE_DATE_EPOCH`` when the environment sets one; otherwise the value
    the build recorded, so that ``mise run package`` — where only the ``build``
    task exports the variable — stamps the archive with the same instant as the
    fonts inside it. With neither, the floor: a fixed date beats the clock,
    because a clock makes every archive unique.
    """
    epoch = assemble.source_date_epoch()
    if epoch is None:
        recorded = build_info.get("source_date_epoch")
        if isinstance(recorded, int) and not isinstance(recorded, bool):
            epoch = recorded
    return _in_zip_range(ZIP_EPOCH_FLOOR if epoch is None else epoch)


def _in_zip_range(epoch: int) -> int:
    return max(ZIP_EPOCH_FLOOR, min(ZIP_EPOCH_CEILING, epoch))


def zip_timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    """``(Y, M, D, h, m, s)`` in UTC — the tuple ``zipfile`` stores."""
    parts = time.gmtime(_in_zip_range(epoch))
    return (
        parts.tm_year,
        parts.tm_mon,
        parts.tm_mday,
        parts.tm_hour,
        parts.tm_min,
        parts.tm_sec,
    )


def write_zip(members: Sequence[Member], output: Path, *, epoch: int) -> Path:
    """Write the release archive. Same tree and same epoch → same bytes."""
    timestamp = zip_timestamp(epoch)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for member in members:
            info = zipfile.ZipInfo(member.arcname, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = ZIP_CREATE_SYSTEM
            info.external_attr = ZIP_FILE_MODE
            archive.writestr(info, member.source.read_bytes())
    return output


# --------------------------------------------------------------------------- #
# SHA256SUMS.txt
# --------------------------------------------------------------------------- #

CHECKSUMS_HEADER = (
    "# sha256 of every file this release ships, the release zip included.",
    "# Paths are relative to fonts/: run `cd fonts && sha256sum -c "
    f"{DIST_DIR}/{CHECKSUMS_NAME}`.",
    "# The ../ entries are the repository documents the zip carries at its root.",
)


def checksums_text(entries: Mapping[str, str]) -> str:
    """``SHA256SUMS.txt``: ``sha256sum`` format, sorted, with a short header.

    The two-space separator is what ``sha256sum`` writes for a text-mode read
    and what every checker accepts; the comment lines are skipped by GNU
    coreutils and by Perl's ``shasum``.
    """
    lines = [
        *CHECKSUMS_HEADER,
        *(f"{digest}  {path}" for path, digest in sorted(entries.items())),
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# RELEASE-NOTES.md
# --------------------------------------------------------------------------- #


def source_counts(rows: Sequence[allowlist.Row]) -> dict[str, int]:
    """Allowlist rows per source, in the manifest's own source order."""
    counts = {source: 0 for source in allowlist.SOURCES}
    for row in rows:
        counts[row.source] = counts.get(row.source, 0) + 1
    return counts


def _pinned_at(pin: upstream.Pin) -> str:
    """How a pin is identified in prose: a commit hash or a tag, whichever it is."""
    if not pin.ref:
        return "release asset"
    ref = pin.ref.lower()
    if len(ref) == 40 and all(character in "0123456789abcdef" for character in ref):
        return f"commit `{ref[:12]}`"
    return f"tag `{pin.ref}`"


def _asset_rows(digests: Mapping[str, str], prefix: str) -> list[str]:
    """The zip members under one directory, roman before italic — reading order."""
    return sorted(
        (name for name in digests if name.startswith(f"{prefix}/")),
        key=lambda name: ("-Italic" in name, name),
    )


def release_notes(
    *,
    family: Family,
    pins: Sequence[upstream.Pin],
    rows: Sequence[allowlist.Row],
    digests: Mapping[str, str],
    archive: str,
    archive_sha256: str,
    epoch: int,
    build_info: Mapping[str, object],
    summary: str = "",
) -> str:
    """The release body (§10.2), built from the same data as the other two files.

    ``summary`` is the ChangeLog entry that asked for this release — the one
    human-written sentence about what changed, which GitHub's generated commit
    list underneath cannot supply.
    """
    counts = source_counts(rows)
    variable = _asset_rows(digests, "variable")
    webfonts = _asset_rows(digests, "webfonts")
    statics = _asset_rows(digests, "ttf")
    tools = build_info.get("tools")
    tools = tools if isinstance(tools, dict) else {}

    lines: list[str] = [
        f"# {family.family} v{family.version}",
        "",
        *((summary, "") if summary else ()),
        f"{family.family} is the Literata prose face extended with a curated set of "
        "symbols, arrows, geometric shapes and ornaments, plus an original six-petal "
        "star family. The symbols are scaled to the prose face's cap height and are "
        "invariant across weight and optical size, so a marker set in running text "
        "reads as part of the same voice.",
        "",
        "## Assets",
        "",
        "| Asset | What it is |",
        "| --- | --- |",
        f"| `{archive}` | everything: {len(variable)} variable fonts, {len(statics)} "
        f"statics, {len(webfonts)} web fonts, the specimen, the fontbakery "
        "reports, the licences, `FONTLOG.txt`, `AUTHORS.txt`, `README.md`, "
        "`allowlist.tsv` and `BUILD-INFO.json` |",
        "| "
        + ", ".join(f"`{Path(name).name}`" for name in variable)
        + " | the variable fonts, `opsz` 7–72 and `wght` 200–900, for installing |",
        "| "
        + ", ".join(f"`{Path(name).name}`" for name in webfonts)
        + " | the same two fonts, WOFF2, for the web |",
        f"| `{CHECKSUMS_NAME}` | sha256 of every file this release ships |",
        "| `OFL.txt`, `DEJAVU-LICENSE.txt` | the licences, also inside the zip |",
        "",
        "## Upstream",
        "",
        "| Upstream | Version | Pinned at | Licence |",
        "| --- | --- | --- | --- |",
    ]
    for pin in pins:
        named = f"[{pin.name}]({pin.repo})" if pin.repo else pin.name
        lines.append(
            f"| {named} | {pin.version} | {_pinned_at(pin)}, asset sha256 "
            f"`{pin.sha256[:12]}…` | {pin.license or '—'} |"
        )
    lines += [
        "",
        "The full pins — release URL, byte size, archive and per-member sha256 — are "
        "in `sources/upstream.toml`, and every extracted member is verified against "
        "its own digest before the build touches it.",
        "",
        "## Coverage",
        "",
        "| Source | Code points | |",
        "| --- | ---: | --- |",
    ]
    for source, count in counts.items():
        lines.append(f"| `{source}` | {count} | {SOURCE_GLOSS.get(source, '')} |")
    lines += [
        f"| **total** | **{len(rows)}** | one row per code point in `allowlist.tsv` |",
        "",
        "## Checksums",
        "",
        "```",
        f"{archive_sha256}  {archive}",
        *(f"{digests[name]}  {Path(name).name}" for name in (*variable, *webfonts)),
        "```",
        "",
        f"Every other shipped file is in `{CHECKSUMS_NAME}`, with paths relative to "
        f"`fonts/`: from a build tree, `cd fonts && sha256sum -c "
        f"{DIST_DIR}/{CHECKSUMS_NAME}`.",
        "",
        "## Build",
        "",
        f"Reproducible: built with `SOURCE_DATE_EPOCH={epoch}` "
        f"({time.strftime('%Y-%m-%d', time.gmtime(epoch))}) from the pins above, "
        "so the same commit rebuilds to the same bytes.",
        "",
        "| Tool | Version |",
        "| --- | --- |",
        *(f"| {name} | {version} |" for name, version in sorted(tools.items())),
        "",
        "## Licensing",
        "",
        f"{family.family} is licensed under the SIL Open Font License, Version 1.1 "
        f"(`OFL.txt`)"
        + (
            f', with `"{family.reserved_font_name}"` as a Reserved Font Name'
            if family.reserved_font_name
            else ""
        )
        + ". The glyphs imported from DejaVu Sans come from material the DejaVu "
        "project placed in the public domain; the Bitstream Vera and Arev notices "
        "travel with the fonts in `DEJAVU-LICENSE.txt` and in their name table. "
        "The fonts may be bundled and sold as part of a larger package, but no copy "
        "of the font software may be sold by itself. `FONTLOG.txt` states the "
        "position in full.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def _logger(quiet: bool) -> Log:
    def log(message: str) -> None:
        if not quiet:
            print(message)

    return log


def release_summary(root: Path, version: str) -> str:
    """The ChangeLog entry for the version being packaged, if there is one.

    Grammar is QA's gate — ``mise run package`` runs it before this stage — so a
    ``FONTLOG.txt`` this parser cannot read is not packaging's error to raise:
    the notes simply carry no summary line.
    """
    try:
        entries = read_changelog(root)
    except PackageError:
        return ""
    return next((entry.text for entry in entries if entry.version == version), "")


def package(root: Path | None = None, *, quiet: bool = False) -> int:
    """``asterwell-build package`` — write the three ``fonts/dist`` files."""
    root = root if root is not None else upstream.default_root()
    log = _logger(quiet)
    resolved = resolve_version(root)
    family = assemble.load_family(assemble.family_path_for(root), resolved.version)
    build_info = read_build_info(root)

    members = manifest(root, build_info)
    digests = digest_members(members)
    problems = build_info_problems(digests, build_info)
    if problems:
        raise PackageError(
            f"{len(problems)} shipped file(s) do not match the build: "
            + "; ".join(problems)
        )

    epoch = release_epoch(build_info)
    archive = write_zip(members, archive_path_for(root, family), epoch=epoch)
    archive_sha256 = upstream.sha256_file(archive)

    fonts_dir = assemble.fonts_dir_for(root)
    entries = {
        member.checksum_path(fonts_dir): digests[member.arcname] for member in members
    }
    entries[Path(os.path.relpath(archive, fonts_dir)).as_posix()] = archive_sha256

    checksums = dist_dir_for(root) / CHECKSUMS_NAME
    checksums.write_text(checksums_text(entries), encoding="utf-8")

    notes = dist_dir_for(root) / RELEASE_NOTES_NAME
    notes.write_text(
        release_notes(
            family=family,
            pins=upstream.load_pins(upstream.pin_file_for(root)),
            rows=allowlist.read_tsv(allowlist.tsv_path_for(root)),
            digests=digests,
            archive=archive.name,
            archive_sha256=archive_sha256,
            epoch=epoch,
            build_info=build_info,
            summary=release_summary(root, family.version),
        ),
        encoding="utf-8",
    )

    stamped = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
    log(f"package: version {resolved}")
    log(
        f"package: {archive} ({len(members)} files, "
        f"{archive.stat().st_size:,} B, entries stamped {stamped})"
    )
    log(f"  sha256 {archive_sha256}")
    log(f"  {checksums} ({len(entries)} entries)")
    log(f"  {notes}")
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``package``'s own options (called from :mod:`asterwell_build.cli`)."""
    parser.add_argument("--quiet", action="store_true", help="do not print what was written")


def run(args: argparse.Namespace) -> int:
    """``asterwell-build package`` — see :func:`package`."""
    try:
        return package(quiet=getattr(args, "quiet", False))
    except (
        PackageError,
        assemble.AssembleError,
        allowlist.AllowlistError,
        upstream.UpstreamError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# --------------------------------------------------------------------------- #
# The version: FONTLOG.txt's ChangeLog, the tag it becomes, the build (§15.4)
# --------------------------------------------------------------------------- #
#
# There is no version in any tracked file. What a release *is*, is a ChangeLog
# entry at the top of ``FONTLOG.txt``:
#
#     0.002 (2026-09-10): what changed.
#
# Merging that to `main` runs the Release workflow, which asks `--pending` which
# version the ChangeLog names that no `v*` tag records yet, builds *that* commit
# with it, and — only once the build and QA pass — tags the commit `v0.002` and
# publishes it. The tag is both the version of record and the lock: once it
# exists the entry is no longer pending, so re-runs and later pushes do nothing.
# Nothing is edited or committed by the workflow, and no ritual is performed by
# hand (README, "Releasing").
#
# A build outside that path still needs a version for the name table, so
# :func:`resolve_version` answers in three steps: ``ASTERWELL_VERSION`` (what
# the workflow sets), else a ``v*`` tag pointing at HEAD (a local build of a
# released commit), else ``0.000`` — a dev build, which is what CI, a pull
# request and a laptop get.

#: The file the ChangeLog lives in. If it ever moves, this and `release.yml`'s
#: `paths:` filter are the two places to change (§15.4.1).
FONTLOG_NAME = "FONTLOG.txt"

#: The heading the entries live under, underlined with dashes as FONTLOG's own
#: sections are.
CHANGELOG_HEADING = "ChangeLog"

#: What the release workflow sets, and what a local build can set to rehearse a
#: release: ``ASTERWELL_VERSION=0.002 mise run package``.
VERSION_ENV = "ASTERWELL_VERSION"

#: The version of a build that no tag and no environment names.
DEV_VERSION = "0.000"

#: Where a resolved version came from (:class:`Resolved`).
SOURCE_ENV = "env"
SOURCE_TAG = "tag"
SOURCE_DEV = "dev"

#: A version as written by a person: ``0.001``, or a tag's ``v0.001``. The
#: grammar itself is :data:`asterwell_build.assemble.VERSION_PATTERN`, so the
#: fonts, the tags and the ChangeLog cannot drift apart.
_VERSION_RE = re.compile(rf"^v?{assemble.VERSION_PATTERN}$")

#: A release tag: exactly ``v`` and the version. Tags that do not match are
#: ignored rather than refused — this repository has never had another kind,
#: but a `v*` tag someone adds for a different purpose must not stop a release.
_TAG_RE = re.compile(rf"^v{assemble.VERSION_PATTERN}$")

#: The shape of an entry's first line: a version, a parenthesised date, a colon
#: and the text. Deliberately loose about *what* the version and the date are —
#: a line that reaches for this shape and misses gets an error naming the fix,
#: rather than being read as prose.
_ENTRY_RE = re.compile(r"^(?P<version>[^\s(]+)\s+\((?P<date>[^)]*)\):\s?(?P<text>.*)$")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fontlog_path_for(root: Path) -> Path:
    return root / FONTLOG_NAME


def format_version(major: int, minor: int) -> str:
    """``(0, 2)`` → ``0.002`` — the one spelling, everywhere."""
    return f"{major}.{minor:03d}"


def parse_version(text: str) -> tuple[int, int]:
    """``0.002`` or ``v0.002`` → ``(0, 2)``; anything else raises with the fix.

    Refusing ``0.2`` is the point of the whole scheme: ``head.fontRevision`` is a
    number, so a one- or two-digit minor sorts the tenth release below the ninth
    in a font menu. The message says what to write instead.
    """
    candidate = text.strip()
    match = _VERSION_RE.match(candidate)
    if match is None:
        raise PackageError(f"{candidate!r} is not a version — {_version_advice(candidate)}")
    return int(match.group(1)), int(match.group(2))


def _version_advice(candidate: str) -> str:
    """The remedy for a version that did not parse, as specifically as possible."""
    stripped = candidate[1:] if candidate[:1] in ("v", "V") else candidate
    major, dot, minor = stripped.partition(".")
    if dot and major.isdigit() and minor.isdigit():
        if len(minor) < 3:
            return (
                "the minor is exactly three digits: write "
                f"{int(major)}.{int(minor):03d}"
            )
        if len(minor) > 3:
            return "the minor is exactly three digits, no more"
        if len(major) > 1:
            return f"the major has no leading zeros: write {int(major)}.{minor}"
    return (
        "a version is M.mmm — an integer major, a dot, and exactly three minor "
        "digits (e.g. 0.001)"
    )


@dataclass(frozen=True)
class Entry:
    """One ChangeLog entry: ``0.002 (2026-09-10): what changed.``"""

    version: str
    date: str
    text: str

    @property
    def number(self) -> tuple[int, int]:
        """``(major, minor)`` — how two versions are compared."""
        return parse_version(self.version)

    @property
    def tag(self) -> str:
        return f"v{self.version}"


def _underlined(lines: Sequence[str], index: int) -> bool:
    """FONTLOG's section-heading shape: a line of text under a rule of dashes."""
    if not lines[index].strip() or index + 1 >= len(lines):
        return False
    rule = lines[index + 1].strip()
    return len(rule) >= 3 and set(rule) == {"-"}


def _entry_shape(line: str) -> re.Match[str] | None:
    """The match if this line reaches for the entry shape, whatever it got wrong."""
    return _ENTRY_RE.match(line.strip())


def _is_an_entry(line: str) -> bool:
    """A well-formed entry — used to spot one that has wandered out of place."""
    match = _entry_shape(line)
    if match is None or not _DATE_RE.match(match.group("date")):
        return False
    try:
        parse_version(match.group("version"))
    except PackageError:
        return False
    return True


def parse_changelog(text: str) -> list[Entry]:
    """``FONTLOG.txt``'s ChangeLog, newest entry first.

    The grammar is strict on purpose: this list decides what gets released and
    what number the fonts carry, so every way of writing an entry that a reader
    would understand but a parser would not has to be refused with the fix
    spelled out, before a pull request can merge (`ci.yml`).

    An entry begins at column 0 with ``M.mmm (YYYY-MM-DD): text`` and may run on
    over indented continuation lines. Prose that does not begin with a digit —
    the note under the heading, for instance — is left alone; a line that begins
    with one is an entry and is held to the grammar.
    """
    lines = text.splitlines()
    heading = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() == CHANGELOG_HEADING and _underlined(lines, index)
        ),
        None,
    )
    if heading is None:
        raise PackageError(
            f"{FONTLOG_NAME} has no `{CHANGELOG_HEADING}` heading — the version "
            "being released is read from the entries under it"
        )
    for number, line in enumerate(lines[:heading], start=1):
        if _is_an_entry(line):
            raise PackageError(
                f"{FONTLOG_NAME} line {number} is a ChangeLog entry above the "
                f"`{CHANGELOG_HEADING}` heading — entries go under it, newest first"
            )

    start = heading + 2
    end = next(
        (index for index in range(start, len(lines)) if _underlined(lines, index)),
        len(lines),
    )
    entries: list[Entry] = []
    for number, line in enumerate(lines[start:end], start=start + 1):
        if not line.strip():
            continue
        if line[:1].isspace():
            if _is_an_entry(line):
                raise PackageError(
                    f"{FONTLOG_NAME} line {number}: a ChangeLog entry starts at "
                    "column 0 (indented lines continue the entry above)"
                )
            if entries:
                entries[-1] = replace(
                    entries[-1], text=f"{entries[-1].text} {line.strip()}".strip()
                )
            continue
        if not line[:1].isdigit():
            continue  # prose under the heading: the note that says how to add an entry
        match = _entry_shape(line)
        if match is None:
            raise PackageError(
                f"{FONTLOG_NAME} line {number}: a ChangeLog entry reads "
                "`M.mmm (YYYY-MM-DD): what changed.`"
            )
        try:
            major, minor = parse_version(match.group("version"))
        except PackageError as exc:
            raise PackageError(f"{FONTLOG_NAME} line {number}: {exc}") from exc
        if not _DATE_RE.match(match.group("date")):
            raise PackageError(
                f"{FONTLOG_NAME} line {number}: the date "
                f"{match.group('date')!r} is not YYYY-MM-DD"
            )
        entries.append(
            Entry(
                version=format_version(major, minor),
                date=match.group("date"),
                text=match.group("text").strip(),
            )
        )

    for newer, older in zip(entries, entries[1:]):
        if newer.number == older.number:
            raise PackageError(
                f"{FONTLOG_NAME}'s ChangeLog lists {newer.version} twice — one "
                "entry per version"
            )
        if newer.number < older.number:
            raise PackageError(
                f"{FONTLOG_NAME}'s ChangeLog lists {newer.version} above "
                f"{older.version} — entries are newest first"
            )
    return entries


def parse_tags(names: Iterable[str]) -> dict[tuple[int, int], str]:
    """The release tags among ``names``, as ``(major, minor)`` → tag name."""
    found: dict[tuple[int, int], str] = {}
    for name in names:
        match = _TAG_RE.match(name.strip())
        if match is not None:
            found[(int(match.group(1)), int(match.group(2)))] = name.strip()
    return found


def pending_release(
    entries: Sequence[Entry],
    tags: Iterable[str],
    tags_at_head: Iterable[str] = (),
) -> str | None:
    """The version a merge to ``main`` would release, or ``None`` for nothing.

    "A new entry" is not a text diff: it is a version in the ChangeLog that no
    tag records. So a typo fix, a reworded paragraph, or an edit to an entry
    that has already shipped releases nothing, and the same answer comes back
    whichever commit asks.

    ``tags_at_head`` is the resume case, and only that: a run that pushed its
    tag and then failed before publishing is re-run, finds its own tag on the
    commit it is building, and carries on to the release step.
    """
    released = parse_tags(tags)
    at_head = parse_tags(tags_at_head)
    if not entries:
        return None

    top, older = entries[0], entries[1:]
    untagged = [entry for entry in older if entry.number not in released]
    if untagged:
        raise PackageError(
            f"{FONTLOG_NAME}'s ChangeLog entry {untagged[0].version} has no "
            f"{untagged[0].tag} tag and is not the newest entry — only the top "
            "entry may be untagged (one release at a time)"
        )

    if top.number in released:
        # Already released. Pending only for a re-run of the release that tagged
        # this very commit and did not get as far as publishing.
        return top.version if top.number in at_head else None

    if released:
        highest = max(released)
        if top.number <= highest:
            raise PackageError(
                f"{FONTLOG_NAME}'s top ChangeLog entry {top.version} must be "
                f"greater than the newest tag {released[highest]} — a release "
                "moves forwards (0.001 → 0.002 for a minor, 1.017 → 2.000 for "
                "a major)"
            )
    return top.version


def read_changelog(root: Path) -> list[Entry]:
    """:func:`parse_changelog` over the checked-in ``FONTLOG.txt``."""
    path = fontlog_path_for(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PackageError(f"missing {path} — it is checked in; restore it") from exc
    return parse_changelog(text)


@dataclass(frozen=True)
class Resolved:
    """The version this build carries, and where it came from."""

    version: str
    source: str
    detail: str = ""
    """The tag name, when the version came from one."""

    @property
    def is_release(self) -> bool:
        """A build that claims a version somebody chose, rather than a dev build.

        QA holds these to the FONTLOG gate: the version being built must be the
        ChangeLog's top entry (§15.4.1, Q10).
        """
        return self.source != SOURCE_DEV

    def __str__(self) -> str:
        if self.source == SOURCE_ENV:
            return f"{self.version} (from {VERSION_ENV})"
        if self.source == SOURCE_TAG:
            return f"{self.version} (from the {self.detail} tag at HEAD)"
        return f"{self.version} (dev build)"


def tags_at_head(root: Path) -> list[str]:
    """``git tag --points-at HEAD``, or nothing at all.

    Nothing is what a source download, a checkout without git, or a machine
    without the binary gets — none of which is an error: the answer is then a
    dev build, which is exactly what those are.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "tag", "--points-at", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def resolve_version(
    root: Path | None = None, environ: Mapping[str, str] | None = None
) -> Resolved:
    """What version this build carries: the environment, a tag at HEAD, or dev.

    The release workflow sets ``ASTERWELL_VERSION`` from the pending ChangeLog
    entry, so that the fonts it builds carry the version it is about to tag. A
    local build of an already-released commit finds the tag itself. Everything
    else — CI, a pull request, a laptop — is ``0.000``.
    """
    root = root if root is not None else upstream.default_root()
    environ = environ if environ is not None else os.environ

    named = (environ.get(VERSION_ENV) or "").strip()
    if named:
        try:
            major, minor = parse_version(named)
        except PackageError as exc:
            raise PackageError(f"{VERSION_ENV}: {exc}") from exc
        return Resolved(format_version(major, minor), SOURCE_ENV)

    tags = parse_tags(tags_at_head(root))
    if tags:
        highest = max(tags)
        return Resolved(format_version(*highest), SOURCE_TAG, tags[highest])
    return Resolved(DEV_VERSION, SOURCE_DEV)


def add_version_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``version``'s own options (called from :mod:`asterwell_build.cli`)."""
    parser.add_argument(
        "--pending",
        action="store_true",
        help=(
            "print the version merging this FONTLOG.txt would release, or nothing; "
            "existing tags are read from stdin (`git tag -l 'v*' | …`)"
        ),
    )
    parser.add_argument(
        "--at-head",
        metavar="TAGS",
        default="",
        help=(
            "with --pending: tags pointing at HEAD, so a release that was tagged "
            "but never published is still pending (`git tag --points-at HEAD`)"
        ),
    )


def run_version(args: argparse.Namespace) -> int:
    """``asterwell-build version [--pending [--at-head TAGS]]``."""
    at_head = (getattr(args, "at_head", "") or "").split()
    try:
        if not getattr(args, "pending", False):
            if at_head:
                raise PackageError("--at-head only means something with --pending")
            print(resolve_version())
            return 0
        # stdin is where the tags come from (`git tag -l 'v*' | …`). A terminal
        # is not a tag list: answer from the ChangeLog alone rather than hang.
        tags = [] if sys.stdin.isatty() else sys.stdin.read().splitlines()
        version = pending_release(read_changelog(upstream.default_root()), tags, at_head)
        if version is not None:
            print(version)
    except (PackageError, assemble.AssembleError, upstream.UpstreamError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
