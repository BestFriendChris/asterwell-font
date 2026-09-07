"""Assemble one Asterwell Text variable font per style.

Each of Literata's two variable fonts is opened, extended and written back out
under the family's own name. Nothing is re-drawn that Literata already draws:
the prose face, its two axes, its layout tables and every vertical metric come
through untouched, and the only edits are additive.

Three kinds of glyph enter a font here:

*Preserved* — everything Literata already has, which is the whole prose face.
Not redrawn, not re-scaled, not rounded a second time: every point, flag and
contour of a preserved glyph, and every one of its ``gvar`` deltas, is the
upstream release's. (The *containers* are recompiled, as they must be — adding
508 glyphs pushes ``glyf`` past the 128 KB that a short ``loca`` can index, so
``head.indexToLocFormat`` flips to 1 and the pad byte on odd-length glyphs goes
away. Outlines, not bytes, are what "untouched" means here.)

*Custom* — the four stars :mod:`asterwell_build.stars` draws (✽ ⁎ ⁑ ⁂) plus the
unencoded ``star.small`` they share. The first four are appended to the glyph
order; ⁂ replaces Literata's own ``uni2042`` **in place**, keeping its glyph ID,
its advance and its cmap entry, and losing its ``gvar`` entry so it stops
varying with weight (decision D8 — the stars are one fixed design, D5).

*Imported* — every row of ``sources/allowlist.tsv`` whose source is ``dejavu``.
The DejaVu outline is drawn through a decomposing pen (so a composite arrives as
one flat outline, D7), replayed through a uniform scale of
``k = Literata sCapHeight ÷ DejaVu H-height = 700/1493`` (D6) and rounded to
integers. One override in ``sources/allowlist.toml`` overrides the uniform
scale: ◦ U+25E6 is fitted to Literata's own • instead, since a bullet's size is
a typographic decision rather than a cap-height one.

The delicate parts, in the order they bite:

1. **Force-read every table before touching the glyph order** (D11). ``gvar``,
   ``HVAR``, ``VVAR``, ``hmtx``, ``vmtx``, ``loca`` and ``post`` all size
   themselves against the glyph count when they are first decompiled, and
   ``gvar.decompile`` *asserts* that count. Appending a glyph before those
   tables have been read makes the assert fire; ``lazy=False`` does not help,
   because the tables are still decompiled on demand.
2. **Advance variations must be rebuilt, not edited.** ``HVAR`` and ``VVAR``
   map every glyph ID to an advance-delta index, so they are wrong the moment
   the glyph order grows. Both are deleted and regenerated from ``gvar``; a
   glyph with no ``gvar`` entry (every glyph this module adds) comes out with a
   constant advance, which is exactly D5's promise that symbols do not move
   when the weight does.
3. **Overlap removal runs only on imported glyphs.** They are the ones drawn
   from another font's outlines with no variation data. Running it on a
   Literata glyph would silently desynchronise that glyph's points from its
   ``gvar`` deltas.

What the build must *not* change is asserted rather than hoped for:
:func:`invariants` is taken from the Literata input before any edit and
compared against the reloaded output — vertical metrics, ``head`` bounding box,
``fvar``, ``avar``, ``MVAR``, and the ``GSUB``/``GPOS`` feature lists.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import shutil
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from fontTools.misc.roundTools import otRound
from fontTools.misc.timeTools import timestampSinceEpoch
from fontTools.misc.transform import Transform
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.removeOverlaps import removeOverlaps
from fontTools.ttLib.tables._g_l_y_f import Glyph
from fontTools.varLib import _add_VHVAR
from fontTools.varLib import hvar as hvar_module

from asterwell_build import allowlist, stars, upstream

Log = Callable[[str], None]

__all__ = [
    "AssembleError",
    "Family",
    "Invariants",
    "Style",
    "STYLES",
    "assemble",
    "build",
    "build_info",
    "import_scale",
    "invariants",
    "load_family",
    "open_source_font",
    "source_date_epoch",
    "write_build_info",
]


class AssembleError(Exception):
    """The inputs cannot be assembled into the family this repository promises."""


# --------------------------------------------------------------------------- #
# Styles
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Style:
    """One shipped variable font: which Literata file it starts from, and the
    naming the finished font carries."""

    key: str
    """``roman`` / ``italic`` — also the key :func:`stars.build_glyphs` takes."""

    member: str
    """Basename of the pinned Literata member this style is built from."""

    subfamily: str
    """Name ID 2, and the suffix in the PostScript name (IDs 3 and 6)."""

    postscript_suffix: str
    """Appended to ``ps_family`` for name ID 25 (the variations PS prefix)."""

    file_suffix: str
    """Appended to ``ps_family`` in the output filename."""


STYLES: tuple[Style, ...] = (
    Style("roman", "Literata[opsz,wght].ttf", "Regular", "", ""),
    Style("italic", "Literata-Italic[opsz,wght].ttf", "Italic", "Italic", "-Italic"),
)

#: Basename of the pinned donor. Only the Regular is ever opened (D2).
DEJAVU_MEMBER = "DejaVuSans.ttf"

#: The donor glyph whose bounding-box height defines the import scale: DejaVu
#: `OS/2` is version 1 and carries no ``sCapHeight``, so its cap height is
#: measured from ``H`` (1493 units at ``unitsPerEm`` 2048).
SCALE_REFERENCE_GLYPH = "H"

#: What ``k`` must come out as for the pinned pair (700 ÷ 1493). Checked, not
#: used: the scale is measured from the fonts so a pin bump cannot go unnoticed.
EXPECTED_SCALE = (700, 1493)

#: ``GDEF.GlyphClassDef`` value for a base glyph. Every glyph this module adds
#: is one: no marks, no ligatures, no components of a mark.
GDEF_CLASS_BASE = 1

#: ``STAT`` axis-value flag that hides a value's name in an instance name, so
#: the ``opsz=12`` statics are "Bold" and not "12pt Bold" (D19).
ELIDABLE_AXIS_VALUE_NAME = 0x0002

#: The Windows/Unicode/English name record every string is written to. Mac
#: records are dropped wholesale (D18), so this is the only platform left.
WINDOWS_ENGLISH = (3, 1, 0x409)

#: Name IDs this module writes. ID 7 is deleted rather than written: it carries
#: Literata's Google trademark string, which must not travel. ID 12 (the base
#: designers' URL) and the style/axis names ≥ 256 are left exactly as they are.
NAME_IDS = (0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 25)

#: Appended to Literata's own name ID 9 (its designer credit, kept verbatim).
STAR_CREDIT = "star ornaments by {holder}"

#: Notice sentences the DejaVu license requires to travel with any glyph
#: derived from it; reproduced verbatim from ``DEJAVU-LICENSE.txt``.
DEJAVU_NOTICE = (
    "Portions from DejaVu Sans: Copyright (c) 2003 by Bitstream, Inc. "
    "All Rights Reserved. Bitstream Vera is a trademark of Bitstream, Inc. "
    "Copyright (c) 2006 by Tavmjong Bah. All Rights Reserved. "
    "DejaVu changes are in public domain."
)

#: Upstream copyright line for the base family, as its own name ID 0 states it.
LITERATA_NOTICE = (
    "Portions Copyright 2017 The Literata Project Authors "
    "(https://github.com/googlefonts/literata)."
)

#: Name ID 13, templated with ``license_url``.
LICENSE_DESCRIPTION = (
    "This Font Software is licensed under the SIL Open Font License, "
    "Version 1.1. This license is available with a FAQ at: {license_url}. "
    "Glyphs derived from DejaVu Sans are used under the DejaVu Fonts license; "
    "see DEJAVU-LICENSE.txt distributed with this font."
)

#: Name ID 10, templated with ``family`` and ``repo_url``.
DESCRIPTION = (
    "{family} is a derivative of Literata by TypeTogether, extended with "
    "selected symbols and ornaments from DejaVu Sans and an original "
    "six-petal star family. Glyph provenance: {repo_url}"
)


# --------------------------------------------------------------------------- #
# Family metadata (sources/family.toml)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Family:
    """What ``sources/family.toml`` says the family is called and claims."""

    family: str
    ps_family: str
    version: str
    vendor_id: str
    repo_url: str
    license_url: str
    copyright_holder: str
    copyright_year: str
    reserved_font_name: str

    @property
    def holder(self) -> str:
        """The holder without its parenthesised URL — how a credit reads it."""
        return re.sub(r"\s*\(\s*https?://[^)]*\)\s*$", "", self.copyright_holder)

    @property
    def font_revision(self) -> float:
        """``head.fontRevision``: the version as a number (``1.000`` → 1.0)."""
        try:
            return float(self.version)
        except ValueError as exc:  # pragma: no cover - guarded by load_family
            raise AssembleError(f"version {self.version!r} is not a number") from exc

    @property
    def copyright(self) -> str:
        """Name ID 0: our notice, then every upstream notice we must carry."""
        reserved = (
            f', with Reserved Font Name "{self.reserved_font_name}"'
            if self.reserved_font_name
            else ""
        )
        return (
            f"Copyright {self.copyright_year} {self.copyright_holder}{reserved}. "
            f"{LITERATA_NOTICE} {DEJAVU_NOTICE}"
        )

    def vendor_bytes(self) -> str:
        """``OS/2.achVendID`` is a fixed four-character field."""
        return self.vendor_id.ljust(4)[:4]


def family_path_for(root: Path) -> Path:
    return root / "sources" / "family.toml"


def fonts_dir_for(root: Path) -> Path:
    """Where everything the build ships is written (gitignored, Q3)."""
    return root / "fonts"


def load_family(path: Path) -> Family:
    """Parse and validate ``family.toml``."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssembleError(f"missing family file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AssembleError(f"{path}: not valid TOML: {exc}") from exc

    def string(key: str, *, allow_empty: bool = False) -> str:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = f"{value:g}" if isinstance(value, float) else str(value)
        if not isinstance(value, str) or (not value and not allow_empty):
            raise AssembleError(f"{path}: `{key}` must be a non-empty string")
        return value

    family = Family(
        family=string("family"),
        ps_family=string("ps_family"),
        version=string("version"),
        vendor_id=string("vendor_id"),
        repo_url=string("repo_url"),
        license_url=string("license_url"),
        copyright_holder=string("copyright_holder"),
        copyright_year=string("copyright_year"),
        reserved_font_name=string("reserved_font_name", allow_empty=True),
    )
    try:
        float(family.version)
    except ValueError as exc:
        raise AssembleError(
            f"{path}: `version` must be a number like \"1.000\", got {family.version!r}"
        ) from exc
    if len(family.vendor_id) > 4:
        raise AssembleError(
            f"{path}: `vendor_id` must be at most four characters, got {family.vendor_id!r}"
        )
    if " " in family.ps_family:
        raise AssembleError(
            f"{path}: `ps_family` is a PostScript name and cannot contain spaces"
        )
    return family


