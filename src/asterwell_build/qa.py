"""Prove that what ``build`` wrote is the family this repository promises.

``asterwell-build qa`` reads the finished files — the two variable fonts, the
sixteen statics and the two WOFF2 web fonts, the last decompressed in memory —
and answers seven questions about them, each as a plain pass/fail with a
one-line reason when it fails:

*Metrics freeze.* Adding 508 glyphs to somebody else's typeface must not move
its line box. The variable fonts are compared field by field against the pinned
Literata they were cut from, envelope included; a static's envelope is its own
(a variable font's ``head`` box is the *default instance's*, and Bold really is
bigger than Regular — Literata's own statics do the same), so a static freezes
the vertical metrics, the ``fsSelection`` bits outside the style bits, the
feature lists and the units per em, and its box and widest advance are
*reported* instead.

*Coverage.* Every row of ``sources/allowlist.tsv`` reaches a glyph with real
ink in every shipped file, the ``cmap`` gained exactly the approved code points
and nothing else, and ``star.small`` is reachable only as a component.

*Shaping.* HarfBuzz is the only witness that matters for "does this character
work": every allowlist row and every character of the required inventory shapes
to a real glyph, the imported and original symbols hold one advance across the
whole design space (D5) while ordinary prose moves.

*Names and bits.* The expected strings are computed by the very functions the
build writes from — :func:`asterwell_build.assemble.name_strings` and
:func:`asterwell_build.instances.name_strings` — so the check cannot drift from
the build. No Macintosh records, no upstream family name anywhere it would be
read as ours, the Reserved Font Name only in the copyright.

*Stars.* The eight ornaments have the envelopes
:mod:`asterwell_build.stars` computes — and every drawn one is that generator's
outline point for point — ⁎ ⁑ ⁂ are composites of ``star.small`` alone, and the
italic's stars are the roman's turned by ``[italic] rotation`` about their own
centres, with the stacks of ⁑ and ⁂ leaned by ``stack_slant`` and nothing
sheared (D21).

*Licensing.* The OFL body is the pinned upstream text byte for byte, the DejaVu
notices are the pinned file, and the notices that must travel in name ID 0 are
there.

*fontbakery.* ``check-universal`` over the built fonts, once per file class,
with the four documented exclusions in ``qa/fontbakery.yml``. Any FAIL or ERROR
outside those fails the command. The invocation is seeded
(``PYTHONHASHSEED=0``, see :func:`fontbakery_env`) so that the reports it
writes into ``fonts/qa`` are byte-stable across runs and can travel in the
release zip.

Running the variable fonts and the statics as **two** invocations is not a
convenience: four of fontbakery's family-wide checks (``family/single_directory``,
``opentype/family/max_4_fonts_per_family_name``,
``opentype/family/bold_italic_unique_for_nameid1``,
``opentype/varfont/family_axis_ranges``) compare every file given on one command
line as if it were one family, and a variable font plus the statics cut from it
is two file classes, not six styles. Passing them together produces four FAILs
that say nothing about the fonts; passing them apart produces none.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import pathops
import uharfbuzz as hb
from fontTools.ttLib import TTFont, woff2
from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphCoordinates

from asterwell_build import allowlist, assemble, instances, stars, upstream, web
from asterwell_build.assemble import Family, Invariants, Style
from asterwell_build.instances import Instance

Log = Callable[[str], None]

__all__ = [
    "QaError",
    "Report",
    "Result",
    "check",
    "run",
]


class QaError(Exception):
    """The built family cannot be checked — a file is missing or unreadable."""


# --------------------------------------------------------------------------- #
# What gets checked
# --------------------------------------------------------------------------- #

KIND_VARIABLE = "variable"
KIND_STATIC = "static"
KIND_WEBFONT = "webfont"

#: File class → the directory under ``fonts/`` it lives in.
DIRECTORIES: Mapping[str, str] = {
    KIND_VARIABLE: "variable",
    KIND_STATIC: "ttf",
    KIND_WEBFONT: "webfonts",
}

#: Where the fontbakery reports are written, under ``fonts/``.
QA_DIR = "qa"

#: The three design-space corners §9.3 names, and the label each is reported
#: under. The middle one is the default instance: what a static is cut at.
SHAPING_LOCATIONS: tuple[tuple[str, Mapping[str, float]], ...] = (
    ("wght 200, opsz 7", {"wght": 200.0, "opsz": 7.0}),
    ("wght 400, opsz 12", {"wght": 400.0, "opsz": 12.0}),
    ("wght 900, opsz 72", {"wght": 900.0, "opsz": 72.0}),
)

#: Index of the default location in :data:`SHAPING_LOCATIONS`.
DEFAULT_LOCATION = 1

#: Prose glyphs whose advance *must* move across those locations. Without them
#: an all-invariant font would pass the invariance check for the wrong reason:
#: these prove the axes still do something.
VARYING_SAMPLES = ("H", "x")

#: Sources whose glyphs this build owns, and which must therefore be invariant
#: across the design space (D5).
INVARIANT_SOURCES = (allowlist.SOURCE_CUSTOM, allowlist.SOURCE_DEJAVU)

#: Name IDs that identify the *family* to a host. None of them may contain an
#: upstream project's name: a user picking "Asterwell Text" out of a menu must
#: not be reading Literata's or DejaVu's name.
IDENTITY_NAME_IDS = (1, 3, 4, 6, 16, 17, 25)

#: The only records allowed to say "Literata": the copyright, the designer
#: credit and the description, which is where the derivation is *supposed* to
#: be stated (§7.4).
LITERATA_NAME_IDS = (0, 9, 10)

#: Upstream names that must never appear in an :data:`IDENTITY_NAME_IDS` record.
UPSTREAM_WORDS = (
    "Literata",
    "Google",
    "Bitstream",
    "Vera",
    "Arev",
    "DejaVu",
    "Tavmjong",
)

#: The OFL's Reserved Font Name clause belongs in the copyright and nowhere
#: else; a host that shows it anywhere else is showing a licence term as a name.
RESERVED_FONT_NAME_PHRASE = "Reserved Font Name"

#: Name ID that carries the copyright and every upstream notice.
COPYRIGHT_NAME_ID = 0

#: Literata's trademark record. Deleted rather than rewritten (§7.4), because a
#: claim about a different font must not travel with this one.
TRADEMARK_NAME_ID = 7

#: Records a static inherits unchanged from the variable font it was cut from.
#: The rest of its identity is :func:`asterwell_build.instances.name_strings`.
INHERITED_NAME_IDS = (0, 5, 8, 9, 10, 11, 13, 14)

#: Tolerance on a star's bounding box, in font units (§9.5).
STAR_BBOX_TOLERANCE = 2

#: How much of one style's star may fall outside the other style's turned copy,
#: as a fraction of its own area (§15.1.5). The two outlines are drawn from the
#: same template but rounded to the grid independently, which alone accounts for
#: about 1 %; a shear of the stacks' own 2.5° is 8 % and up and an unturned
#: italic is over 100 %, so the gate tells a rotation from a skew by a factor of
#: six rather than merely accepting anything nearby.
STAR_ROTATION_TOLERANCE = 0.02

#: How far one star of a stack may sit from where the lean rule puts it, in font
#: units. Each style rounds its own placement to the grid, so a component can be
#: half a unit out in each and a whole unit apart — never more, which is why this
#: gate cannot fire on rounding alone.
STAR_LEAN_TOLERANCE = 1.0

#: The on-curve bit of a TrueType point flag. It is the only bit that says
#: anything about the *drawing*: the instancer sets ``OVERLAP_SIMPLE`` (0x40) on
#: the first point of every static it cuts, which is a rasteriser hint and not a
#: change to the outline, so the comparisons here mask the flags down to this.
ON_CURVE = 0x01

#: Slice of the pinned Literata ``OFL.txt`` that is the licence body: from the
#: line of dashes to the end, i.e. everything but the upstream's own header.
OFL_BODY_SLICE = slice(7, 93)

#: Sentences name ID 0 must carry verbatim, whatever else it says.
REQUIRED_NOTICES = (assemble.LITERATA_NOTICE, assemble.DEJAVU_NOTICE)

#: Longest list a one-line reason spells out before it starts counting.
REASON_ITEMS = 6


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Result:
    """One check against one subject."""

    check: str
    subject: str
    reasons: tuple[str, ...] = ()
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.reasons


@dataclass
class Report:
    """Every result of one ``qa`` run, and whether the run passed."""

    results: list[Result] = field(default_factory=list)

    def add(
        self,
        check: str,
        subject: str,
        reasons: Iterable[str] = (),
        note: str = "",
    ) -> Result:
        result = Result(check, subject, tuple(reasons), note)
        self.results.append(result)
        return result

    @property
    def failures(self) -> list[Result]:
        return [result for result in self.results if not result.ok]

    @property
    def ok(self) -> bool:
        return not self.failures


def summarize(items: Sequence[object], limit: int = REASON_ITEMS) -> str:
    """A list of items as one line, counted rather than spelled out past
    ``limit`` — every failure reason this module produces is a single line."""
    shown = ", ".join(str(item) for item in items[:limit])
    if len(items) > limit:
        return f"{shown} … and {len(items) - limit} more"
    return shown


# --------------------------------------------------------------------------- #
# The files, and what each is measured against
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Target:
    """One shipped file: which class it belongs to and which style it is."""

    path: Path
    kind: str
    style_key: str
    instance: Instance | None = None

    @property
    def label(self) -> str:
        return f"{DIRECTORIES[self.kind]}/{self.path.name}"

    @property
    def variable(self) -> bool:
        """Whether the file still has a design space to move in."""
        return self.kind in (KIND_VARIABLE, KIND_WEBFONT)


@dataclass(frozen=True)
class StyleReference:
    """Everything one style's outputs are checked against, read once."""

    style: Style
    source: Path
    """The pinned Literata member this style was built from."""

    invariants: Invariants
    codepoints: frozenset[int]
    names: Mapping[int, str]
    """Expected name records, from the same function the build wrote them with."""

    stars: Mapping[str, stars.StarGlyph]
    variable_path: Path
    instances: tuple[Instance, ...]
    advances: Mapping[int, int]
    """Code point → x-advance at the default location of the *variable* font;
    what a static's invariant glyphs must reproduce."""

    upem: int


