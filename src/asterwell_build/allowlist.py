"""Resolve every promised code point to one source, as a checked-in manifest.

The family is three things at once: Literata's prose face, a hand-picked set of
symbols imported from DejaVu Sans, and four original six-petal stars. Which of
those a given character comes from is a design decision, not an accident of
whichever font happened to have it, so it is recorded per code point in a file
that lives in review: ``sources/allowlist.tsv``.

``sources/allowlist.toml`` holds the *rules* — four block ranges to sweep, a
short list of individually chosen extras, the four custom code points, and any
per-code-point import overrides. This module expands those rules against the
**pinned** fonts in ``build/upstream/`` and writes the *result*, one row per
code point, with the column set::

    codepoint  char  unicode_name  block  source  glyph_name  group
    emoji_presentation  note

Precedence is the design's: an original outline wins over a Literata glyph,
which wins over a DejaVu import. So a range code point Literata already draws
is recorded ``literata`` and is never imported (13 today, all Geometric
Shapes), and a range code point neither font has is simply not listed — the
ranges are a scope, not a promise. The 43 characters of the required inventory
are always listed too, whichever source they resolve to, so the manifest is the
whole answer to "where does this character come from?".

Two things make the file worth committing rather than computing at build time:

*Review.* ``asterwell-build allowlist --check`` regenerates in memory and
diffs, so an upstream bump lands as a reviewable coverage diff rather than a
silent expansion.

*Provenance.* The same pass reports the **Bitstream Vera overlap** — imported
code points that also exist in Bitstream Vera Sans 1.10
(``qa/vera-1.10-codepoints.txt``). It must be empty, and ``--check`` fails if
it is not: the handful of characters the two sets share are all sourced from
Literata, so no outline with a Bitstream Vera lineage ever enters the family
and the DejaVu material that does is the DejaVu team's own, public-domain work.
That does not remove the obligation to ship DejaVu's notices — it is what keeps
the claim behind them true.
"""

from __future__ import annotations

import argparse
import difflib
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import regex
from fontTools import unicodedata as ud
from fontTools.ttLib import TTFont

from asterwell_build import upstream

Log = Callable[[str], None]

__all__ = [
    "AllowlistError",
    "Row",
    "Rules",
    "generate",
    "load_rules",
    "read_tsv",
    "render",
    "resolve_rows",
]

#: Bumped when the generated layout changes, so a stale file is obvious in the
#: diff even if no code point moved.
GENERATOR_VERSION = 1

SOURCE_CUSTOM = "custom"
SOURCE_LITERATA = "literata"
SOURCE_DEJAVU = "dejavu"

#: Report order, which is also precedence order.
SOURCES = (SOURCE_CUSTOM, SOURCE_LITERATA, SOURCE_DEJAVU)

#: The generated file's columns, in order.
COLUMNS = (
    "codepoint",
    "char",
    "unicode_name",
    "block",
    "source",
    "glyph_name",
    "group",
    "emoji_presentation",
    "note",
)

#: ``group`` value for the four original outlines.
GROUP_CUSTOM = "custom"

#: ``group`` value for a required-inventory character no other rule selects —
#: Literata already draws it, so there is nothing to import, but the manifest
#: still answers for it.
GROUP_INVENTORY = "required-inventory"

#: The 38-character UI inventory from the design document, plus the five
#: markers it names alongside it (⁎ ⁑ ⁕ ✽ •): 43 characters the family must
#: cover in every shipped style. Every one appears in the generated file with
#: its resolved source; one that is neither custom nor present in either pinned
#: font is a hard error, not a silent omission.
REQUIRED_INVENTORY = (
    "×⁂←↑→↓↔⇥⇧∅−∪≈≤≥⊆⌘⌫⏎␣─▸▾◆◇○●◦☙⚑⚙✎✓✕✦✱❦❧"  # the 38 UI symbols
    "⁎⁑⁕✽•"  # the five selected markers
)

#: Basenames of the pinned members this module reads, by archive key. Matching
#: on the basename keeps the version out of the code: DejaVu's member path
#: carries its version number, and the pin file is the place that records it.
FONT_MEMBERS: Mapping[str, tuple[str, ...]] = {
    "literata": ("Literata[opsz,wght].ttf", "Literata-Italic[opsz,wght].ttf"),
    "dejavu": ("DejaVuSans.ttf",),
}