# --------------------------------------------------------------------------- #
# Opening the inputs
# --------------------------------------------------------------------------- #


def pinned_member(root: Path, key: str, basename: str) -> Path:
    """Locate one pinned archive member by basename, keeping the version in
    ``sources/upstream.toml`` and out of this module."""
    pins = {pin.key: pin for pin in upstream.load_pins(upstream.pin_file_for(root))}
    pin = pins.get(key)
    if pin is None:
        raise AssembleError(f"sources/upstream.toml has no [{key}] section")
    matches = [name for name in pin.members if name.rsplit("/", 1)[-1] == basename]
    if len(matches) != 1:
        raise AssembleError(
            f"sources/upstream.toml [{key}.members] must pin exactly one "
            f"{basename}; found {len(matches)}"
        )
    path = pin.extract_dir(upstream.upstream_dir_for(root)) / matches[0]
    if not path.is_file():
        raise AssembleError(f"{path} is missing — run `mise run fetch`")
    return path


def open_source_font(path: Path) -> TTFont:
    """Open a font and decompile **every** table before anything can grow the
    glyph order (D11).

    ``gvar.decompile`` asserts ``len(glyphOrder) == glyphCount``; ``hmtx``,
    ``vmtx``, ``HVAR``, ``VVAR``, ``loca`` and ``post`` all read their length
    from the glyph count as well. Reading them here — while that count is still
    the file's own — is what makes appending a glyph afterwards safe.

    ``recalcTimestamp=False`` keeps ``head.modified`` under this module's
    control rather than fontTools' clock; see :func:`stamp_timestamp`.
    """
    font = TTFont(path, recalcTimestamp=False)
    for tag in font.keys():
        if tag != "GlyphOrder":
            font[tag]  # noqa: B018 — decompiling is the point
    return font