def font_bytes(target: Target) -> bytes:
    """The TrueType bytes of one target — a WOFF2 decompressed in memory.

    A web font is the same font in a different container, so it is checked as
    one rather than trusted because the TTF beside it passed.
    """
    if target.kind != KIND_WEBFONT:
        return target.path.read_bytes()
    buffer = BytesIO()
    try:
        woff2.decompress(str(target.path), buffer)
    except Exception as exc:  # noqa: BLE001 - any failure here means the same thing
        raise QaError(f"{target.label}: cannot decompress: {exc}") from exc
    return buffer.getvalue()


def open_font(data: bytes) -> TTFont:
    return TTFont(BytesIO(data), recalcTimestamp=False)


def discover(
    root: Path, family: Family, parameters: stars.Parameters, log: Log
) -> tuple[list[StyleReference], list[Target]]:
    """Locate every file the build should have written, and read the references.

    Nothing is opened twice and nothing is held open: each reference is a small
    value read from the pinned input and the built variable font, so the 18
    targets can then be checked one at a time.
    """
    fonts_dir = assemble.fonts_dir_for(root)
    references: list[StyleReference] = []
    targets: list[Target] = []

    for style in assemble.STYLES:
        source = assemble.pinned_member(root, "literata", style.member)
        literata = TTFont(source, lazy=True)
        try:
            reference_invariants = assemble.invariants(literata)
            codepoints = frozenset(assemble.unicode_cmap(literata))
            names = assemble.name_strings(literata, family, style)
        finally:
            literata.close()

        variable_path = fonts_dir / DIRECTORIES[KIND_VARIABLE] / assemble.output_name(
            family, style
        )
        if not variable_path.is_file():
            raise QaError(f"{variable_path} is missing — run `mise run build`")
        data = variable_path.read_bytes()
        vf = open_font(data)
        try:
            style_instances = tuple(instances.named_instances(vf))
            upem = vf["head"].unitsPerEm
        finally:
            vf.close()

        references.append(
            StyleReference(
                style=style,
                source=source,
                invariants=reference_invariants,
                codepoints=codepoints,
                names=names,
                stars=stars.build_glyphs(parameters, style.key),
                variable_path=variable_path,
                instances=style_instances,
                advances=default_advances(data, upem),
                upem=upem,
            )
        )

        targets.append(Target(variable_path, KIND_VARIABLE, style.key))
        targets.append(
            Target(
                fonts_dir / DIRECTORIES[KIND_WEBFONT] / web.output_name(variable_path),
                KIND_WEBFONT,
                style.key,
            )
        )
        targets += [
            Target(
                fonts_dir / DIRECTORIES[KIND_STATIC] / instances.output_name(family, one),
                KIND_STATIC,
                style.key,
                instance=one,
            )
            for one in style_instances
        ]

    missing = [target.label for target in targets if not target.path.is_file()]
    if missing:
        raise QaError(
            f"{len(missing)} built file(s) missing — run `mise run build`: "
            f"{summarize(missing)}"
        )
    log(f"checking {len(targets)} file(s) in {fonts_dir}")
    return references, targets


def unexpected_files(root: Path, targets: Sequence[Target]) -> list[str]:
    """Files sitting in the shipped directories that the build did not write.

    A stale output from an earlier name or an earlier weight is not harmless:
    it is what the release zip would carry and what a host would install.
    """
    fonts_dir = assemble.fonts_dir_for(root)
    expected = {target.path.resolve() for target in targets}
    strays: list[str] = []
    for kind, name in DIRECTORIES.items():
        directory = fonts_dir / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.resolve() not in expected:
                strays.append(f"{name}/{path.name}")
    return strays


# --------------------------------------------------------------------------- #
# 9.1 Metrics freeze
# --------------------------------------------------------------------------- #