#: Only ``[overrides]`` key understood today; see :class:`Rules`.
OVERRIDE_KEYS = frozenset({"match_metrics_of"})

#: Path of the Bitstream Vera fixture, relative to the repository root.
VERA_FIXTURE = Path("qa") / "vera-1.10-codepoints.txt"


class AllowlistError(Exception):
    """The rules are malformed, or the pinned fonts cannot satisfy them."""


# --------------------------------------------------------------------------- #
# Code-point spelling
# --------------------------------------------------------------------------- #


def format_codepoint(codepoint: int) -> str:
    """``U+25E6`` — the spelling used in the rules, the manifest and messages."""
    return f"U+{codepoint:04X}"


def parse_codepoint(value: object, where: str) -> int:
    """Read one ``U+XXXX`` string, rejecting anything a rule file should not say."""
    if not isinstance(value, str):
        raise AllowlistError(f"{where}: expected a \"U+XXXX\" string, got {value!r}")
    text = value.strip()
    if not text.upper().startswith("U+"):
        raise AllowlistError(f"{where}: {value!r} does not start with \"U+\"")
    digits = text[2:]
    if not digits or any(c not in "0123456789abcdefABCDEF" for c in digits):
        raise AllowlistError(f"{where}: {value!r} is not a hex code point")
    codepoint = int(digits, 16)
    if not 0 <= codepoint <= 0x10FFFF:
        raise AllowlistError(f"{where}: {value!r} is outside Unicode")
    return codepoint


def production_name(codepoint: int) -> str:
    """Glyph name an imported or custom outline gets in the output font.

    Only ``uniXXXX`` is produced: the assembly step maps every allowlist code
    point through Literata's two format-4 ``cmap`` subtables, which cannot
    address anything above the BMP, so a supplementary-plane code point is a
    rule error rather than something to name ``uXXXXX`` and fail on later.
    """
    if codepoint > 0xFFFF:
        raise AllowlistError(
            f"{format_codepoint(codepoint)} is outside the BMP; the family's cmap "
            "subtables are format 4 and cannot map it"
        )
    return f"uni{codepoint:04X}"


# --------------------------------------------------------------------------- #
# Rules (sources/allowlist.toml)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Rules:
    """What ``sources/allowlist.toml`` asks for."""

    custom: tuple[int, ...]
    """Code points drawn from scratch; precedence 1, no donor font required."""

    ranges: Mapping[str, tuple[int, int]]
    """Name → inclusive ``(first, last)``. Whatever a pinned font covers."""

    extras: Mapping[str, tuple[int, ...]]
    """Group name → hand-picked code points, each of which must exist in DejaVu."""

    overrides: Mapping[int, Mapping[str, str]]
    """Code point → per-import adjustment applied by the assembly step."""


def rules_path_for(root: Path) -> Path:
    return root / "sources" / "allowlist.toml"


def tsv_path_for(root: Path) -> Path:
    return root / "sources" / "allowlist.tsv"


def vera_path_for(root: Path) -> Path:
    return root / VERA_FIXTURE