def unicode_cmap(font: TTFont) -> dict[int, str]:
    """Union of a font's Unicode ``cmap`` subtables, code point → glyph name."""
    mapping: dict[int, str] = {}
    for subtable in font["cmap"].tables:
        if subtable.isUnicode():
            mapping.update(subtable.cmap)
    return mapping


def import_scale(literata: TTFont, dejavu: TTFont) -> float:
    """``k``: the factor that puts DejaVu's cap height on Literata's (D6).

    Measured from the two fonts rather than hard-coded, so a pin bump that
    moves either cap height changes the scale instead of silently mis-sizing
    every import; the pinned pair must still give 700/1493.
    """
    cap_height = literata["OS/2"].sCapHeight
    reference = dejavu["glyf"][SCALE_REFERENCE_GLYPH]
    donor_height = reference.yMax - reference.yMin
    if cap_height <= 0 or donor_height <= 0:
        raise AssembleError(
            f"cannot measure the import scale: Literata sCapHeight {cap_height}, "
            f"DejaVu {SCALE_REFERENCE_GLYPH} height {donor_height}"
        )
    expected_cap, expected_donor = EXPECTED_SCALE
    if (cap_height, donor_height) != (expected_cap, expected_donor):
        raise AssembleError(
            f"import scale changed: Literata sCapHeight {cap_height} (expected "
            f"{expected_cap}) and DejaVu {SCALE_REFERENCE_GLYPH} height "
            f"{donor_height} (expected {expected_donor}); re-review the pins"
        )
    return cap_height / donor_height


# --------------------------------------------------------------------------- #
# The invariants
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Invariants:
    """Everything the build promises not to change, read off one font.

    Taken from the Literata input before any edit and re-read from the finished
    file; :func:`Invariants.differences` is what turns a silent metric drift
    into a build failure. ``qa`` (Step 8) checks the same values against the
    shipped files.
    """

    ascent: int
    descent: int
    line_gap: int
    typo_ascender: int
    typo_descender: int
    typo_line_gap: int
    win_ascent: int
    win_descent: int
    fs_selection: int
    advance_width_max: int
    head_bbox: tuple[int, int, int, int]
    units_per_em: int
    fvar_axes: tuple[tuple[str, float, float, float], ...]
    fvar_instances: tuple[tuple[int, tuple[tuple[str, float], ...]], ...]
    mvar: tuple[str, ...]
    avar: tuple[tuple[str, tuple[tuple[float, float], ...]], ...]
    gsub_features: tuple[str, ...]
    gpos_features: tuple[str, ...]

    def differences(self, other: Invariants) -> list[str]:
        """Human-readable list of every field that moved; empty means frozen."""
        return [
            f"{name}: {getattr(self, name)!r} → {getattr(other, name)!r}"
            for name in self.__dataclass_fields__
            if getattr(self, name) != getattr(other, name)
        ]


def invariants(font: TTFont) -> Invariants:
    """Read the frozen values off a font.

    Every variation table is read through ``font.get`` so that the same reader
    measures a *static* instance, where ``fvar``, ``avar`` and ``MVAR`` are gone
    by design: instancing empties those three fields and leaves every other one
    alone, which is exactly the shape of the check
    :mod:`asterwell_build.instances` runs on the 16 statics.
    """
    hhea, os2, head = font["hhea"], font["OS/2"], font["head"]
    fvar = font.get("fvar")
    avar = font.get("avar")
    mvar = font.get("MVAR")
    return Invariants(
        ascent=hhea.ascent,
        descent=hhea.descent,
        line_gap=hhea.lineGap,
        typo_ascender=os2.sTypoAscender,
        typo_descender=os2.sTypoDescender,
        typo_line_gap=os2.sTypoLineGap,
        win_ascent=os2.usWinAscent,
        win_descent=os2.usWinDescent,
        fs_selection=os2.fsSelection,
        advance_width_max=hhea.advanceWidthMax,
        head_bbox=(head.xMin, head.yMin, head.xMax, head.yMax),
        units_per_em=head.unitsPerEm,
        fvar_axes=(
            tuple((a.axisTag, a.minValue, a.defaultValue, a.maxValue) for a in fvar.axes)
            if fvar is not None
            else ()
        ),
        fvar_instances=(
            tuple(
                (i.subfamilyNameID, tuple(sorted(i.coordinates.items())))
                for i in fvar.instances
            )
            if fvar is not None
            else ()
        ),
        mvar=(
            tuple(record.ValueTag for record in mvar.table.ValueRecord)
            if mvar is not None
            else ()
        ),
        avar=(
            tuple((tag, tuple(sorted(seg.items()))) for tag, seg in sorted(avar.segments.items()))
            if avar is not None
            else ()
        ),
        gsub_features=_feature_tags(font, "GSUB"),
        gpos_features=_feature_tags(font, "GPOS"),
    )


