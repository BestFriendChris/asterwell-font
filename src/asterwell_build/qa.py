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

*Stars.* The four ornaments have the envelopes
:mod:`asterwell_build.stars` computes, ⁎ ⁑ ⁂ are composites of ``star.small``
alone, and the two *outlines* are byte-identical in the roman and the italic.

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
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import uharfbuzz as hb
from fontTools.ttLib import TTFont, woff2

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
    root: Path, family: Family, log: Log
) -> tuple[list[StyleReference], list[Target]]:
    """Locate every file the build should have written, and read the references.

    Nothing is opened twice and nothing is held open: each reference is a small
    value read from the pinned input and the built variable font, so the 18
    targets can then be checked one at a time.
    """
    fonts_dir = assemble.fonts_dir_for(root)
    references: list[StyleReference] = []
    targets: list[Target] = []
    parameters = stars.load_parameters(stars.parameters_path_for(root))

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


def bit_problems(font: TTFont, target: Target, family: Family) -> list[str]:
    """``head.fontRevision``, the vendor id, and a static's style bits."""
    problems: list[str] = []
    revision = font["head"].fontRevision
    if abs(revision - family.font_revision) > 1e-6:
        problems.append(
            f"head.fontRevision is {revision}, expected {family.font_revision}"
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
    """Simple glyphs: the coordinates, flags and contour ends that ship."""

    components: Mapping[str, tuple[tuple[str, int, int], ...]]
    """Composites: each component's name and its offset **relative to the
    glyph's own centre** (``2·x − advance``), which is the only form in which
    the roman and the italic can agree: ⁎ ⁑ ⁂ inherit the style's own asterisk
    advance, so their absolute offsets differ by exactly half that difference."""


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
            coordinates, end_points, flags = glyph.getCoordinates(glyf)
            outlines[name] = (
                tuple(int(value) for point in coordinates for value in point),
                tuple(int(end) for end in end_points),
                tuple(int(flag) for flag in flags),
            )
    return StarSignature(outlines=outlines, components=components)


def star_problems(font: TTFont, expected: Mapping[str, stars.StarGlyph]) -> list[str]:
    """§9.5: the four ornaments have the envelopes :mod:`stars` computed, and
    ⁎ ⁑ ⁂ are built from ``star.small`` and nothing else."""
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
    return problems


def cross_style_star_problems(
    signatures: Mapping[str, StarSignature], kind: str
) -> list[str]:
    """The stars are one drawing, used twice.

    The two *outlines* — ``star.small`` and ✽ — must be identical in the roman
    and the italic, because an ornament that leaned in the italic would be a
    different ornament (D5). The three *composites* cannot be byte-identical:
    each inherits its style's own asterisk advance, so the shared component sits
    at a different absolute offset. What must match is the arrangement, which is
    what :class:`StarSignature` normalises.
    """
    keys = sorted(signatures)
    if len(keys) < 2:
        return []
    first, second = signatures[keys[0]], signatures[keys[1]]
    problems = [
        f"{name} differs between {keys[0]} and {keys[1]} ({kind})"
        for name in sorted(first.outlines)
        if first.outlines.get(name) != second.outlines.get(name)
    ]
    problems += [
        f"{name} is arranged differently in {keys[0]} and {keys[1]} ({kind}): "
        f"{first.components.get(name)} vs {second.components.get(name)}"
        for name in sorted(first.components)
        if first.components.get(name) != second.components.get(name)
    ]
    return problems


# --------------------------------------------------------------------------- #
# 9.6 Licensing
# --------------------------------------------------------------------------- #


def license_problems(root: Path, family: Family) -> list[str]:
    """The licence files say what the fonts claim they say.

    The OFL body is not paraphrased or re-typed: it is the pinned upstream file
    from its line of dashes onward, compared line for line, because a licence
    with a typo in it is a licence nobody can rely on.
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

    fontlog = root / "FONTLOG.txt"
    if not fontlog.is_file():
        problems.append(f"{fontlog} is missing")
    elif family.version not in fontlog.read_text(encoding="utf-8"):
        problems.append(f"FONTLOG.txt never mentions version {family.version}")
    return problems


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
    root = root if root is not None else upstream.default_root()
    family = assemble.load_family(assemble.family_path_for(root))
    rows = allowlist.read_tsv(allowlist.tsv_path_for(root))
    report = Report()

    references, targets = discover(root, family, log)
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
               reasons=license_problems(root, family))

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
            reasons=cross_style_star_problems(per_style, kind),
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
    try:
        return qa(
            skip_fontbakery=getattr(args, "skip_fontbakery", False),
            jobs=getattr(args, "jobs", None),
            quiet=getattr(args, "quiet", False),
        )
    except (
        QaError,
        assemble.AssembleError,
        allowlist.AllowlistError,
        stars.StarsError,
        upstream.UpstreamError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