def load_rules(path: Path) -> Rules:
    """Parse and validate the rule file."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AllowlistError(f"missing rules file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AllowlistError(f"{path}: not valid TOML: {exc}") from exc

    unknown = set(raw) - {"custom", "ranges", "extras", "overrides"}
    if unknown:
        raise AllowlistError(f"{path}: unknown table(s): {', '.join(sorted(unknown))}")

    return Rules(
        custom=_load_custom(path, raw.get("custom", {})),
        ranges=_load_ranges(path, raw.get("ranges", {})),
        extras=_load_extras(path, raw.get("extras", {})),
        overrides=_load_overrides(path, raw.get("overrides", {})),
    )


def _table(path: Path, name: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise AllowlistError(f"{path}: [{name}] is not a table")
    return value


def _load_custom(path: Path, section: object) -> tuple[int, ...]:
    table = _table(path, "custom", section)
    codepoints = table.get("codepoints", [])
    if not isinstance(codepoints, list):
        raise AllowlistError(f"{path}: [custom] codepoints must be a list")
    parsed = tuple(
        parse_codepoint(value, f"{path}: [custom] codepoints") for value in codepoints
    )
    _reject_duplicates(path, "[custom] codepoints", parsed)
    return parsed


def _load_ranges(path: Path, section: object) -> dict[str, tuple[int, int]]:
    table = _table(path, "ranges", section)
    ranges: dict[str, tuple[int, int]] = {}
    for name, bounds in table.items():
        where = f"{path}: [ranges] {name!r}"
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise AllowlistError(f"{where}: expected [first, last]")
        first, last = (parse_codepoint(value, where) for value in bounds)
        if first > last:
            raise AllowlistError(
                f"{where}: {format_codepoint(first)} is above "
                f"{format_codepoint(last)}"
            )
        ranges[name] = (first, last)
    return ranges


def _load_extras(path: Path, section: object) -> dict[str, tuple[int, ...]]:
    table = _table(path, "extras", section)
    extras: dict[str, tuple[int, ...]] = {}
    for name, codepoints in table.items():
        where = f"{path}: [extras] {name}"
        if not isinstance(codepoints, list):
            raise AllowlistError(f"{where}: expected a list of code points")
        parsed = tuple(parse_codepoint(value, where) for value in codepoints)
        _reject_duplicates(path, f"[extras] {name}", parsed)
        extras[name] = parsed
    return extras


def _load_overrides(path: Path, section: object) -> dict[int, dict[str, str]]:
    table = _table(path, "overrides", section)
    overrides: dict[int, dict[str, str]] = {}
    for key, options in table.items():
        where = f"{path}: [overrides] {key!r}"
        codepoint = parse_codepoint(key, where)
        if not isinstance(options, dict) or not options:
            raise AllowlistError(f"{where}: expected a non-empty table of adjustments")
        unknown = set(options) - OVERRIDE_KEYS
        if unknown:
            raise AllowlistError(
                f"{where}: unknown adjustment(s) {', '.join(sorted(unknown))}; "
                f"known: {', '.join(sorted(OVERRIDE_KEYS))}"
            )
        overrides[codepoint] = {
            name: format_codepoint(parse_codepoint(value, f"{where} {name}"))
            for name, value in options.items()
        }
    return overrides


def _reject_duplicates(path: Path, where: str, codepoints: Sequence[int]) -> None:
    seen: set[int] = set()
    for codepoint in codepoints:
        if codepoint in seen:
            raise AllowlistError(f"{path}: {where} lists {format_codepoint(codepoint)} twice")
        seen.add(codepoint)


# --------------------------------------------------------------------------- #
# Coverage of the pinned fonts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceFont:
    """One pinned font file, as the generated header records it."""

    project: str
    version: str
    member: str
    sha256: str
    url: str


@dataclass(frozen=True)
class Coverage:
    """What the pinned fonts cover, and which files that was read from."""

    literata: Mapping[int, str]
    """Code point → Literata's own glyph name, for the code points **both**
    styles map to the same name; those are the ones that can be preserved."""

    dejavu: frozenset[int]
    """Every code point DejaVu Sans maps."""

    literata_split: frozenset[int] = frozenset()
    """Code points the roman and italic disagree about. Harmless unless a rule
    selects one, in which case the family would be inconsistent between styles
    and resolution stops rather than guessing."""

    fonts: tuple[SourceFont, ...] = ()
    """Provenance for the generated header; empty in unit tests."""


def read_unicode_cmap(path: Path) -> dict[int, str]:
    """Union of a font's Unicode ``cmap`` subtables, code point → glyph name."""
    font = TTFont(path, lazy=True)
    try:
        mapping: dict[int, str] = {}
        for subtable in font["cmap"].tables:
            if not subtable.isUnicode():
                continue
            for codepoint, name in subtable.cmap.items():
                previous = mapping.setdefault(codepoint, name)
                if previous != name:
                    raise AllowlistError(
                        f"{path}: cmap subtables disagree about "
                        f"{format_codepoint(codepoint)}: {previous} vs {name}"
                    )
        return mapping
    finally:
        font.close()