def _feature_tags(font: TTFont, tag: str) -> tuple[str, ...]:
    """The feature tags of a layout table, or none when the table is absent —
    a table that disappears reads as an emptied feature list, which is a
    difference the caller will report either way."""
    table = font.get(tag)
    if table is None:
        return ()
    return tuple(sorted({r.FeatureTag for r in table.table.FeatureList.FeatureRecord}))


def vertical_advance(font: TTFont) -> int:
    """The vertical advance Literata gives every drawn glyph (1485).

    Read from the font rather than taken on faith. Literata's own ``vmtx`` holds
    exactly two values: this one for every glyph with an outline, and 0 for the
    eight blank ones — a rule this module then extends to the glyphs it adds.
    """
    heights = {
        advance for advance, _tsb in font["vmtx"].metrics.values() if advance != 0
    }
    if len(heights) != 1:
        raise AssembleError(
            f"expected one vertical advance for all drawn glyphs, found {sorted(heights)}"
        )
    return heights.pop()


def top_side_bearing(font: TTFont, glyph: Glyph) -> int:
    """Literata's own rule, verified across all 1789/1769 glyphs:
    ``tsb = hhea.ascent − yMax`` (with ``yMax`` 0 for an empty glyph)."""
    y_max = glyph.yMax if glyph.numberOfContours != 0 else 0
    return font["hhea"].ascent - y_max


# --------------------------------------------------------------------------- #
# Glyph-order surgery
# --------------------------------------------------------------------------- #


def append_glyph(font: TTFont, name: str, glyph: Glyph, advance: int, lsb: int) -> None:
    """Add one glyph at the end of the glyph order, with its horizontal metrics.

    ``vmtx``, ``GDEF`` and ``cmap`` are filled in later, in one pass over every
    added glyph (:func:`finish_new_glyphs`), because they are the same three
    edits whether the glyph is custom or imported.
    """
    if name in font["glyf"].glyphs:
        raise AssembleError(f"glyph {name!r} is already in the font")
    order = font.getGlyphOrder()
    order.append(name)
    # Re-setting the (same) list drops TTFont's reverse-map cache and keeps the
    # glyf table's own copy of the order pointed at it.
    font.setGlyphOrder(order)
    font["glyf"].glyphs[name] = glyph
    font["hmtx"][name] = (advance, lsb)
    font["maxp"].numGlyphs = len(order)


def replace_glyph(font: TTFont, name: str, glyph: Glyph, advance: int, lsb: int) -> None:
    """Swap one glyph's outline without moving it in the glyph order.

    The glyph ID, the cmap entry that points at it and every layout lookup that
    names it survive; its ``gvar`` entry does not, because the replacement is a
    fixed design that must not vary with the axes (D8, D5).
    """
    if name not in font["glyf"].glyphs:
        raise AssembleError(f"glyph {name!r} is not in the font, so it cannot be replaced")
    font["glyf"].glyphs[name] = glyph
    font["hmtx"][name] = (advance, lsb)
    font["gvar"].variations.pop(name, None)


def set_vertical_metrics(font: TTFont, names: Iterable[str]) -> None:
    """Write ``vmtx`` for the named glyphs, following Literata's own rule.

    Called again after overlap removal, because a simplified outline can have a
    different ``yMax`` and the top side bearing is measured from it.
    """
    advance = vertical_advance(font)
    glyf = font["glyf"]
    for name in names:
        glyph = glyf[name]
        drawn = glyph.numberOfContours != 0
        font["vmtx"][name] = (advance if drawn else 0, top_side_bearing(font, glyph))


def finish_new_glyphs(
    font: TTFont, names: Sequence[str], codepoints: Mapping[str, int]
) -> None:
    """Give every added or replaced glyph its vertical metrics, glyph class and
    cmap entry.

    Vertical metrics come first because ``font.getGlyphSet()`` — which overlap
    removal goes through — reads ``vmtx`` for every glyph it touches.
    """
    set_vertical_metrics(font, names)
    class_defs = font["GDEF"].table.GlyphClassDef.classDefs
    for name in names:
        class_defs[name] = GDEF_CLASS_BASE
    add_cmap_entries(font, codepoints)


def check_side_bearings(font: TTFont, names: Iterable[str]) -> None:
    """Every drawn glyph in this font has ``lsb == xMin``; keep it that way.

    A glyph whose recorded left side bearing disagrees with its outline sets its
    origin somewhere other than x=0, which shows up as a shifted symbol rather
    than as an error, so it is worth one pass.
    """
    glyf, hmtx = font["glyf"], font["hmtx"]
    wrong = [
        name
        for name in names
        if glyf[name].numberOfContours != 0 and hmtx[name][1] != glyf[name].xMin
    ]
    if wrong:
        raise AssembleError(
            "left side bearing does not match the outline for: " + ", ".join(wrong)
        )


def add_cmap_entries(font: TTFont, codepoints: Mapping[str, int]) -> None:
    """Map each code point to its glyph in every Unicode ``cmap`` subtable.

    Both of Literata's subtables are format 4, which cannot address anything
    above the BMP. The allowlist generator already refuses a supplementary code
    point; this is the second gate, stated in terms of the table that would
    have silently dropped it.
    """
    subtables = [s for s in font["cmap"].tables if s.isUnicode()]
    if not subtables:
        raise AssembleError("the font has no Unicode cmap subtable")
    for name, codepoint in codepoints.items():
        for subtable in subtables:
            if subtable.format == 4 and codepoint > 0xFFFF:
                raise AssembleError(
                    f"U+{codepoint:04X} ({name}) is outside the BMP and cannot go "
                    f"into a format-4 cmap subtable; the family is BMP-only"
                )
            subtable.cmap[codepoint] = name


# --------------------------------------------------------------------------- #
# The custom stars
# --------------------------------------------------------------------------- #