def metrics_problems(
    font: TTFont, target: Target, reference: StyleReference
) -> list[str]:
    """Nothing the family froze may have moved (§9.1, as corrected in Step 7).

    A variable font and its web font are compared whole against the pinned
    Literata — vertical metrics, ``head`` box, ``hhea.advanceWidthMax``, ``fvar``,
    ``avar``, ``MVAR`` and the feature lists alike. A static cannot be: pinning
    a weight recomputes the box and the widest advance from that weight's own
    outlines, and Bold's box is legitimately larger than the variable font's in
    every direction — Literata's own statics behave identically. So a static is
    frozen on everything that *can* be frozen and its envelope is reported.
    """
    measured = assemble.invariants(font)
    if target.kind == KIND_STATIC:
        return instances.frozen_differences(reference.invariants, measured)
    return reference.invariants.differences(measured)


def envelope(font: TTFont) -> str:
    """The one-line envelope a static reports instead of freezing."""
    head, hhea = font["head"], font["hhea"]
    return (
        f"head bbox ({head.xMin}, {head.yMin}, {head.xMax}, {head.yMax}), "
        f"advanceWidthMax {hhea.advanceWidthMax}"
    )


# --------------------------------------------------------------------------- #
# 9.2 Coverage
# --------------------------------------------------------------------------- #


def has_content(glyph: object) -> bool:
    """True when a glyph draws something: an outline, or at least one component."""
    contours = getattr(glyph, "numberOfContours", 0)
    if contours > 0:
        return True
    return bool(getattr(glyph, "components", ()))


def coverage_problems(
    font: TTFont, rows: Sequence[allowlist.Row], source_codepoints: frozenset[int]
) -> list[str]:
    """Every promised code point reaches real ink, and nothing else was added.

    The three questions are separate on purpose. *Reachability* is whether the
    manifest's row can be typed; *identity* is whether it reaches the glyph the
    manifest names (a mapping that silently moved to another glyph is a coverage
    bug the count would hide); *delta* is whether the file gained exactly the
    approved code points relative to the Literata it came from.
    """
    problems: list[str] = []
    cmap = assemble.unicode_cmap(font)
    glyf = font["glyf"]

    unmapped: list[str] = []
    misdirected: list[str] = []
    empty: list[str] = []
    for row in rows:
        name = cmap.get(row.codepoint)
        if name is None:
            unmapped.append(allowlist.format_codepoint(row.codepoint))
            continue
        if name != row.glyph_name:
            misdirected.append(
                f"{allowlist.format_codepoint(row.codepoint)} → {name} "
                f"(manifest says {row.glyph_name})"
            )
            continue
        if name not in glyf.glyphs or not has_content(glyf[name]):
            empty.append(f"{allowlist.format_codepoint(row.codepoint)} {name}")
    if unmapped:
        problems.append(f"{len(unmapped)} allowlist row(s) not in cmap: {summarize(unmapped)}")
    if misdirected:
        problems.append(
            f"{len(misdirected)} allowlist row(s) map to another glyph: "
            f"{summarize(misdirected)}"
        )
    if empty:
        problems.append(
            f"{len(empty)} allowlist row(s) map to an empty glyph: {summarize(empty)}"
        )

    approved = {row.codepoint for row in rows if row.source in INVARIANT_SOURCES}
    expected_added = approved - source_codepoints
    added = set(cmap) - source_codepoints
    removed = source_codepoints - set(cmap)
    unapproved = sorted(added - expected_added)
    absent = sorted(expected_added - added)
    if unapproved:
        problems.append(
            f"{len(unapproved)} cmap entry/entries added that the allowlist does not "
            f"approve: {summarize([allowlist.format_codepoint(cp) for cp in unapproved])}"
        )
    if absent:
        problems.append(
            f"{len(absent)} approved code point(s) never reached the cmap: "
            f"{summarize([allowlist.format_codepoint(cp) for cp in absent])}"
        )
    if removed:
        problems.append(
            f"{len(removed)} code point(s) Literata mapped are gone: "
            f"{summarize([allowlist.format_codepoint(cp) for cp in sorted(removed)])}"
        )
    return problems


def component_only_problems(font: TTFont, name: str = stars.SMALL) -> list[str]:
    """``star.small`` is a shared outline, not a character: it belongs in the
    glyph order and in no ``cmap`` subtable at all — not only in the Unicode
    ones, since a legacy subtable would encode it just as effectively."""
    problems: list[str] = []
    if name not in font.getGlyphOrder():
        return [f"{name} is not in the glyph order"]
    encodings = sorted(
        {
            f"({subtable.platformID},{subtable.platEncID}) "
            f"{allowlist.format_codepoint(codepoint)}"
            for subtable in font["cmap"].tables
            for codepoint, glyph in subtable.cmap.items()
            if glyph == name
        }
    )
    if encodings:
        problems.append(
            f"{name} is an unencoded component but is mapped by "
            f"{len(encodings)} cmap entry/entries: {summarize(encodings)}"
        )
    return problems


# --------------------------------------------------------------------------- #
# 9.3 Shaping
# --------------------------------------------------------------------------- #


def shaping_font(data: bytes, upem: int, variations: Mapping[str, float] | None) -> hb.Font:
    """A HarfBuzz font at one design-space location, scaled to font units."""
    font = hb.Font(hb.Face(data))
    font.scale = (upem, upem)
    if variations:
        font.set_variations(dict(variations))
    return font


def shape(font: hb.Font, text: str) -> tuple[tuple[int, ...], int]:
    """Shape one string: the glyph ids it produced and their total x-advance."""
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    hb.shape(font, buffer)
    return (
        tuple(info.codepoint for info in buffer.glyph_infos),
        sum(position.x_advance for position in buffer.glyph_positions),
    )


def default_advances(data: bytes, upem: int) -> dict[int, int]:
    """Every code point's x-advance at the variable font's default instance.

    This is the number a static cut at ``opsz 12`` must reproduce for the glyphs
    that do not vary; taking it from HarfBuzz rather than from ``hmtx`` means the
    comparison is of what a text engine actually does.
    """
    _label, location = SHAPING_LOCATIONS[DEFAULT_LOCATION]
    font = shaping_font(data, upem, location)
    cmap = TTFont(BytesIO(data), lazy=True)
    try:
        codepoints = sorted(assemble.unicode_cmap(cmap))
    finally:
        cmap.close()
    return {codepoint: shape(font, chr(codepoint))[1] for codepoint in codepoints}