def load_coverage(root: Path) -> Coverage:
    """Read the pinned fonts named in ``build/upstream/.verified``."""
    manifest = upstream.read_verified(root)
    files = _pinned_font_files(root, manifest)

    # A glyph can only be *preserved* if both styles have it under the same name;
    # anything the two disagree about is held back for resolve_rows to complain
    # about, and only if a rule actually asks for it.
    roman, italic = (read_unicode_cmap(path) for path, _font in files["literata"])
    shared = {cp: name for cp, name in roman.items() if italic.get(cp) == name}
    split = (set(roman) | set(italic)) - set(shared)

    dejavu_path, _dejavu_font = files["dejavu"][0]
    dejavu = frozenset(read_unicode_cmap(dejavu_path))

    return Coverage(
        literata=shared,
        dejavu=dejavu,
        literata_split=frozenset(split),
        fonts=tuple(font for entries in files.values() for _path, font in entries),
    )


def _pinned_font_files(
    root: Path, manifest: Mapping[str, object]
) -> dict[str, list[tuple[Path, SourceFont]]]:
    """Locate each font this module reads inside the verified upstream tree."""
    archives = manifest.get("archives")
    if not isinstance(archives, dict):
        raise AllowlistError("build/upstream/.verified has no `archives` table")
    upstream_dir = upstream.upstream_dir_for(root)

    found: dict[str, list[tuple[Path, SourceFont]]] = {}
    for key, basenames in FONT_MEMBERS.items():
        entry = archives.get(key)
        if not isinstance(entry, dict):
            raise AllowlistError(
                f"build/upstream/.verified has no `{key}` archive; re-run `mise run fetch`"
            )
        members = entry.get("members")
        if not isinstance(members, dict):
            raise AllowlistError(f"build/upstream/.verified: [{key}] has no `members` table")
        base = upstream_dir / str(entry.get("dir", key))
        for basename in basenames:
            matches = [name for name in members if name.rsplit("/", 1)[-1] == basename]
            if len(matches) != 1:
                raise AllowlistError(
                    f"sources/upstream.toml [{key}.members] must pin exactly one "
                    f"{basename}; found {len(matches)}"
                )
            member = matches[0]
            path = base / member
            if not path.is_file():
                raise AllowlistError(f"{path} is missing — run `mise run fetch`")
            found.setdefault(key, []).append(
                (
                    path,
                    SourceFont(
                        project=str(entry.get("name", key)),
                        version=str(entry.get("version", "")),
                        member=member,
                        sha256=str(members[member]),
                        url=str(entry.get("url", "")),
                    ),
                )
            )
    return found


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Row:
    """One code point of the family, and where its outline comes from."""

    codepoint: int
    char: str
    unicode_name: str
    block: str
    source: str
    glyph_name: str
    group: str
    emoji_presentation: str
    """``yes`` when platforms default to an emoji rendering; see
    :func:`is_emoji_presentation`."""

    note: str

    def fields(self) -> tuple[str, ...]:
        """The row as it is written, in :data:`COLUMNS` order."""
        return (
            format_codepoint(self.codepoint),
            self.char,
            self.unicode_name,
            self.block,
            self.source,
            self.glyph_name,
            self.group,
            self.emoji_presentation,
            self.note,
        )


def is_emoji_presentation(char: str) -> bool:
    """True when a platform renders ``char`` as colour emoji by default.

    Such a character needs U+FE0E after it to get the text presentation these
    glyphs are drawn for, which is app-facing information — hence a column
    rather than a build-time decision. The property comes from the ``regex``
    package's Unicode tables, so no external UCD file is fetched.
    """
    return regex.match(r"\p{Emoji_Presentation}", char) is not None


def _unicode_name(char: str, codepoint: int) -> str:
    name = ud.name(char, "")
    return name if name else f"<unnamed {format_codepoint(codepoint)}>"


def resolve_rows(rules: Rules, coverage: Coverage) -> list[Row]:
    """Expand the rules against the fonts, sorted by code point.

    Selection and precedence are separate questions. *Selection* is which rule
    first asked for a code point, and becomes the ``group`` column; *precedence*
    is custom > preserved Literata > DejaVu, and becomes ``source``.
    """
    selected: dict[int, str] = {}

    def select(codepoint: int, group: str) -> None:
        selected.setdefault(codepoint, group)

    for codepoint in rules.custom:
        select(codepoint, GROUP_CUSTOM)
    for name, codepoints in rules.extras.items():
        for codepoint in codepoints:
            _require_dejavu(coverage, codepoint, f"[extras] {name}")
            select(codepoint, name)
    for name, (first, last) in rules.ranges.items():
        for codepoint in range(first, last + 1):
            # A range is a scope, not a promise: nothing covers it, nothing to say.
            if codepoint in coverage.literata or codepoint in coverage.dejavu:
                select(codepoint, name)
    for char in REQUIRED_INVENTORY:
        select(ord(char), GROUP_INVENTORY)

    custom = frozenset(rules.custom)
    rows = [
        _row_for(codepoint, selected[codepoint], rules, coverage, custom)
        for codepoint in sorted(selected)
    ]
    _check_overrides(rules, rows, coverage)
    return rows


