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
import stat
import sys
import time
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from asterwell_build import allowlist, assemble, qa, specimen, upstream
from asterwell_build.assemble import Family, Log

__all__ = [
    "Member",
    "PackageError",
    "archive_name",
    "assert_tag",
    "checksums_text",
    "dist_dir_for",
    "manifest",
    "package",
    "release_notes",
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
    """``AsterwellText-1.000.zip`` — the PostScript family name, then the version."""
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
) -> str:
    """The release body (§10.2), built from the same data as the other two files."""
    counts = source_counts(rows)
    variable = _asset_rows(digests, "variable")
    webfonts = _asset_rows(digests, "webfonts")
    statics = _asset_rows(digests, "ttf")
    tools = build_info.get("tools")
    tools = tools if isinstance(tools, dict) else {}

    lines: list[str] = [
        f"# {family.family} {family.version}",
        "",
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


def package(root: Path | None = None, *, quiet: bool = False) -> int:
    """``asterwell-build package`` — write the three ``fonts/dist`` files."""
    root = root if root is not None else upstream.default_root()
    log = _logger(quiet)
    family = assemble.load_family(assemble.family_path_for(root))
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
        ),
        encoding="utf-8",
    )

    stamped = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
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
# `asterwell-build version`
# --------------------------------------------------------------------------- #


def family_version(root: Path | None = None) -> str:
    """The version ``sources/family.toml`` declares — the family's own version.

    Not the Python package's: ``pyproject.toml`` pins that at ``0`` on purpose,
    because the build tool and the font it builds are versioned separately.
    """
    root = root if root is not None else upstream.default_root()
    return assemble.load_family(assemble.family_path_for(root)).version


def normalise_tag(tag: str) -> str:
    """``v1.000`` → ``1.000``. One leading ``v``, and surrounding whitespace."""
    stripped = tag.strip()
    return stripped[1:] if stripped[:1] in ("v", "V") else stripped


def assert_tag(tag: str, root: Path | None = None) -> str:
    """Check a release tag against ``family.toml``; raise on a mismatch.

    This is the gate the release workflow runs before it publishes anything
    (§10.2): a tag that does not match the version inside the fonts would ship
    a release whose name and whose ``name`` table disagree, and the disagreement
    would only surface in somebody's font menu.
    """
    version = family_version(root)
    given = normalise_tag(tag)
    if given == version:
        return version
    numerically_equal = _same_number(given, version)
    advice = (
        f"the tag must be spelled exactly `v{version}`"
        if numerically_equal
        else f"either tag `v{version}` or bump `version` in sources/family.toml"
    )
    raise PackageError(
        f"tag {tag!r} does not match sources/family.toml version {version!r} — {advice}"
    )


def _same_number(given: str, version: str) -> bool:
    """``1.0`` and ``1.000`` are the same number spelled two ways."""
    try:
        return float(given) == float(version)
    except ValueError:
        return False


def add_version_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``version``'s own options (called from :mod:`asterwell_build.cli`)."""
    parser.add_argument(
        "--assert-tag",
        metavar="TAG",
        help="exit non-zero unless TAG (with or without a leading v) is this version",
    )


def run_version(args: argparse.Namespace) -> int:
    """``asterwell-build version [--assert-tag vX.YYY]``."""
    tag = getattr(args, "assert_tag", None)
    try:
        if tag is None:
            print(family_version())
        else:
            version = assert_tag(tag)
            print(f"{tag} matches sources/family.toml version {version}")
    except (PackageError, assemble.AssembleError, upstream.UpstreamError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