def shaping_problems(
    data: bytes,
    target: Target,
    reference: StyleReference,
    rows: Sequence[allowlist.Row],
    inventory: str = allowlist.REQUIRED_INVENTORY,
) -> list[str]:
    """§9.3: nothing shapes to ``.notdef``, symbols hold still, prose does not.

    The whole manifest is shaped rather than a sample of it — 532 single-character
    runs at three locations is a fraction of a second, and a sample is exactly
    the kind of check that passes while the one glyph nobody sampled is broken.
    """
    problems: list[str] = []
    locations = SHAPING_LOCATIONS if target.variable else (SHAPING_LOCATIONS[DEFAULT_LOCATION],)
    fonts = [
        (label, shaping_font(data, reference.upem, location if target.variable else None))
        for label, location in locations
    ]

    characters = sorted({row.char for row in rows} | set(inventory))
    notdef: list[str] = []
    advances: dict[str, list[int]] = {}
    for character in characters:
        measured: list[int] = []
        for label, font in fonts:
            gids, advance = shape(font, character)
            if 0 in gids:
                notdef.append(f"{allowlist.format_codepoint(ord(character))} at {label}")
            measured.append(advance)
        advances[character] = measured
    if notdef:
        problems.append(
            f"{len(notdef)} character(s) shape to .notdef: {summarize(notdef)}"
        )

    drifting: list[str] = []
    mismatched: list[str] = []
    for row in rows:
        if row.source not in INVARIANT_SOURCES:
            continue
        measured = advances[row.char]
        if len(set(measured)) > 1:
            drifting.append(
                f"{allowlist.format_codepoint(row.codepoint)} {tuple(measured)}"
            )
        expected = reference.advances.get(row.codepoint)
        if expected is not None and measured[0] != expected:
            mismatched.append(
                f"{allowlist.format_codepoint(row.codepoint)} {measured[0]} "
                f"≠ {expected}"
            )
    if drifting:
        problems.append(
            f"{len(drifting)} invariant glyph(s) change advance across the design "
            f"space: {summarize(drifting)}"
        )
    if mismatched:
        problems.append(
            f"{len(mismatched)} invariant glyph(s) do not match the variable font's "
            f"default advance: {summarize(mismatched)}"
        )

    if target.variable:
        steady = [
            f"{character!r} {tuple(shape(font, character)[1] for _label, font in fonts)}"
            for character in VARYING_SAMPLES
            if len({shape(font, character)[1] for _label, font in fonts}) == 1
        ]
        if steady:
            problems.append(
                f"prose glyph(s) do not vary across the design space, so the axes are "
                f"not working: {summarize(steady)}"
            )
    return problems


# --------------------------------------------------------------------------- #
# 9.4 Names and bits
# --------------------------------------------------------------------------- #


def expected_names(
    target: Target, reference: StyleReference, family: Family
) -> dict[int, str]:
    """What this file's name table must say, from the build's own functions."""
    if target.instance is None:
        return dict(reference.names)
    strings = {
        name_id: reference.names[name_id]
        for name_id in INHERITED_NAME_IDS
        if name_id in reference.names
    }
    strings.update(instances.name_strings(family, target.instance))
    return strings


def forbidden_names(target: Target) -> tuple[int, ...]:
    """Name IDs this file must not carry at all."""
    absent = [TRADEMARK_NAME_ID]
    if target.instance is not None:
        absent += list(instances.VARIABLE_ONLY_NAME_IDS)
        if target.instance.ribbi:
            absent += list(instances.TYPOGRAPHIC_NAME_IDS)
    return tuple(sorted(set(absent)))


def windows_names(font: TTFont) -> dict[int, str]:
    """The Windows/English records, ID → string. After D18 there are no others."""
    platform, encoding, language = assemble.WINDOWS_ENGLISH
    return {
        record.nameID: str(record)
        for record in font["name"].names
        if (record.platformID, record.platEncID, record.langID)
        == (platform, encoding, language)
    }


def name_problems(
    font: TTFont, target: Target, reference: StyleReference, family: Family
) -> list[str]:
    """§9.4: the strings, the platforms, and what must not appear anywhere."""
    problems: list[str] = []
    present = windows_names(font)
    expected = expected_names(target, reference, family)

    wrong = [
        f"ID {name_id} is {present.get(name_id)!r}, expected {value!r}"
        for name_id, value in sorted(expected.items())
        if present.get(name_id) != value
    ]
    if wrong:
        problems.append(f"{len(wrong)} name record(s) wrong: {summarize(wrong, 2)}")

    lingering = [name_id for name_id in forbidden_names(target) if name_id in present]
    if lingering:
        problems.append(
            f"name ID(s) that must be absent are present: "
            f"{summarize([f'{i} ({present[i]!r})' for i in lingering])}"
        )

    mac = [record.nameID for record in font["name"].names if record.platformID == 1]
    if mac:
        problems.append(
            f"{len(mac)} Macintosh name record(s) survive (D18 drops them all): "
            f"{summarize(sorted(set(mac)))}"
        )

    reserved = [
        name_id
        for name_id, value in present.items()
        if RESERVED_FONT_NAME_PHRASE in value and name_id != COPYRIGHT_NAME_ID
    ]
    if reserved:
        problems.append(
            f"{RESERVED_FONT_NAME_PHRASE!r} appears outside name ID "
            f"{COPYRIGHT_NAME_ID}: {summarize(sorted(reserved))}"
        )
    if family.reserved_font_name and RESERVED_FONT_NAME_PHRASE not in present.get(
        COPYRIGHT_NAME_ID, ""
    ):
        problems.append(
            f"family.toml declares the Reserved Font Name "
            f"{family.reserved_font_name!r} but name ID {COPYRIGHT_NAME_ID} does not "
            f"state it"
        )

    leaks = [
        f"ID {name_id}: {word}"
        for name_id in IDENTITY_NAME_IDS
        for word in UPSTREAM_WORDS
        if word in present.get(name_id, "")
    ]
    if leaks:
        problems.append(
            f"an upstream project's name appears where this family's own belongs: "
            f"{summarize(leaks)}"
        )
    stray_literata = [
        name_id
        for name_id, value in present.items()
        if "Literata" in value and name_id not in LITERATA_NAME_IDS
    ]
    if stray_literata:
        problems.append(
            f"'Literata' appears outside name ID(s) "
            f"{'/'.join(str(i) for i in LITERATA_NAME_IDS)}: "
            f"{summarize(sorted(stray_literata))}"
        )

    missing_notices = [
        notice for notice in REQUIRED_NOTICES if notice not in present.get(COPYRIGHT_NAME_ID, "")
    ]
    if missing_notices:
        problems.append(
            f"name ID {COPYRIGHT_NAME_ID} is missing {len(missing_notices)} required "
            f"upstream notice(s): {summarize([n[:48] + '…' for n in missing_notices], 2)}"
        )
    return problems


#: One unit of ``head.fontRevision``. The field is a 16.16 fixed-point number,
#: so most versions have no exact representation: ``0.002`` is written, and read
#: back, as 0.0019989013671875 — 1.1e-6 away, which a tolerance tighter than the
#: format's own grid would call a defect. Half a step is the real question ("is
#: this the same number after rounding to 16.16?"), and it still separates two
#: adjacent releases: 0.001 and 0.002 are 65 steps apart.
FONT_REVISION_STEP = 1 / 65536


def bit_problems(font: TTFont, target: Target, family: Family) -> list[str]:
    """``head.fontRevision``, the vendor id, and a static's style bits."""
    problems: list[str] = []
    revision = font["head"].fontRevision
    if abs(revision - family.font_revision) > FONT_REVISION_STEP / 2:
        problems.append(
            f"head.fontRevision is {revision}, expected {family.font_revision} "
            "(to within half a 16.16 step)"
        )
    vendor = font["OS/2"].achVendID
    if vendor != family.vendor_bytes():
        problems.append(
            f"OS/2.achVendID is {vendor!r}, expected {family.vendor_bytes()!r}"
        )

    instance = target.instance
    if instance is None:
        return problems

    os2, head = font["OS/2"], font["head"]
    if os2.usWeightClass != instance.weight:
        problems.append(
            f"OS/2.usWeightClass is {os2.usWeightClass}, expected {instance.weight}"
        )
    style_mask = instances.FS_ITALIC | instances.FS_BOLD | instances.FS_REGULAR
    if os2.fsSelection & style_mask != instance.fs_selection_bits:
        problems.append(
            f"fsSelection style bits are {os2.fsSelection & style_mask:#06x}, expected "
            f"{instance.fs_selection_bits:#06x}"
        )
    if not os2.fsSelection & instances.FS_USE_TYPO_METRICS:
        problems.append("fsSelection lost USE_TYPO_METRICS (bit 7)")
    mac_mask = instances.MAC_BOLD | instances.MAC_ITALIC
    if head.macStyle & mac_mask != instance.mac_style_bits:
        problems.append(
            f"head.macStyle is {head.macStyle & mac_mask:#04x}, expected "
            f"{instance.mac_style_bits:#04x}"
        )
    return problems