def _require_dejavu(coverage: Coverage, codepoint: int, where: str) -> None:
    """An extra is a hand-picked DejaVu import; a missing one is a rule error."""
    if codepoint not in coverage.dejavu:
        raise AllowlistError(
            f"{where}: {format_codepoint(codepoint)} {chr(codepoint)!r} is not in the "
            "pinned DejaVu Sans — remove it from the rules or pick a covered character"
        )


def _row_for(
    codepoint: int,
    group: str,
    rules: Rules,
    coverage: Coverage,
    custom: frozenset[int],
) -> Row:
    char = chr(codepoint)
    if codepoint in coverage.literata_split:
        raise AllowlistError(
            f"{format_codepoint(codepoint)} {char!r} is mapped differently by the two "
            "Literata styles; the family cannot preserve it consistently"
        )

    if codepoint in custom:
        source, glyph_name = SOURCE_CUSTOM, production_name(codepoint)
    elif codepoint in coverage.literata:
        source, glyph_name = SOURCE_LITERATA, coverage.literata[codepoint]
    elif codepoint in coverage.dejavu:
        source, glyph_name = SOURCE_DEJAVU, production_name(codepoint)
    else:
        # Only reachable for the required inventory: ranges skip what nothing
        # covers, and extras were checked against DejaVu when they were selected.
        raise AllowlistError(
            f"required inventory: {format_codepoint(codepoint)} {char!r} is in neither "
            "Literata nor DejaVu Sans and is not a custom outline"
        )

    override = rules.overrides.get(codepoint, {})
    note = "; ".join(
        f"metrics of {value}" if key == "match_metrics_of" else f"{key} {value}"
        for key, value in sorted(override.items())
    )
    return Row(
        codepoint=codepoint,
        char=char,
        unicode_name=_unicode_name(char, codepoint),
        block=ud.block(char),
        source=source,
        glyph_name=glyph_name,
        group=group,
        emoji_presentation="yes" if is_emoji_presentation(char) else "no",
        note=note,
    )


def _check_overrides(rules: Rules, rows: Sequence[Row], coverage: Coverage) -> None:
    """An override must adjust an import, against a glyph the family will have."""
    by_codepoint = {row.codepoint: row for row in rows}
    for codepoint, options in rules.overrides.items():
        where = f"[overrides] {format_codepoint(codepoint)}"
        row = by_codepoint.get(codepoint)
        if row is None:
            raise AllowlistError(f"{where}: no rule selects that code point")
        if row.source != SOURCE_DEJAVU:
            raise AllowlistError(
                f"{where}: adjusts an import, but {format_codepoint(codepoint)} is "
                f"{row.source}"
            )
        for key, value in options.items():
            reference = parse_codepoint(value, f"{where} {key}")
            if reference not in by_codepoint and reference not in coverage.literata:
                raise AllowlistError(
                    f"{where} {key}: {value} is in neither the allowlist nor Literata, "
                    "so the family has no such glyph to match"
                )


# --------------------------------------------------------------------------- #
# Bitstream Vera overlap
# --------------------------------------------------------------------------- #