def add_custom_glyphs(
    font: TTFont, star_glyphs: Mapping[str, stars.StarGlyph]
) -> tuple[list[str], list[str], dict[str, int]]:
    """Append ``star.small`` ✽ ⁎ ⁑ and replace ⁂ in place.

    Returns ``(appended, replaced, codepoints)`` — the third being the cmap
    entries these glyphs need, which is every star but ``star.small`` (it is a
    component, reachable only through the composites that use it).
    """
    appended: list[str] = []
    replaced: list[str] = []
    codepoints: dict[str, int] = {}
    existing = unicode_cmap(font)
    for name, star in star_glyphs.items():
        if name in font["glyf"].glyphs:
            # Literata's own ⁂: same glyph ID, new outline (D8).
            if star.codepoint is not None and existing.get(star.codepoint) != name:
                raise AssembleError(
                    f"expected U+{star.codepoint:04X} to already map to {name!r}, "
                    f"found {existing.get(star.codepoint)!r}"
                )
            replace_glyph(font, name, star.glyph, star.advance, star.lsb)
            replaced.append(name)
        else:
            append_glyph(font, name, star.glyph, star.advance, star.lsb)
            appended.append(name)
        if star.codepoint is not None:
            codepoints[name] = star.codepoint
    return appended, replaced, codepoints


# --------------------------------------------------------------------------- #
# The DejaVu imports
# --------------------------------------------------------------------------- #


def _control_bounds(recording: DecomposingRecordingPen) -> tuple[float, float, float, float]:
    """Bounds over every point a recording touches, control points included —
    which is what ``glyf`` stores as the glyph's bounding box."""
    xs: list[float] = []
    ys: list[float] = []
    for _operator, arguments in recording.value:
        for point in arguments:
            if point is None:  # a qCurveTo of nothing but off-curve points
                continue
            if isinstance(point, tuple) and len(point) == 2:
                xs.append(point[0])
                ys.append(point[1])
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def record_glyph(glyph_set: Mapping[str, object], name: str) -> DecomposingRecordingPen:
    """Draw one donor glyph, flattening any composite into plain contours (D7)."""
    recording = DecomposingRecordingPen(glyph_set)
    glyph_set[name].draw(recording)  # type: ignore[attr-defined]
    return recording


def replay_transformed(recording: DecomposingRecordingPen, transform: Transform) -> Glyph:
    """Replay a recording through a transform into a rounded TrueType glyph."""
    pen = TTGlyphPen(None)
    recording.replay(TransformPen(pen, transform))
    glyph = pen.glyph()
    glyph.recalcBounds(glyfTable=None)
    return glyph


def match_metrics_transform(
    recording: DecomposingRecordingPen, reference: tuple[int, int, int, int]
) -> Transform:
    """The transform that fits a donor outline into a reference glyph's box.

    Used for the one ``[overrides]`` entry: ◦ U+25E6 is a bullet, and a bullet's
    size is set by the family's own • rather than by cap height. The outline is
    scaled so its height matches the reference's, then centred on the
    reference's box; the caller copies the reference's advance too.
    """
    x_min, y_min, x_max, y_max = _control_bounds(recording)
    height = y_max - y_min
    if height <= 0:
        raise AssembleError("cannot match metrics of a glyph with no height")
    ref_x_min, ref_y_min, ref_x_max, ref_y_max = reference
    scale = (ref_y_max - ref_y_min) / height
    dx = (ref_x_min + ref_x_max) / 2 - scale * (x_min + x_max) / 2
    dy = (ref_y_min + ref_y_max) / 2 - scale * (y_min + y_max) / 2
    return Transform().translate(dx, dy).scale(scale)


@dataclass(frozen=True)
class Override:
    """One ``[overrides]`` instruction, resolved against the font being built."""

    codepoint: int
    reference_name: str
    bounds: tuple[int, int, int, int]
    advance: int


def resolve_overrides(
    font: TTFont, rules: allowlist.Rules, rows: Sequence[allowlist.Row]
) -> dict[int, Override]:
    """Turn ``match_metrics_of`` code points into the reference glyph's real box.

    The reference has to be a glyph the font already has at this point — the
    imports have not run yet, so it is a preserved Literata glyph or a star.
    """
    by_codepoint = {row.codepoint: row for row in rows}
    resolved: dict[int, Override] = {}
    for codepoint, override in rules.overrides.items():
        reference = override.get("match_metrics_of")
        if reference is None:
            raise AssembleError(
                f"U+{codepoint:04X}: unsupported override {sorted(override)}"
            )
        reference_cp = allowlist.parse_codepoint(reference, "sources/allowlist.toml")
        row = by_codepoint.get(reference_cp)
        if row is None:
            raise AssembleError(
                f"U+{codepoint:04X} matches the metrics of U+{reference_cp:04X}, "
                f"which is not in sources/allowlist.tsv"
            )
        if row.glyph_name not in font["glyf"].glyphs:
            raise AssembleError(
                f"U+{codepoint:04X} matches the metrics of {row.glyph_name!r}, "
                f"which the font does not have"
            )
        # Through the table, not the dict: that is what expands a glyph whose
        # outline has not been decompiled yet, and so what gives it a bbox.
        glyph = font["glyf"][row.glyph_name]
        resolved[codepoint] = Override(
            codepoint=codepoint,
            reference_name=row.glyph_name,
            bounds=(glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax),
            advance=font["hmtx"][row.glyph_name][0],
        )
    return resolved