# --------------------------------------------------------------------------- #
# 9.5 Stars
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StarSignature:
    """One style's stars, in a shape the two styles can be compared through."""

    outlines: Mapping[str, tuple[tuple[int, ...], ...]]
    """Simple glyphs: the coordinates, contour ends and on-curve flags that
    ship. Enough to redraw the outline (:func:`outline_path`), which is what the
    cross-style rule needs: the italic's is the roman's turned, not its bytes."""

    components: Mapping[str, tuple[tuple[str, int, int], ...]]
    """Composites: each component's name, its x **relative to the glyph's own
    centre and doubled** (``2·x − advance``, so that a half-unit stays an
    integer), and its y exactly as it ships. That is the only form in which the
    two styles can be compared at all: ⁎ ⁑ ⁂ inherit the style's own asterisk
    advance, so their absolute offsets differ by half that difference before
    anything leans. Nothing moves a star vertically, so y needs no such care —
    but it does mean every x read from here is halved before it is a distance."""


def outline_signature(glyph: Glyph, glyf: object | None) -> tuple[tuple[int, ...], ...]:
    """One simple glyph as ``(coordinates, contour ends, on-curve flags)``.

    The flags are masked to :data:`ON_CURVE` so that two spellings of the same
    outline compare equal: a static carries the instancer's ``OVERLAP_SIMPLE``
    hint on its first point, and a hint is not a drawing.
    """
    coordinates, end_points, flags = glyph.getCoordinates(glyf)
    return (
        tuple(int(value) for point in coordinates for value in point),
        tuple(int(end) for end in end_points),
        tuple(int(flag) & ON_CURVE for flag in flags),
    )


def outline_path(outline: tuple[tuple[int, ...], ...]) -> pathops.Path:
    """A signature's outline back as a drawable path, ready to be compared.

    The comparison the italic needs is geometric, not textual — one shape turned
    onto another — so the points come back through a real quadratic pen rather
    than being differenced as numbers.
    """
    coordinates, end_points, flags = outline
    glyph = Glyph()
    glyph.numberOfContours = len(end_points)
    glyph.coordinates = GlyphCoordinates(
        [(coordinates[i], coordinates[i + 1]) for i in range(0, len(coordinates), 2)]
    )
    glyph.endPtsOfContours = list(end_points)
    glyph.flags = array.array("B", flags)
    path = pathops.Path()
    glyph.draw(path.getPen(), None)
    return path


def centred(path: pathops.Path) -> pathops.Path:
    """The same path with its bounding box centred on the origin.

    Where a star sits inside its advance is the *placement* question, answered
    by :func:`star_problems` against the generator's own numbers. What is being
    asked here is about the shape alone, so both styles are moved to one spot
    first.
    """
    x_min, y_min, x_max, y_max = path.bounds
    return path.transform(
        1.0, 0.0, 0.0, 1.0, -(x_min + x_max) / 2.0, -(y_min + y_max) / 2.0
    )


def turned(path: pathops.Path, degrees: float) -> pathops.Path:
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    return path.transform(cos, sin, -sin, cos, 0.0, 0.0)


def difference_area(one: pathops.Path, other: pathops.Path) -> float:
    """Area covered by exactly one of the two shapes: 0 if they coincide."""
    out = pathops.Path()
    pathops.xor([one], [other], out.getPen())
    return abs(out.area)


def star_signature(font: TTFont, advances: Mapping[str, int]) -> StarSignature:
    glyf = font["glyf"]
    outlines: dict[str, tuple[tuple[int, ...], ...]] = {}
    components: dict[str, tuple[tuple[str, int, int], ...]] = {}
    for name in stars.GLYPH_ORDER:
        glyph = glyf[name]
        if glyph.isComposite():
            components[name] = tuple(
                (component.glyphName, 2 * int(component.x) - advances[name], int(component.y))
                for component in glyph.components
            )
        else:
            outlines[name] = outline_signature(glyph, glyf)
    return StarSignature(outlines=outlines, components=components)


def star_problems(font: TTFont, expected: Mapping[str, stars.StarGlyph]) -> list[str]:
    """§9.5: every ornament has the envelope :mod:`stars` computed and, when it
    is drawn rather than assembled, that generator's outline point for point;
    ⁎ ⁑ ⁂ are built from ``star.small`` and nothing else.

    The outline comparison is exact because the build *is* the generator: these
    glyphs are written straight out of :func:`stars.build_glyphs`, so anything
    but equality means a point moved somewhere between drawing and shipping.
    ``expected`` is built per style, so the italic is checked against the turned
    template and the roman against the upright one.
    """
    problems: list[str] = []
    glyf = font["glyf"]
    order = set(font.getGlyphOrder())
    for name, star in expected.items():
        if name not in order:
            problems.append(f"{name} is missing")
            continue
        advance, _lsb = font["hmtx"][name]
        if advance != star.advance:
            problems.append(f"{name} advance {advance}, expected {star.advance}")
        glyph = glyf[name]
        bounds = (glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax)
        drift = max(abs(a - b) for a, b in zip(bounds, star.bounds))
        if drift > STAR_BBOX_TOLERANCE:
            problems.append(
                f"{name} bbox {bounds}, expected {star.bounds} "
                f"(±{STAR_BBOX_TOLERANCE})"
            )
        found = tuple(component.glyphName for component in getattr(glyph, "components", ()))
        if found != star.components:
            problems.append(
                f"{name} is built from {found or 'an outline'}, expected "
                f"{star.components or 'an outline'}"
            )
        elif not star.is_composite:
            problems += outline_drift(name, outline_signature(glyph, glyf), star)
    return problems


def outline_drift(
    name: str, shipped: tuple[tuple[int, ...], ...], star: stars.StarGlyph
) -> list[str]:
    """One drawn star against the outline :mod:`stars` generated for it."""
    drawn = outline_signature(star.glyph, None)
    if shipped == drawn:
        return []
    shipped_points, shipped_ends, _ = shipped
    drawn_points, drawn_ends, _ = drawn
    if len(shipped_points) != len(drawn_points) or shipped_ends != drawn_ends:
        return [
            f"{name} is {len(shipped_points) // 2} point(s) in "
            f"{len(shipped_ends)} contour(s), expected {len(drawn_points) // 2} "
            f"in {len(drawn_ends)}"
        ]
    moved = sum(
        1
        for index in range(0, len(drawn_points), 2)
        if shipped_points[index : index + 2] != drawn_points[index : index + 2]
    )
    if moved:
        return [f"{name} is not the outline `stars` draws: {moved} point(s) moved"]
    return [f"{name} is not the outline `stars` draws: an on-curve flag changed"]