def load_vera_codepoints(path: Path) -> frozenset[int]:
    """Read the checked-in Bitstream Vera Sans 1.10 fixture."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AllowlistError(f"missing Bitstream Vera fixture: {path}") from exc
    codepoints = set()
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            codepoints.add(parse_codepoint(stripped, f"{path}:{number}"))
    if not codepoints:
        raise AllowlistError(f"{path}: no code points")
    return frozenset(codepoints)


def vera_overlap(rows: Iterable[Row], vera: frozenset[int]) -> list[Row]:
    """Imported rows that Bitstream Vera Sans 1.10 also covers — must be empty.

    The characters the two sets share are all drawn by Literata, so preserving
    Literata's glyph (precedence) is what keeps this list empty. A non-empty
    list means an outline with a Bitstream Vera lineage would be copied in, and
    the licensing claims in ``FONTLOG.txt`` would no longer be true.
    """
    return [row for row in rows if row.source == SOURCE_DEJAVU and row.codepoint in vera]


# --------------------------------------------------------------------------- #
# Rendering and parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Summary:
    """Counts a run reports, and the header records."""

    total: int
    by_source: Mapping[str, int]
    by_block: Mapping[str, Mapping[str, int]]
    emoji: int
    inventory: Mapping[str, int]
    overlap: tuple[Row, ...] = ()


def summarize(rows: Sequence[Row], overlap: Sequence[Row] = ()) -> Summary:
    by_source = dict.fromkeys(SOURCES, 0)
    by_block: dict[str, dict[str, int]] = {}
    inventory_codepoints = {ord(char) for char in REQUIRED_INVENTORY}
    inventory = dict.fromkeys(SOURCES, 0)
    emoji = 0
    for row in rows:
        by_source[row.source] = by_source.get(row.source, 0) + 1
        block = by_block.setdefault(row.block, dict.fromkeys(SOURCES, 0))
        block[row.source] += 1
        if row.emoji_presentation == "yes":
            emoji += 1
        if row.codepoint in inventory_codepoints:
            inventory[row.source] += 1
    return Summary(
        total=len(rows),
        by_source=by_source,
        by_block=by_block,
        emoji=emoji,
        inventory=inventory,
        overlap=tuple(overlap),
    )


def render(
    rows: Sequence[Row],
    *,
    fonts: Sequence[SourceFont] = (),
    overlap: Sequence[Row] = (),
) -> str:
    """The complete ``sources/allowlist.tsv`` text, header included."""
    summary = summarize(rows, overlap)
    lines = [
        "# sources/allowlist.tsv — GENERATED by `asterwell-build allowlist`; do not edit.",
        "#",
        "# Every code point the family promises, and where its outline comes from:",
        "#   custom    an original Asterwell outline",
        "#   literata  preserved from Literata; neither imported nor redrawn",
        "#   dejavu    imported from DejaVu Sans, scaled to Literata's cap height",
        "#",
        "# Rules live in sources/allowlist.toml. Regenerate with `mise run allowlist`",
        "# and commit the diff; `asterwell-build allowlist --check` fails if this file",
        "# no longer matches the rules and the pinned fonts.",
        "#",
        f"# generator {GENERATOR_VERSION}",
    ]
    for font in fonts:
        lines.append(f"# {font.project} {font.version} — {font.member}")
        lines.append(f"#     sha256 {font.sha256}")
        if font.url:
            lines.append(f"#     from   {font.url}")
    counts = ", ".join(f"{summary.by_source.get(name, 0)} {name}" for name in SOURCES)
    lines += [
        "#",
        f"# {summary.total} rows: {counts}",
        f"# {summary.emoji} rows are emoji-presentation by default and need U+FE0E in text",
    ]
    lines.append(
        f"# {len(summary.overlap)} imported code point(s) appear in Bitstream Vera "
        f"Sans 1.10 ({VERA_FIXTURE.as_posix()}); this must be 0"
    )
    lines.append("#")
    lines.append("\t".join(COLUMNS))
    lines += ["\t".join(row.fields()) for row in rows]
    return "\n".join(lines) + "\n"


def parse_rows(text: str, where: str = "<allowlist>") -> list[Row]:
    """Inverse of :func:`render`, ignoring the header comments."""
    rows: list[Row] = []
    header_seen = False
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if not header_seen:
            if tuple(fields) != COLUMNS:
                raise AllowlistError(
                    f"{where}:{number}: expected the column header "
                    f"{'/'.join(COLUMNS)}, got {line!r}"
                )
            header_seen = True
            continue
        if len(fields) != len(COLUMNS):
            raise AllowlistError(
                f"{where}:{number}: expected {len(COLUMNS)} tab-separated fields, "
                f"got {len(fields)}"
            )
        codepoint = parse_codepoint(fields[0], f"{where}:{number}")
        rows.append(
            Row(
                codepoint=codepoint,
                char=fields[1],
                unicode_name=fields[2],
                block=fields[3],
                source=fields[4],
                glyph_name=fields[5],
                group=fields[6],
                emoji_presentation=fields[7],
                note=fields[8],
            )
        )
    if not header_seen:
        raise AllowlistError(f"{where}: no column header row")
    return rows


def read_tsv(path: Path) -> list[Row]:
    """Load the checked-in manifest — the reader every later build step uses."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AllowlistError(
            f"missing {path} — run `mise run allowlist` to generate it"
        ) from exc
    return parse_rows(text, str(path))


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def _logger(quiet: bool) -> Log:
    def log(message: str) -> None:
        if not quiet:
            print(message)

    return log