def import_dejavu_glyphs(
    font: TTFont,
    dejavu: TTFont,
    rows: Sequence[allowlist.Row],
    scale: float,
    overrides: Mapping[int, Override],
) -> tuple[list[str], dict[str, int]]:
    """Import every ``dejavu`` row, in file order.

    Returns ``(names, codepoints)``. Overlap removal is left to the caller so
    it runs once, over exactly this list.
    """
    glyph_set = dejavu.getGlyphSet()
    donor_cmap = unicode_cmap(dejavu)
    donor_metrics = dejavu["hmtx"].metrics
    existing = unicode_cmap(font)
    uniform = Transform().scale(scale)

    names: list[str] = []
    codepoints: dict[str, int] = {}
    for row in rows:
        if row.source != allowlist.SOURCE_DEJAVU:
            continue
        donor_name = donor_cmap.get(row.codepoint)
        if donor_name is None:
            raise AssembleError(
                f"U+{row.codepoint:04X} is marked `dejavu` but the pinned "
                f"DejaVu Sans does not map it; re-run `mise run allowlist`"
            )
        if row.glyph_name in font["glyf"].glyphs:
            raise AssembleError(
                f"U+{row.codepoint:04X} would be imported as {row.glyph_name!r}, "
                f"a glyph name the font already uses"
            )
        if row.codepoint in existing:
            raise AssembleError(
                f"U+{row.codepoint:04X} is marked `dejavu` but Literata already "
                f"maps it to {existing[row.codepoint]!r}; re-run `mise run allowlist`"
            )
        recording = record_glyph(glyph_set, donor_name)
        override = overrides.get(row.codepoint)
        if override is None:
            glyph = replay_transformed(recording, uniform)
            advance = otRound(donor_metrics[donor_name][0] * scale)
        else:
            glyph = replay_transformed(
                recording, match_metrics_transform(recording, override.bounds)
            )
            advance = override.advance
        lsb = glyph.xMin if glyph.numberOfContours != 0 else 0
        append_glyph(font, row.glyph_name, glyph, advance, lsb)
        names.append(row.glyph_name)
        codepoints[row.glyph_name] = row.codepoint
    return names, codepoints


# --------------------------------------------------------------------------- #
# Variation tables
# --------------------------------------------------------------------------- #


def regenerate_hvar(font: TTFont) -> None:
    """Rebuild ``HVAR`` from ``gvar`` for the new glyph order."""
    if "HVAR" in font:
        del font["HVAR"]
    hvar_module.add_HVAR(font)


def regenerate_vvar(font: TTFont) -> None:
    """Rebuild ``VVAR`` from ``gvar``, around a fontTools bug.

    ``fontTools.varLib.hvar.add_VVAR`` is broken in 4.64.0: it builds the
    ``partial(...)`` that captures ``axisTags`` on the line *before* ``axisTags``
    is assigned, so every call raises ``UnboundLocalError: cannot access local
    variable 'axisTags'``. ``add_HVAR``, which is otherwise the same function,
    has the two lines the right way round. This is that function with the order
    fixed — nothing else.

    ``tests/test_assemble.py::test_upstream_add_vvar_is_still_broken`` calls the
    real ``add_VVAR`` and asserts it still raises. When a fontTools upgrade makes
    that test fail, the fix has landed upstream and this helper can go.
    """
    if "VVAR" in font:
        del font["VVAR"]
    axis_tags = [axis.axisTag for axis in font["fvar"].axes]
    _add_VHVAR(
        font,
        axis_tags,
        hvar_module.VVAR_FIELDS,
        partial(
            hvar_module._get_advance_metrics, font, axis_tags, hvar_module.VVAR_FIELDS
        ),
    )


# --------------------------------------------------------------------------- #
# Font-wide tables
# --------------------------------------------------------------------------- #


def update_os2(font: TTFont, family: Family) -> None:
    """Recompute what the new coverage changed, and stamp the vendor."""
    os2 = font["OS/2"]
    os2.recalcUnicodeRanges(font)
    os2.recalcAvgCharWidth(font)
    codepoints = unicode_cmap(font)
    os2.usLastCharIndex = min(max(codepoints), 0xFFFF)
    os2.achVendID = family.vendor_bytes()


def set_optical_size_elidable(font: TTFont, value: float = 12.0) -> int:
    """Mark the ``opsz`` axis value at ``value`` elidable (D19).

    Without this the static instances are named "12pt Bold" rather than "Bold",
    because ``instantiateVariableFont(updateFontNames=True)`` spells out every
    non-elided axis value. Returns how many axis values were changed.
    """
    stat = font["STAT"].table
    axes = [axis.AxisTag for axis in stat.DesignAxisRecord.Axis]
    array = stat.AxisValueArray
    changed = 0
    for axis_value in array.AxisValue if array is not None else []:
        index = getattr(axis_value, "AxisIndex", None)
        if index is None or axes[index] != "opsz":
            continue
        if getattr(axis_value, "Value", None) != value:
            continue
        axis_value.Flags |= ELIDABLE_AXIS_VALUE_NAME
        changed += 1
    if changed != 1:
        raise AssembleError(
            f"expected exactly one STAT opsz axis value at {value:g}, found {changed}"
        )
    return changed