def cross_style_star_problems(
    signatures: Mapping[str, StarSignature],
    kind: str,
    parameters: stars.Parameters,
) -> list[str]:
    """The stars are one drawing, turned — never a second drawing (D21).

    Both styles come from the same template, so the italic's outline has to be
    the roman's rotated by ``[italic] rotation`` about its own centre and
    nothing else: not sheared, not redrawn, not left upright. That is measured
    as area rather than compared as bytes, because the two are rounded to the
    grid independently — see :data:`STAR_ROTATION_TOLERANCE` for what the gate
    separates.

    The composites cannot be byte-identical either: each inherits its style's
    own asterisk advance, which is what :class:`StarSignature` normalises away.
    What is left is the arrangement, and the arrangement moves in exactly one
    way — the *stacks* ⁑ and ⁂ lean in the italic, every star shifting
    horizontally by its own height above the stack's mean times
    ``tan(stack_slant)``. So each italic component is checked against the roman's
    plus that shift, and each stack is checked for leaning by the angle its own
    style claims: ``stack_slant`` in the italic, nothing at all in the roman.
    """
    if stars.ROMAN not in signatures or stars.ITALIC not in signatures:
        return []
    roman, italic = signatures[stars.ROMAN], signatures[stars.ITALIC]
    rotation = parameters.italic.rotation
    problems: list[str] = []

    for name in sorted(roman.outlines):
        if name not in italic.outlines:
            problems.append(f"{name} is drawn in the roman but not in the italic ({kind})")
            continue
        upright = centred(outline_path(roman.outlines[name]))
        leaning = centred(outline_path(italic.outlines[name]))
        area = abs(leaning.area)
        if not area:
            problems.append(f"{name} in the italic encloses no area ({kind})")
            continue
        ratio = difference_area(turned(upright, rotation), leaning) / area
        if ratio > STAR_ROTATION_TOLERANCE:
            problems.append(
                f"{name} in the italic is not the roman turned {rotation:g}° "
                f"({kind}): {ratio:.1%} of its area falls outside it, over "
                f"{STAR_ROTATION_TOLERANCE:.0%}"
            )

    for name in sorted(roman.components):
        problems += stack_problems(name, roman, italic, kind, parameters)
    return problems


def stack_problems(
    name: str,
    roman: StarSignature,
    italic: StarSignature,
    kind: str,
    parameters: stars.Parameters,
) -> list[str]:
    """One composite's arrangement in the two styles.

    The stars are paired by height, top first, rather than by the order they
    happen to be written in: nothing moves a star vertically between the styles,
    so the heights are what identify it, and star 1 below is the top of the
    stack in both. Offsets arrive doubled (:class:`StarSignature`), so every
    measurement here is halved back into font units before it is compared with a
    tolerance.
    """
    here = sorted(roman.components[name], key=lambda component: -component[2])
    there = sorted(italic.components.get(name, ()), key=lambda component: -component[2])
    if [component for component, _x, _y in here] != [
        component for component, _x, _y in there
    ]:
        return [
            f"{name} is arranged differently in the roman and the italic "
            f"({kind}): {here} vs {there}"
        ]
    problems: list[str] = []
    lean = parameters.italic.lean
    mean_y = sum(y for _, _x, y in here) / len(here)
    for index, ((_, x_roman, y_roman), (_, x_italic, y_italic)) in enumerate(
        zip(here, there), start=1
    ):
        if y_italic != y_roman:
            problems.append(
                f"{name}'s star {index} sits at y {y_italic} in the italic and "
                f"{y_roman} in the roman ({kind}): the lean is horizontal"
            )
            continue
        shift = (y_roman - mean_y) * lean
        drift = abs(x_italic - x_roman - 2 * shift) / 2.0
        if drift > STAR_LEAN_TOLERANCE:
            problems.append(
                f"{name}'s star {index} moved {(x_italic - x_roman) / 2.0:+.1f} "
                f"units in the italic, expected {shift:+.1f} "
                f"(±{STAR_LEAN_TOLERANCE:g}, {kind})"
            )
    for style, signature, slant in (
        (stars.ROMAN, roman, 0.0),
        (stars.ITALIC, italic, parameters.italic.stack_slant),
    ):
        upright = stack_slant_problem(name, signature, style, slant, kind)
        if upright:
            problems.append(upright)
    return problems


def stack_slant_problem(
    name: str, signature: StarSignature, style: str, slant: float, kind: str
) -> str | None:
    """Whether one style's stack leans by the angle that style claims.

    The measurement is the one the design states: the top star against the mean
    of the ones it stands on — ⁑'s single partner, ⁂'s pair — which is 0 in the
    roman and ``rise · tan(slant)`` in the italic. A stack of one (⁎) has
    nothing to lean against and is skipped.
    """
    components = signature.components.get(name, ())
    if len(components) < 2:
        return None
    ordered = sorted(components, key=lambda component: component[2])
    _, top_x, top_y = ordered[-1]
    below = ordered[:-1]
    base_x = sum(x for _, x, _ in below) / len(below)
    base_y = sum(y for _, _, y in below) / len(below)
    # x arrives doubled and y does not (:class:`StarSignature`).
    measured = (top_x - base_x) / 2.0
    wanted = (top_y - base_y) * math.tan(math.radians(slant))
    if abs(measured - wanted) <= STAR_LEAN_TOLERANCE:
        return None
    return (
        f"{name}'s top star sits {measured:+.1f} units from the ones below it in "
        f"the {style}, expected {wanted:+.1f} for a {slant:g}° lean "
        f"(±{STAR_LEAN_TOLERANCE:g}, {kind})"
    )


# --------------------------------------------------------------------------- #
# 9.6 Licensing
# --------------------------------------------------------------------------- #


def license_problems(root: Path, family: Family, *, release: bool = False) -> list[str]:
    """The licence files say what the fonts claim they say.

    The OFL body is not paraphrased or re-typed: it is the pinned upstream file
    from its line of dashes onward, compared line for line, because a licence
    with a typo in it is a licence nobody can rely on.

    ``release`` says whether this build carries a version somebody chose
    (``ASTERWELL_VERSION`` or a tag at HEAD) rather than the ``0.000`` dev
    default; :func:`fontlog_problems` holds those to the ChangeLog.
    """
    problems: list[str] = []

    ofl = root / "OFL.txt"
    pinned = assemble.pinned_member(root, "literata", "OFL.txt")
    try:
        ours = ofl.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return [f"{ofl} is missing"]
    body = pinned.read_text(encoding="utf-8").splitlines(keepends=True)[OFL_BODY_SLICE]
    if "".join(ours[-len(body):]) != "".join(body):
        problems.append(
            f"OFL.txt's licence body is not the pinned upstream text "
            f"({pinned.name} lines {OFL_BODY_SLICE.start + 1}–{OFL_BODY_SLICE.stop})"
        )

    dejavu = root / "DEJAVU-LICENSE.txt"
    pins = {pin.key: pin for pin in upstream.load_pins(upstream.pin_file_for(root))}
    expected = next(
        (
            digest
            for member, digest in pins["dejavu"].members.items()
            if member.rsplit("/", 1)[-1] == "LICENSE"
        ),
        None,
    )
    if expected is None:
        problems.append("sources/upstream.toml pins no DejaVu LICENSE member")
    elif not dejavu.is_file():
        problems.append(f"{dejavu} is missing")
    else:
        digest = upstream.sha256_file(dejavu)
        if digest != expected:
            problems.append(
                f"DEJAVU-LICENSE.txt sha256 {digest[:12]}… does not match the pin "
                f"{expected[:12]}…"
            )

    # The one place this module names the file; `release.yml`'s `paths:` filter
    # is the other place it is spelled (§15.4.1).
    fontlog = root / "FONTLOG.txt"
    if not fontlog.is_file():
        problems.append(f"{fontlog} is missing")
    else:
        problems += fontlog_problems(
            fontlog.read_text(encoding="utf-8"), family.version, release=release
        )
    return problems