def _report(summary: Summary, log: Log) -> None:
    log(f"{summary.total} rows")
    for name in SOURCES:
        log(f"  {name:<9}{summary.by_source.get(name, 0):>5}")
    log("")
    log(f"  {'block':<34}{'rows':>6}{'custom':>8}{'literata':>10}{'dejavu':>8}")
    for block in sorted(summary.by_block):
        counts = summary.by_block[block]
        total = sum(counts.values())
        log(
            f"  {block:<34}{total:>6}{counts[SOURCE_CUSTOM]:>8}"
            f"{counts[SOURCE_LITERATA]:>10}{counts[SOURCE_DEJAVU]:>8}"
        )
    log("")
    inventory = ", ".join(f"{summary.inventory.get(name, 0)} {name}" for name in SOURCES)
    covered = sum(summary.inventory.values())
    log(f"required inventory: {covered}/{len(REQUIRED_INVENTORY)} — {inventory}")
    log(f"emoji presentation: {summary.emoji} row(s) need U+FE0E in text")


def _report_overlap(overlap: Sequence[Row], vera: frozenset[int], log: Log) -> None:
    log(
        f"Bitstream Vera Sans 1.10 overlap: {len(overlap)} of {len(vera)} fixture code "
        f"point(s) are imported from DejaVu"
    )
    for row in overlap:
        log(f"  {format_codepoint(row.codepoint)} {row.char} {row.unicode_name}")


def generate(
    root: Path | None = None,
    *,
    check: bool = False,
    quiet: bool = False,
) -> int:
    """Write (or verify) ``sources/allowlist.tsv``; returns a process exit code."""
    root = root if root is not None else upstream.default_root()
    log = _logger(quiet)

    rules = load_rules(rules_path_for(root))
    coverage = load_coverage(root)
    rows = resolve_rows(rules, coverage)

    vera = load_vera_codepoints(vera_path_for(root))
    overlap = vera_overlap(rows, vera)

    text = render(rows, fonts=coverage.fonts, overlap=overlap)
    path = tsv_path_for(root)
    status = 0

    if check:
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(
                f"error: {path} does not exist — run `mise run allowlist`",
                file=sys.stderr,
            )
            return 1
        if current != text:
            diff = difflib.unified_diff(
                current.splitlines(keepends=True),
                text.splitlines(keepends=True),
                fromfile=f"{path} (checked in)",
                tofile=f"{path} (regenerated)",
            )
            sys.stdout.writelines(diff)
            print(
                f"error: {path} is stale — run `mise run allowlist` and commit the diff",
                file=sys.stderr,
            )
            return 1
        log(f"{path} is up to date")
    else:
        path.write_text(text, encoding="utf-8")
        log(f"wrote {path}")

    _report(summarize(rows, overlap), log)
    _report_overlap(overlap, vera, log)
    if overlap:
        print(
            "error: code points imported from DejaVu must not exist in Bitstream Vera "
            "Sans 1.10 — source the ones listed above from Literata instead",
            file=sys.stderr,
        )
        status = 1
    return status


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``allowlist``'s own options (called from :mod:`asterwell_build.cli`)."""
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "regenerate in memory and fail if sources/allowlist.tsv differs, instead "
            "of rewriting it"
        ),
    )


def run(args: argparse.Namespace) -> int:
    """``asterwell-build allowlist`` — see :func:`generate`."""
    try:
        return generate(check=getattr(args, "check", False))
    except (AllowlistError, upstream.UpstreamError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