def source_date_epoch() -> int | None:
    """``SOURCE_DATE_EPOCH`` as an integer, or ``None`` when the build sets none.

    One reader for the two places the value is used: the timestamp stamped into
    every font, and the value recorded in ``BUILD-INFO.json``.
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if not epoch:
        return None
    try:
        return int(epoch)
    except ValueError as exc:
        raise AssembleError(f"SOURCE_DATE_EPOCH is not an integer: {epoch!r}") from exc


def stamp_timestamp(font: TTFont) -> None:
    """``head.modified`` from ``SOURCE_DATE_EPOCH`` when the build sets one.

    ``head.created`` is never touched: the family's outlines are Literata's, and
    so is the date they were first drawn.
    """
    epoch = source_date_epoch()
    if epoch is None:
        return
    font["head"].modified = timestampSinceEpoch(epoch)


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


def name_strings(font: TTFont, family: Family, style: Style) -> dict[int, str]:
    """The name records this family writes, ID → string.

    Everything the family says about itself is here; anything not in this table
    (ID 12, and the axis/instance style names ≥ 256 that ``fvar`` and ``STAT``
    point at) is Literata's and stays as it is. ID 7 is absent because it is
    deleted rather than rewritten.
    """
    name_table = font["name"]
    designer = name_table.getDebugName(9) or ""
    credit = STAR_CREDIT.format(holder=family.holder)
    postscript = f"{family.ps_family}-{style.subfamily}"
    return {
        0: family.copyright,
        1: family.family,
        2: style.subfamily,
        3: f"{family.version};{family.vendor_id};{postscript}",
        4: f"{family.family} {style.subfamily}",
        5: f"Version {family.version}",
        6: postscript,
        8: family.holder,
        # Literata's designer credit, kept and extended. Its roman ends in a
        # full stop and its italic does not, so the stop comes off before the
        # semicolon: "…Evstafieva.; star ornaments…" would be the alternative,
        # and both styles end up saying the same thing.
        9: f"{designer.rstrip('.')}; {credit}" if designer else credit,
        10: DESCRIPTION.format(family=family.family, repo_url=family.repo_url),
        11: family.repo_url,
        13: LICENSE_DESCRIPTION.format(license_url=family.license_url),
        14: family.license_url,
        25: f"{family.ps_family}{style.postscript_suffix}",
    }


def apply_names(font: TTFont, family: Family, style: Style) -> dict[int, str]:
    """Drop the Mac records, delete ID 7 and write the family's own strings."""
    strings = name_strings(font, family, style)
    name_table = font["name"]
    # D18: only platform 3 (Windows) records survive, which fixes fontbakery's
    # `no_mac_entries` and halves the table.
    name_table.names = [record for record in name_table.names if record.platformID != 1]
    # Literata's ID 7 is "Literata is a trademark of Google Inc." — a claim
    # about a font this one is not.
    name_table.removeNames(nameID=7)
    platform, encoding, language = WINDOWS_ENGLISH
    for name_id, string in strings.items():
        name_table.setName(string, name_id, platform, encoding, language)
    return strings


# --------------------------------------------------------------------------- #
# Assembling one style
# --------------------------------------------------------------------------- #


@dataclass
class StyleReport:
    """What one assembled style did, for the console and for the caller."""

    style: str
    source: Path
    output: Path
    glyphs_before: int
    glyphs_after: int
    custom_appended: tuple[str, ...] = ()
    custom_replaced: tuple[str, ...] = ()
    imported: int = 0
    codepoints_before: int = 0
    codepoints_after: int = 0
    scale: float = 0.0

    @property
    def glyphs_added(self) -> int:
        return self.glyphs_after - self.glyphs_before

    @property
    def codepoints_added(self) -> int:
        return self.codepoints_after - self.codepoints_before


def output_name(family: Family, style: Style) -> str:
    return f"{family.ps_family}{style.file_suffix}[opsz,wght].ttf"


def assemble_style(
    root: Path,
    style: Style,
    *,
    family: Family,
    parameters: stars.Parameters,
    rules: allowlist.Rules,
    rows: Sequence[allowlist.Row],
    log: Log,
) -> StyleReport:
    """Build one variable font, from the pinned inputs to ``fonts/variable``."""
    source = pinned_member(root, "literata", style.member)
    donor = pinned_member(root, "dejavu", DEJAVU_MEMBER)
    font = open_source_font(source)
    dejavu = TTFont(donor, lazy=True)
    try:
        before = invariants(font)
        glyphs_before = len(font.getGlyphOrder())
        codepoints_before = len(unicode_cmap(font))
        scale = import_scale(font, dejavu)

        star_glyphs = stars.build_glyphs(parameters, style.key)
        appended, replaced, star_codepoints = add_custom_glyphs(font, star_glyphs)

        overrides = resolve_overrides(font, rules, rows)
        imported, import_codepoints = import_dejavu_glyphs(
            font, dejavu, rows, scale, overrides
        )
        finish_new_glyphs(
            font,
            [*appended, *replaced, *imported],
            {**star_codepoints, **import_codepoints},
        )
        # Only the imports: they are the glyphs drawn from another font's
        # outlines, and the only ones with no gvar deltas to desynchronise.
        removeOverlaps(font, glyphNames=imported)
        # Simplifying can move an outline's box; `removeOverlaps` re-syncs the
        # left side bearing itself, the top one is ours.
        set_vertical_metrics(font, imported)
        check_side_bearings(font, [*appended, *replaced, *imported])

        regenerate_hvar(font)
        regenerate_vvar(font)
        update_os2(font, family)
        set_optical_size_elidable(font)
        apply_names(font, family, style)
        font["head"].fontRevision = family.font_revision
        stamp_timestamp(font)

        work = root / "build" / "work" / output_name(family, style)
        work.parent.mkdir(parents=True, exist_ok=True)
        font.save(work)
    finally:
        dejavu.close()
        font.close()

    # Re-read the compiled file: the invariants are only proven on the bytes
    # that ship, not on the object graph that produced them.
    written = TTFont(work, recalcTimestamp=False)
    try:
        drift = before.differences(invariants(written))
        glyphs_after = len(written.getGlyphOrder())
        codepoints_after = len(unicode_cmap(written))
    finally:
        written.close()
    if drift:
        raise AssembleError(
            f"{style.key}: the build changed values it must not:\n  "
            + "\n  ".join(drift)
        )

    output = fonts_dir_for(root) / "variable" / output_name(family, style)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(work, output)

    report = StyleReport(
        style=style.key,
        source=source,
        output=output,
        glyphs_before=glyphs_before,
        glyphs_after=glyphs_after,
        custom_appended=tuple(appended),
        custom_replaced=tuple(replaced),
        imported=len(imported),
        codepoints_before=codepoints_before,
        codepoints_after=codepoints_after,
        scale=scale,
    )
    _report(report, log)
    return report