def fontlog_problems(text: str, version: str, *, release: bool) -> list[str]:
    """``FONTLOG.txt``'s ChangeLog: the grammar always, the top entry on a release.

    Every build parses the ChangeLog, so a malformed entry is caught by the
    first `mise run qa` after it is written rather than by the release that
    tries to act on it. A build that carries a chosen version is held to one
    more rule (Q10): the entry for *that* version is the newest one, so the
    release notes, the tag and the fonts describe the same change. A ``0.000``
    dev build is not making that claim and is only held to the grammar.
    """
    # Deferred: `package` imports this module for the report names it ships, so
    # the parser it owns can only be reached from inside a function here.
    from asterwell_build import package

    try:
        entries = package.parse_changelog(text)
    except package.PackageError as exc:
        return [str(exc)]
    if not release:
        return []
    if not entries:
        return [
            f"FONTLOG.txt's ChangeLog has no entry, but this build is {version} — "
            f"add `{version} (YYYY-MM-DD): what changed.` at the top of it"
        ]
    if entries[0].version != version:
        return [
            f"FONTLOG.txt's top ChangeLog entry is {entries[0].version}, but this "
            f"build is {version} — the entry for the version being released must "
            "be first"
        ]
    return []


# --------------------------------------------------------------------------- #
# 9.7 fontbakery
# --------------------------------------------------------------------------- #

#: The profile §9.7 gates on. ``check-universal`` is fontbakery's vendor-neutral
#: set; the Google Fonts profile checks catalogue metadata this family has none of.
FONTBAKERY_PROFILE = "check-universal"

#: Where the exclusions and their evidence live.
FONTBAKERY_CONFIG = Path("qa") / "fontbakery.yml"

#: Statuses that fail the command. Everything else is reported and kept.
FONTBAKERY_FATAL = ("ERROR", "FATAL", "FAIL")

#: File classes fontbakery is run over, one invocation each — see the module
#: docstring for why they are not run together.
FONTBAKERY_GROUPS = (KIND_VARIABLE, KIND_STATIC)


@dataclass(frozen=True)
class FontbakeryRun:
    """One fontbakery invocation, read back from its own JSON report."""

    group: str
    counts: Mapping[str, int]
    problems: tuple[str, ...]
    warnings: Mapping[str, int]
    reports: tuple[Path, ...]

    @property
    def summary(self) -> str:
        order = ("ERROR", "FATAL", "FAIL", "WARN", "INFO", "SKIP", "PASS")
        return " ".join(f"{name}: {self.counts.get(name, 0)}" for name in order)


def fontbakery_command(
    *,
    config: Path,
    paths: Sequence[Path],
    report_stem: Path,
    jobs: int | None,
) -> list[str]:
    """The exact argv one invocation runs.

    Built as a function so the arguments are testable without running the tool.
    Two details are worth pinning down. The configuration flag is spelled in
    full: fontbakery 1.1.0 calls it ``--configuration`` and only accepts §9.7's
    ``--config`` because argparse expands unambiguous prefixes — which stops
    being true the day another ``--config…`` option appears, and a dropped
    configuration means the exclusions silently do not apply. And the profile
    runs over one file class at a time; see the module docstring.
    """
    command = [
        sys.executable,
        "-m",
        "fontbakery",
        FONTBAKERY_PROFILE,
        "--configuration",
        str(config),
        "--succinct",
        "--loglevel",
        "WARN",
        "--no-progress",
        "--no-colors",
        "--json",
        f"{report_stem}.json",
        "--ghmarkdown",
        f"{report_stem}.md",
        "--html",
        f"{report_stem}.html",
    ]
    if jobs and jobs > 1:
        command += ["--jobs", str(jobs)]
    return command + [str(path) for path in paths]


def fontbakery_env() -> dict[str, str]:
    """The environment one invocation runs in: ours, plus ``PYTHONHASHSEED=0``.

    Without the seed fontbakery's own message ordering is non-deterministic —
    ``interpolation_issues`` iterates a set and so reports a different *sample*
    of its findings each run — and two runs over identical fonts write reports
    that differ byte for byte. Seeded, they are byte-stable, which is what lets
    the release ship them (:mod:`asterwell_build.package`).
    """
    return {**os.environ, "PYTHONHASHSEED": "0"}


def check_id(check: Mapping[str, object]) -> str:
    """The check id out of a JSON report entry (``<FontBakeryCheck:id>``)."""
    key = check.get("key")
    if isinstance(key, list) and len(key) > 1:
        return str(key[1]).removeprefix("<FontBakeryCheck:").removesuffix(">")
    return str(check.get("module", "?"))


def parse_fontbakery(
    payload: Mapping[str, object],
) -> tuple[dict[str, int], list[str], dict[str, int]]:
    """Read one report: the totals, a line per FAIL/ERROR, and the WARN tally."""
    counts = {
        str(status): int(number)
        for status, number in dict(payload.get("result", {})).items()
        if status != "(not finished)"
    }
    problems: list[str] = []
    warnings: dict[str, int] = {}
    for section in payload.get("sections", []):
        for check in section.get("checks", []):
            identifier = check_id(check)
            status = str(check.get("result"))
            if status == "WARN":
                warnings[identifier] = warnings.get(identifier, 0) + 1
            if status not in FONTBAKERY_FATAL:
                continue
            filename = check.get("filename") or "family"
            messages = [
                str(log.get("message", {}).get("message", "")).replace("\n", " ")
                for log in check.get("logs", [])
                if str(log.get("status")) in FONTBAKERY_FATAL
            ]
            problems.append(
                f"{status} {identifier} on {filename}: "
                f"{summarize([m[:120] for m in messages], 1) or 'no message'}"
            )
    return counts, problems, dict(sorted(warnings.items()))