def _report(report: StyleReport, log: Log) -> None:
    log(f"{report.style}: {report.output}")
    log(
        f"  glyphs   {report.glyphs_before} → {report.glyphs_after} "
        f"(+{report.glyphs_added}: {len(report.custom_appended)} custom appended, "
        f"{report.imported} imported; {len(report.custom_replaced)} replaced in place)"
    )
    log(
        f"  cmap     {report.codepoints_before} → {report.codepoints_after} "
        f"(+{report.codepoints_added} code points)"
    )
    log(f"  scale    k = {report.scale:.6f}; vertical metrics and head bbox unchanged")


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def assemble(
    root: Path | None = None,
    *,
    family: Family | None = None,
    styles: Iterable[Style] = STYLES,
    log: Log = print,
) -> list[StyleReport]:
    """Assemble every style: the variable-font stage of ``asterwell-build build``."""
    root = root if root is not None else upstream.default_root()
    family = family if family is not None else load_family(family_path_for(root))
    parameters = stars.load_parameters(stars.parameters_path_for(root))
    rules = allowlist.load_rules(allowlist.rules_path_for(root))
    rows = allowlist.read_tsv(allowlist.tsv_path_for(root))
    return [
        assemble_style(
            root,
            style,
            family=family,
            parameters=parameters,
            rules=rules,
            rows=rows,
            log=log,
        )
        for style in styles
    ]


def _logger(quiet: bool) -> Log:
    def log(message: str) -> None:
        if not quiet:
            print(message)

    return log


# --------------------------------------------------------------------------- #
# BUILD-INFO.json
# --------------------------------------------------------------------------- #

#: Filename of the build manifest, written into ``fonts/`` beside the outputs
#: it describes (and shipped in the release zip, where ``fonts/`` is the root —
#: which is why the output paths it records are relative to that directory).
BUILD_INFO_NAME = "BUILD-INFO.json"

#: Installed distributions whose version the manifest records, alongside the
#: interpreter's — the four names §7.7 asks for. fontTools compiles every byte
#: that ships; uharfbuzz and fontbakery decide whether it ships at all.
BUILD_INFO_TOOLS = ("fonttools", "uharfbuzz", "fontbakery")


def tool_versions() -> dict[str, str]:
    """The interpreter and library versions this build ran with (§7.7).

    Read from the installed distributions rather than from ``pyproject.toml``:
    what a rebuild has to reproduce is what actually ran, not what was asked for.
    """
    versions = {"python": platform.python_version()}
    for distribution in BUILD_INFO_TOOLS:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:  # pragma: no cover
            raise AssembleError(
                f"{distribution} is not installed — run `mise run setup`"
            ) from exc
    return versions


def input_hashes(root: Path) -> dict[str, str]:
    """Every pinned upstream member, keyed by its path under ``build/upstream``.

    Taken from the ``.verified`` manifest rather than re-hashed here: those are
    the digests ``fetch`` checked the downloaded bytes against, so recording
    them ties the outputs to a verified input rather than to whatever is on disk.
    """
    verified = upstream.read_verified(root)
    archives = verified.get("archives", {})
    if not isinstance(archives, dict) or not archives:
        raise AssembleError(
            f"{upstream.upstream_dir_for(root) / upstream.VERIFIED_NAME} lists no "
            "archives — run `mise run fetch`"
        )
    hashes: dict[str, str] = {}
    for key, archive in sorted(archives.items()):
        directory = archive.get("dir", key)
        for member, digest in sorted(archive.get("members", {}).items()):
            hashes[f"{directory}/{member}"] = digest
    return hashes


def build_info(root: Path, family: Family, outputs: Iterable[Path]) -> dict[str, object]:
    """The ``BUILD-INFO.json`` payload (§7.7).

    Everything a rebuild needs to be checked against this one: the version, the
    timestamp the build was pinned to, the tools that ran, the verified inputs,
    the allowlist that decided the coverage, and a digest per shipped file.
    """
    fonts_dir = fonts_dir_for(root)
    return {
        "version": family.version,
        "source_date_epoch": source_date_epoch(),
        "tools": tool_versions(),
        "inputs": input_hashes(root),
        "allowlist_sha256": upstream.sha256_file(allowlist.tsv_path_for(root)),
        "outputs": {
            path.relative_to(fonts_dir).as_posix(): upstream.sha256_file(path)
            for path in sorted(outputs)
        },
    }


def write_build_info(
    root: Path, family: Family, outputs: Iterable[Path], log: Log = print
) -> Path:
    """Write ``fonts/BUILD-INFO.json``. Deterministic: same inputs, same bytes."""
    path = fonts_dir_for(root) / BUILD_INFO_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_info(root, family, outputs)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log(f"build info: {path} ({len(payload['outputs'])} outputs)")  # type: ignore[arg-type]
    return path


# --------------------------------------------------------------------------- #
# The whole family
# --------------------------------------------------------------------------- #


def build(root: Path | None = None, *, quiet: bool = False) -> int:
    """``asterwell-build build`` — the whole family, in one command.

    Four stages, in dependency order: the two variable fonts, the 16 static
    instances cut from them, the two WOFF2 web fonts compressed from them, and
    the manifest that hashes everything the run wrote.
    """
    # Imported here rather than at module scope, and only here: `instances`
    # needs this module's vocabulary (Family, Invariants, open_source_font), so
    # the dependency runs one way — instances → assemble — everywhere except in
    # this function, which is the pipeline and therefore the one place that has
    # to know about every stage.
    from asterwell_build import instances, web

    root = root if root is not None else upstream.default_root()
    log = _logger(quiet)
    family = load_family(family_path_for(root))

    variable = assemble(root, family=family, log=log)
    statics = instances.build_statics(variable, family=family, root=root, log=log)
    webfonts = web.build_webfonts(variable, root=root, log=log)

    write_build_info(
        root,
        family,
        [
            *(report.output for report in variable),
            *(report.output for report in statics),
            *(report.output for report in webfonts),
        ],
        log=log,
    )
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``build``'s own options (called from :mod:`asterwell_build.cli`)."""
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the per-style report"
    )


def run(args: argparse.Namespace) -> int:
    """``asterwell-build build`` — see :func:`build`."""
    try:
        return build(quiet=getattr(args, "quiet", False))
    except (
        AssembleError,
        allowlist.AllowlistError,
        stars.StarsError,
        upstream.UpstreamError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