def run_fontbakery(
    root: Path,
    targets: Sequence[Target],
    *,
    jobs: int | None,
    log: Log,
) -> list[FontbakeryRun]:
    """Run the profile once per file class and read the reports back."""
    config = root / FONTBAKERY_CONFIG
    if not config.is_file():
        raise QaError(f"{config} is missing; the fontbakery exclusions live there")
    report_dir = assemble.fonts_dir_for(root) / QA_DIR
    report_dir.mkdir(parents=True, exist_ok=True)

    runs: list[FontbakeryRun] = []
    for group in FONTBAKERY_GROUPS:
        paths = [target.path for target in targets if target.kind == group]
        if not paths:
            continue
        stem = report_dir / f"fontbakery-{group}"
        command = fontbakery_command(
            config=config, paths=paths, report_stem=stem, jobs=jobs
        )
        log(f"  fontbakery {FONTBAKERY_PROFILE} over {len(paths)} {group} font(s)…")
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=root,
            env=fontbakery_env(),
        )
        report = stem.with_suffix(".json")
        if not report.is_file():
            raise QaError(
                f"fontbakery wrote no report for the {group} fonts "
                f"(exit {completed.returncode}): "
                f"{(completed.stderr or completed.stdout).strip().splitlines()[-1:]}"
            )
        counts, problems, warnings = parse_fontbakery(
            json.loads(report.read_text(encoding="utf-8"))
        )
        if not problems and completed.returncode not in (0, 1):
            problems = [
                f"fontbakery exited {completed.returncode} with no FAIL in its report"
            ]
        runs.append(
            FontbakeryRun(
                group=group,
                counts=counts,
                problems=tuple(problems),
                warnings=warnings,
                reports=(report, stem.with_suffix(".md"), stem.with_suffix(".html")),
            )
        )
    return runs


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


def check(
    root: Path | None = None,
    *,
    skip_fontbakery: bool = False,
    jobs: int | None = None,
    log: Log = print,
) -> Report:
    """Every §9 check over the built family, as one :class:`Report`."""
    # Deferred: see `fontlog_problems`. The version a build carries decides both
    # what the name table must say and whether the FONTLOG gate applies.
    from asterwell_build import package

    root = root if root is not None else upstream.default_root()
    resolved = package.resolve_version(root)
    log(f"version: {resolved}")
    family = assemble.load_family(assemble.family_path_for(root), resolved.version)
    rows = allowlist.read_tsv(allowlist.tsv_path_for(root))
    # The star parameters are read once and used twice: to regenerate what each
    # style's stars must be, and as the rule the two styles are compared by.
    parameters = stars.load_parameters(stars.parameters_path_for(root))
    report = Report()

    references, targets = discover(root, family, parameters, log)
    by_style = {reference.style.key: reference for reference in references}

    report.add(
        "allowlist",
        "sources/allowlist.tsv",
        reasons=(
            ()
            if allowlist.generate(root, check=True, quiet=True) == 0
            else ("the manifest is stale or a DejaVu import overlaps Bitstream Vera; "
                  "run `mise run allowlist`",)
        ),
        note=f"{len(rows)} rows",
    )
    strays = unexpected_files(root, targets)
    report.add(
        "outputs",
        str(assemble.fonts_dir_for(root)),
        reasons=(
            [f"{len(strays)} file(s) the build did not write: {summarize(strays)}"]
            if strays
            else []
        ),
        note=f"{len(targets)} files",
    )
    report.add("licensing", "OFL.txt / DEJAVU-LICENSE.txt / FONTLOG.txt",
               reasons=license_problems(root, family, release=resolved.is_release))

    signatures: dict[str, dict[str, StarSignature]] = {}
    for target in targets:
        reference = by_style[target.style_key]
        data = font_bytes(target)
        font = open_font(data)
        try:
            report.add(
                "metrics",
                target.label,
                reasons=metrics_problems(font, target, reference),
                note=envelope(font) if target.kind == KIND_STATIC else "",
            )
            report.add(
                "coverage",
                target.label,
                reasons=coverage_problems(font, rows, reference.codepoints)
                + component_only_problems(font),
            )
            report.add("names", target.label,
                       reasons=name_problems(font, target, reference, family))
            report.add("bits", target.label, reasons=bit_problems(font, target, family))
            report.add("stars", target.label,
                       reasons=star_problems(font, reference.stars))
            # One signature per (file class, style): the stars carry no `gvar`
            # deltas, so every static of a style has the same ones and the first
            # file of each is a representative — `setdefault`, so which file that
            # is does not depend on iteration order.
            signatures.setdefault(target.kind, {}).setdefault(
                target.style_key,
                star_signature(
                    font, {name: font["hmtx"][name][0] for name in stars.GLYPH_ORDER}
                ),
            )
        finally:
            font.close()
        report.add(
            "shaping",
            target.label,
            reasons=shaping_problems(data, target, reference, rows),
        )

    for kind, per_style in sorted(signatures.items()):
        report.add(
            "stars (roman vs italic)",
            DIRECTORIES[kind],
            reasons=cross_style_star_problems(per_style, kind, parameters),
        )

    if skip_fontbakery:
        log("fontbakery: skipped (--skip-fontbakery)")
    else:
        for run_report in run_fontbakery(root, targets, jobs=jobs, log=log):
            report.add(
                "fontbakery",
                f"{run_report.group} fonts",
                reasons=run_report.problems,
                note=run_report.summary,
            )
            log(f"    reports: {', '.join(p.name for p in run_report.reports)}")
            for identifier, number in run_report.warnings.items():
                log(f"    WARN ×{number:<3} {identifier}")
    return report


def _logger(quiet: bool) -> Log:
    def log(message: str) -> None:
        if not quiet:
            print(message)

    return log


def report_lines(report: Report) -> list[str]:
    """The per-result console output: one line each, failures spelled out."""
    lines: list[str] = []
    for result in report.results:
        status = "PASS" if result.ok else "FAIL"
        suffix = f"  {result.note}" if result.note and result.ok else ""
        lines.append(f"{status}  {result.check:<22} {result.subject}{suffix}")
        for reason in result.reasons:
            lines.append(f"        {reason}")
    return lines


def qa(
    root: Path | None = None,
    *,
    skip_fontbakery: bool = False,
    jobs: int | None = None,
    quiet: bool = False,
) -> int:
    """``asterwell-build qa`` — returns a process exit code."""
    log = _logger(quiet)
    report = check(root, skip_fontbakery=skip_fontbakery, jobs=jobs, log=log)
    for line in report_lines(report):
        log(line)
    passed = len(report.results) - len(report.failures)
    log(f"\n{passed}/{len(report.results)} checks passed")
    if report.failures:
        print(
            f"error: {len(report.failures)} check(s) failed",
            file=sys.stderr,
        )
        return 1
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``qa``'s own options (called from :mod:`asterwell_build.cli`)."""
    parser.add_argument(
        "--skip-fontbakery",
        action="store_true",
        help=(
            "run only this repository's own checks; fontbakery takes minutes and "
            "is the slow half of the gate"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help="fontbakery worker processes (default: one, deterministic ordering)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the per-check report")


def run(args: argparse.Namespace) -> int:
    """``asterwell-build qa`` — see :func:`qa`."""
    # Deferred: see `fontlog_problems`. A malformed ASTERWELL_VERSION is a
    # message, not a traceback, so its error type has to be catchable here.
    from asterwell_build.package import PackageError

    try:
        return qa(
            skip_fontbakery=getattr(args, "skip_fontbakery", False),
            jobs=getattr(args, "jobs", None),
            quiet=getattr(args, "quiet", False),
        )
    except (
        QaError,
        PackageError,
        assemble.AssembleError,
        allowlist.AllowlistError,
        stars.StarsError,
        upstream.UpstreamError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
