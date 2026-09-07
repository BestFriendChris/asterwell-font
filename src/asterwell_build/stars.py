"""Construct the family's four original stars from one parametric template.

The design asks for a six-teardrop-petal star drawn from scratch — not traced,
not scaled from anyone else's outline — at one display size and in three
arrangements of a smaller one::

    ✽  U+273D  uni273D     one full-size star, the family's display ornament
    ⁎  U+204E  uni204E     one small star where an asterisk would sit
    ⁑  U+2051  uni2051     two small stars, stacked
    ⁂  U+2042  uni2042     three small stars, one above two (replaces
                           Literata's own composite-of-asterisks)

Everything here is generated from ``sources/stars.toml`` by the same code, so
the four glyphs are provably one design: a *petal* is the convex hull of a point
at the origin and a circle of radius ``w = petal_width · R`` centred at
``(0, R − w)`` — two tangent lines and the outer arc — and a *star* is six of
those rotated around a hub disc and unioned into a single contour.

The two sizes are **not** the same outline at two scales, which is what the
design's "adjust spacing and stroke weight optically at small sizes rather than
merely scaling identical outlines" rules out. They share the construction and
differ in the two numbers that carry the optical correction: the small star's
petals are proportionally wider (``petal_width`` 0.26 vs 0.22) and its hub is
proportionally larger (0.14 vs 0.10), so the petals stay legible and the centre
does not fill in at text sizes. Tuning those numbers is an edit to
``sources/stars.toml``; no outline is stored anywhere.

Geometry notes worth keeping in mind when reading the numbers this prints:

*Orientation.* The template is built pointing up, as spelled out above, and the
i-th petal's **axis** ends up at ``orientation + i · 360/petals`` degrees: with
``orientation = 90`` one petal points straight up. (The template is therefore
rotated by ``orientation − 90 + i · 60``, since it starts at 90 already.)

*The star is not square.* A six-fold shape repeats every 60°, so its width and
its height are extents in directions 30° apart and cannot both be ``2R``: with a
petal pointing up, the height is exactly ``2R`` (the tips) while the width is
``2·((R − w)·cos 30° + w)``, about 89.6% of it. The box is centred on the star
in both directions, but it is not a square, and the envelopes below are the
real width.

*Extrema are on-curve.* Each petal arc is split at the petal axis and at every
cardinal direction it crosses before being drawn as cubic Béziers, so the
topmost, bottommost, leftmost and rightmost points of the union are real
on-curve points. Without that the ``glyf`` bounding box — which fontTools
computes over control points too — would sit up to 10 units outside the outline.

The pipeline per star is: build the petals and hub as :class:`pathops.Path`
objects → :func:`pathops.union` (which is a ``simplify``, so it also removes the
overlaps that join the petals to the hub) → replay through
``Cu2QuPen(TTGlyphPen(None), max_err=cubic_max_err)`` → round to integers. ⁎ ⁑ ⁂
are composites of a single unencoded ``star.small`` outline, positioned by
integer translation only, so the three of them cost one outline between them and
can never drift apart.

``asterwell-build stars --svg build/stars.svg`` renders all four next to
Literata's own ``*``, ``◆`` and ⁂ at the same scale: the review artifact for the
proportions this module decides.
"""

from __future__ import annotations

import argparse
import math
import sys
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pathops
from fontTools.misc.roundTools import otRound
from fontTools.misc.transform import Transform
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import Glyph

from asterwell_build import upstream

Log = Callable[[str], None]

__all__ = [
    "Parameters",
    "StarGlyph",
    "StarsError",
    "build_glyphs",
    "glyf_table",
    "load_parameters",
    "render_svg",
    "star_path",
]

#: Glyph names. ``star.small`` is unencoded: it exists only as the component the
#: three composites share, and ``qa`` asserts no cmap subtable reaches it.
SMALL = "star.small"
FULL = "uni273D"
ONE = "uni204E"
TWO = "uni2051"
THREE = "uni2042"

#: Insertion order for the assembly step: the component first, then the glyphs
#: that use it. ``uni2042`` is last because it replaces a Literata glyph in
#: place rather than being appended.
GLYPH_ORDER = (SMALL, FULL, ONE, TWO, THREE)

#: Code point each glyph is mapped to; ``star.small`` gets none.
CODEPOINTS: Mapping[str, int | None] = {
    SMALL: None,
    FULL: 0x273D,
    ONE: 0x204E,
    TWO: 0x2051,
    THREE: 0x2042,
}

#: The two styles the family ships. Both get **identical outlines** — the stars
#: stay upright in the italic (design decision D5) — and differ only in the
#: advances they inherit from Literata.
STYLES = ("roman", "italic")

#: Advance of Literata's own ``*`` per style (§14.1). ⁎ and ⁑ take it so they
#: set like an asterisk in running text.
ASTERISK_ADVANCE: Mapping[str, int] = {"roman": 449, "italic": 475}

#: Advance of Literata's own ``⁂`` per style (§14.1), kept because the glyph is
#: replaced in place and its advance is already in ``HVAR``/``hmtx``.
ASTERISM_ADVANCE: Mapping[str, int] = {"roman": 889, "italic": 915}

#: Vertical centre of Literata's ``*`` (bbox y 407–782, §14.1): where a single
#: small star sits so it reads as an asterisk.
ASTERISK_CENTER_Y = 594

#: Basename of the Literata member the ``--svg`` comparison reads, and the
#: glyphs it takes from it: the two the spec asks for, plus the asterism this
#: family replaces.
LITERATA_MEMBER = "Literata[opsz,wght].ttf"
COMPARISON_GLYPHS = (
    ("asterisk", "Literata *", "U+002A"),
    ("uni25C6", "Literata ◆", "U+25C6"),
    ("uni2042", "Literata ⁂", "U+2042 (replaced)"),
)


class StarsError(Exception):
    """The parameters are malformed, or the geometry they ask for is impossible."""


# --------------------------------------------------------------------------- #
# Parameters (sources/stars.toml)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Outline:
    """The three numbers that decide a star's shape."""

    radius: float
    """Outer radius R: the distance from the centre to a petal tip."""

    petal_width: float
    """Bulb radius as a fraction of R; the petal's half-angle is asin(w/(R−w))."""

    hub: float
    """Hub-disc radius as a fraction of R; what joins the petals into one contour."""


@dataclass(frozen=True)
class FullStar(Outline):
    """``[full]`` — the standalone ✽."""

    advance: int
    center_y: float


@dataclass(frozen=True)
class SmallStar(Outline):
    """``[small]`` — the shared component of ⁎ ⁑ ⁂."""

    gap: float
    """Edge-to-edge space between two small stars; centre distance is 2R + gap."""

    @property
    def separation(self) -> float:
        """Centre-to-centre distance between neighbouring stars in ⁑ and ⁂."""
        return 2 * self.radius + self.gap


@dataclass(frozen=True)
class Parameters:
    """All of ``sources/stars.toml``."""

    petals: int
    orientation: float
    cubic_max_err: float
    full: FullStar
    small: SmallStar


def parameters_path_for(root: Path) -> Path:
    return root / "sources" / "stars.toml"


def load_parameters(path: Path) -> Parameters:
    """Parse and validate ``stars.toml``."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StarsError(f"missing parameter file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise StarsError(f"{path}: not valid TOML: {exc}") from exc

    template = _section(path, raw, "template")
    full_section = _section(path, raw, "full")
    small_section = _section(path, raw, "small")

    petals = _number(path, "template", template, "petals")
    if petals != int(petals) or int(petals) < 3:
        raise StarsError(f"{path}: [template] petals must be a whole number ≥ 3")
    cubic_max_err = _number(path, "template", template, "cubic_max_err")
    if cubic_max_err <= 0:
        raise StarsError(f"{path}: [template] cubic_max_err must be positive")

    parameters = Parameters(
        petals=int(petals),
        orientation=_number(path, "template", template, "orientation"),
        cubic_max_err=cubic_max_err,
        full=FullStar(
            radius=_number(path, "full", full_section, "radius"),
            petal_width=_number(path, "full", full_section, "petal_width"),
            hub=_number(path, "full", full_section, "hub"),
            advance=int(_number(path, "full", full_section, "advance")),
            center_y=_number(path, "full", full_section, "center_y"),
        ),
        small=SmallStar(
            radius=_number(path, "small", small_section, "radius"),
            petal_width=_number(path, "small", small_section, "petal_width"),
            hub=_number(path, "small", small_section, "hub"),
            gap=_number(path, "small", small_section, "gap"),
        ),
    )
    _validate(path, parameters)
    return parameters


def _section(path: Path, raw: Mapping[str, object], name: str) -> Mapping[str, object]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise StarsError(f"{path}: missing [{name}] table")
    return section


def _number(path: Path, where: str, section: Mapping[str, object], key: str) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StarsError(f"{path}: [{where}] {key} must be a number, got {value!r}")
    return float(value)


def _validate(path: Path, parameters: Parameters) -> None:
    """Reject parameters that cannot produce a well-formed star."""
    limit = 180.0 / parameters.petals
    for name, outline in (("full", parameters.full), ("small", parameters.small)):
        if outline.radius <= 0:
            raise StarsError(f"{path}: [{name}] radius must be positive")
        if not 0 < outline.petal_width < 0.5:
            raise StarsError(
                f"{path}: [{name}] petal_width must be between 0 and 0.5 "
                "(the bulb has to fit between the tip and the centre)"
            )
        if not 0 < outline.hub < 1:
            raise StarsError(f"{path}: [{name}] hub must be between 0 and 1")
        half_angle = math.degrees(petal_half_angle(outline))
        if half_angle >= limit:
            raise StarsError(
                f"{path}: [{name}] petal_width {outline.petal_width} gives a petal "
                f"half-angle of {half_angle:.1f}°, which is not under the {limit:.0f}° "
                f"that keeps {parameters.petals} petals from overlapping"
            )
    if parameters.small.gap < 0:
        raise StarsError(f"{path}: [small] gap must not be negative")
    if parameters.full.advance <= 0:
        raise StarsError(f"{path}: [full] advance must be positive")


def petal_half_angle(outline: Outline) -> float:
    """Half-angle at the tip, in radians: ``asin(w / (R − w))``."""
    w = outline.petal_width * outline.radius
    return math.asin(w / (outline.radius - w))


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

_QUARTER = math.pi / 2
_TAU = 2 * math.pi

def _kappa(theta: float) -> float:
    """Control-point distance for a cubic approximating an arc of angle ``theta``,
    as a fraction of the radius: the classic 4/3·tan(θ/4)."""
    return 4.0 / 3.0 * math.tan(theta / 4.0)


def _arc_cubic(
    centre: tuple[float, float], radius: float, start: float, end: float
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """One cubic segment of the circle, from angle ``start`` to ``end``."""
    cx, cy = centre
    k = _kappa(end - start) * radius
    x0, y0 = cx + radius * math.cos(start), cy + radius * math.sin(start)
    x1, y1 = cx + radius * math.cos(end), cy + radius * math.sin(end)
    return (
        (x0 - k * math.sin(start), y0 + k * math.cos(start)),
        (x1 + k * math.sin(end), y1 - k * math.cos(end)),
        (x1, y1),
    )


def _arc_spans(
    start: float, end: float, splits: Iterable[float]
) -> Iterator[tuple[float, float]]:
    """Split ``[start, end]`` at every angle in ``splits`` that falls inside it,
    then subdivide each piece so no span exceeds a quarter turn."""
    stops = {start, end}
    for angle in splits:
        inside = start + (angle - start) % _TAU
        if start < inside < end:
            stops.add(inside)
    ordered = sorted(stops)
    for first, last in zip(ordered, ordered[1:]):
        pieces = max(1, math.ceil((last - first) / _QUARTER - 1e-9))
        for i in range(pieces):
            yield first + (last - first) * i / pieces, first + (last - first) * (i + 1) / pieces


def _petal_path(outline: Outline, axis_degrees: float) -> pathops.Path:
    """One teardrop petal, tip at the origin, its axis at ``axis_degrees``."""
    w = outline.petal_width * outline.radius
    distance = outline.radius - w  # centre of the bulb, along the petal axis
    half_angle = petal_half_angle(outline)

    # The template points up (+y), so the bulb sits at (0, R − w) and the two
    # tangent lines from the origin touch it half_angle below the horizontal
    # through that centre, one either side. The outer arc runs from one tangent
    # point over the top to the other; rotating the finished petal by
    # axis − 90° lands it on its axis.
    axis = math.radians(axis_degrees - 90.0)
    rotation = Transform().rotate(axis)
    start, end = -half_angle, math.pi + half_angle
    # Split at the apex (the tip direction) and wherever the arc crosses a
    # cardinal direction of the *final* frame, so every extreme is on-curve.
    splits = [_QUARTER] + [i * _QUARTER - axis for i in range(4)]

    path = pathops.Path()
    pen = path.getPen()
    pen.moveTo(rotation.transformPoint((0.0, 0.0)))
    pen.lineTo(
        rotation.transformPoint((w * math.cos(start), distance + w * math.sin(start)))
    )
    for first, last in _arc_spans(start, end, splits):
        pen.curveTo(
            *(
                rotation.transformPoint(point)
                for point in _arc_cubic((0.0, distance), w, first, last)
            )
        )
    pen.closePath()
    return path


def _disc_path(radius: float, parameters: Parameters) -> pathops.Path:
    """The hub: a circle at the origin, subdivided in step with the petals.

    The valleys between the petals are hub arc, so where the circle is split
    shows up in the finished outline. Splitting it every ``180/petals`` degrees,
    keyed to the petals' own origin, is what has all six valleys drawn the same
    way instead of however the quarter-arcs of a plain circle happened to fall.
    """
    step = math.pi / parameters.petals
    start = math.radians(parameters.orientation) - step * parameters.petals
    splits = [start + i * step for i in range(2 * parameters.petals)]
    path = pathops.Path()
    pen = path.getPen()
    pen.moveTo((radius * math.cos(start), radius * math.sin(start)))
    for first, last in _arc_spans(start, start + _TAU, splits):
        pen.curveTo(*_arc_cubic((0.0, 0.0), radius, first, last))
    pen.closePath()
    return path


def star_path(outline: Outline, parameters: Parameters) -> pathops.Path:
    """The whole star, centred on the origin, as one closed contour.

    ``pathops.union`` is a ``simplify`` over every contour at once, so this both
    merges the petals with the hub and leaves a path with no self-intersections:
    running ``simplify`` on the result changes nothing (a Step-4 test).
    """
    step = 360.0 / parameters.petals
    contours = [
        _petal_path(outline, parameters.orientation + i * step)
        for i in range(parameters.petals)
    ]
    contours.append(_disc_path(outline.hub * outline.radius, parameters))

    path = pathops.Path()
    # clockwise=True: TrueType fills clockwise outer contours, as Literata's own
    # glyphs do (its `asterisk` has negative signed area).
    pathops.union(contours, path.getPen(), fix_winding=True, clockwise=True)
    if len(list(path.contours)) != 1:
        raise StarsError(
            f"the union of {parameters.petals} petals and the hub left "
            f"{len(list(path.contours))} contours; the hub is too small to join them"
        )
    return path


# --------------------------------------------------------------------------- #
# Glyphs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StarGlyph:
    """One finished glyph, with everything the assembly step has to write."""

    name: str
    codepoint: int | None
    glyph: Glyph
    advance: int
    lsb: int
    """Left side bearing = ``xMin``, the TrueType convention."""

    bounds: tuple[int, int, int, int]
    components: tuple[str, ...] = ()

    @property
    def is_composite(self) -> bool:
        return bool(self.components)

    @property
    def width(self) -> int:
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> int:
        return self.bounds[3] - self.bounds[1]


def outline_glyph(
    path: pathops.Path, parameters: Parameters, offset: tuple[float, float]
) -> Glyph:
    """Convert a cubic :class:`pathops.Path` into a rounded quadratic glyph."""
    moved = path.transform(1.0, 0.0, 0.0, 1.0, *offset)
    pen = TTGlyphPen(None)
    moved.draw(Cu2QuPen(pen, max_err=parameters.cubic_max_err))
    glyph = pen.glyph()
    glyph.recalcBounds(glyfTable=None)
    return glyph


def composite_glyph(
    component: str, translations: Sequence[tuple[float, float]], glyf: Mapping[str, Glyph]
) -> Glyph:
    """A composite of ``component`` repeated at each translation.

    Translation only — no scale, no rotation — so the F2Dot14 limit on component
    transforms never comes into play and ``ROUND_XY_TO_GRID`` (0x04) is the only
    flag needed.
    """
    pen = TTGlyphPen(glyf)
    for dx, dy in translations:
        pen.addComponent(component, Transform().translate(dx, dy))
    glyph = pen.glyph(componentFlags=0x04)
    glyph.recalcBounds(glyfTable=glyf)
    return glyph


def build_glyphs(parameters: Parameters, style: str = "roman") -> dict[str, StarGlyph]:
    """The five glyphs of the star family, in glyph-order, for one style.

    The two *outlines* — ``star.small`` and ``uni273D`` — are identical in both
    styles, upright in the italic (D5). What the style changes is the advance ⁎
    ⁑ ⁂ inherit from Literata, and with it where the shared component sits
    inside that advance.
    """
    if style not in STYLES:
        raise StarsError(f"unknown style {style!r}; expected one of {', '.join(STYLES)}")

    small = parameters.small
    # star.small is placed with its centre at (R, R), so it fills the box
    # 0 ≤ y ≤ 2R and is centred on an advance of 2R. Its own bounding box is
    # narrower than that box (a six-fold star is not square) — the composites
    # below place it by its centre, which is what keeps ⁎ optically centred.
    small_advance = otRound(2 * small.radius)
    small_glyph = outline_glyph(
        star_path(small, parameters), parameters, (small.radius, small.radius)
    )
    full = parameters.full
    full_glyph = outline_glyph(
        star_path(full, parameters), parameters, (full.advance / 2.0, full.center_y)
    )

    glyf: dict[str, Glyph] = {SMALL: small_glyph}
    centre = (small.radius, small.radius)

    def offsets(*centres: tuple[float, float]) -> list[tuple[float, float]]:
        return [(x - centre[0], y - centre[1]) for x, y in centres]

    asterisk_advance = ASTERISK_ADVANCE[style]
    asterism_advance = ASTERISM_ADVANCE[style]
    separation = small.separation
    # ⁂ is an equilateral triangle of side `separation`, standing on the pair
    # that sits on the baseline (their centres at y = R, so yMin = 0).
    triangle_rise = separation * math.sin(math.radians(60.0))

    plans: list[tuple[str, int, list[tuple[float, float]]]] = [
        (
            ONE,
            asterisk_advance,
            offsets((asterisk_advance / 2.0, ASTERISK_CENTER_Y)),
        ),
        (
            TWO,
            asterisk_advance,
            offsets(
                (asterisk_advance / 2.0, ASTERISK_CENTER_Y),
                (asterisk_advance / 2.0, ASTERISK_CENTER_Y - separation),
            ),
        ),
        (
            THREE,
            asterism_advance,
            offsets(
                (asterism_advance / 2.0 - separation / 2.0, small.radius),
                (asterism_advance / 2.0 + separation / 2.0, small.radius),
                (asterism_advance / 2.0, small.radius + triangle_rise),
            ),
        ),
    ]
    built: dict[str, StarGlyph] = {
        SMALL: _star_glyph(SMALL, small_glyph, small_advance),
        FULL: _star_glyph(FULL, full_glyph, full.advance),
    }
    for name, advance, translations in plans:
        glyph = composite_glyph(SMALL, translations, glyf)
        glyf[name] = glyph
        built[name] = _star_glyph(
            name, glyph, advance, components=(SMALL,) * len(translations)
        )
    return {name: built[name] for name in GLYPH_ORDER}


def _star_glyph(
    name: str, glyph: Glyph, advance: int, components: tuple[str, ...] = ()
) -> StarGlyph:
    bounds = (glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax)
    return StarGlyph(
        name=name,
        codepoint=CODEPOINTS[name],
        glyph=glyph,
        advance=advance,
        lsb=glyph.xMin,
        bounds=bounds,
        components=components,
    )


def glyf_table(glyphs: Mapping[str, StarGlyph]) -> dict[str, Glyph]:
    """The ``{name: Glyph}`` mapping composites need to resolve components."""
    return {name: star.glyph for name, star in glyphs.items()}


# --------------------------------------------------------------------------- #
# The review SVG
# --------------------------------------------------------------------------- #


class Drawable(Protocol):
    def draw(self, pen: object) -> None: ...  # pragma: no cover - structural type


@dataclass(frozen=True)
class Cell:
    """One glyph as the SVG shows it: a drawable plus what to say about it."""

    label: str
    detail: str
    advance: int
    bounds: tuple[int, int, int, int]
    draw: Callable[[object], None]
    glyph_set: Mapping[str, Drawable] | None = None
    """Where the pen resolves components; ``None`` for a plain outline."""


class _GlyphDrawable:
    """Adapts a ``glyf`` glyph to the ``.draw(pen)`` protocol pens expect."""

    def __init__(self, glyph: Glyph, glyf: Mapping[str, Glyph]) -> None:
        self._glyph = glyph
        self._glyf = glyf

    def draw(self, pen: object) -> None:
        self._glyph.draw(pen, self._glyf)


#: Em size, in px, of the outline row; the text rows are at real px sizes.
SVG_EM = 280
SVG_TEXT_SIZES = (34, 17)
SVG_ASCENDER = 1177  # Literata's hhea ascent/descent, the box each cell shows
SVG_DESCENDER = -308
SVG_CAP_HEIGHT = 700


def _svg_cells(
    glyphs: Mapping[str, StarGlyph], comparisons: Sequence[Cell] = ()
) -> list[Cell]:
    glyf = glyf_table(glyphs)
    glyph_set = {name: _GlyphDrawable(glyph, glyf) for name, glyph in glyf.items()}
    cells: list[Cell] = []
    for name, star in glyphs.items():
        char = chr(star.codepoint) if star.codepoint is not None else "—"
        codepoint = (
            f"U+{star.codepoint:04X}" if star.codepoint is not None else "unencoded"
        )
        cells.append(
            Cell(
                label=f"{char} {name}",
                detail=codepoint,
                advance=star.advance,
                bounds=star.bounds,
                draw=glyph_set[name].draw,
                glyph_set=glyph_set,
            )
        )
    return cells + list(comparisons)


def literata_cells(
    root: Path, names: Sequence[tuple[str, str, str]] = COMPARISON_GLYPHS
) -> list[Cell]:
    """Comparison cells read from the pinned Literata roman variable font."""
    path = literata_path(root)
    font = TTFont(path, lazy=True)
    glyph_set = font.getGlyphSet()
    metrics = font["hmtx"].metrics
    glyf = font["glyf"]
    cells = []
    for name, label, detail in names:
        if name not in glyph_set:
            raise StarsError(f"{path} has no glyph {name!r}")
        glyph = glyf[name]  # expands the glyph; its bounds come from the file
        bounds = (
            (glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax)
            if glyph.numberOfContours
            else (0, 0, 0, 0)
        )
        cells.append(
            Cell(
                label=label,
                detail=detail,
                advance=metrics[name][0],
                bounds=bounds,
                draw=glyph_set[name].draw,
                glyph_set=glyph_set,
            )
        )
    return cells


def literata_path(root: Path) -> Path:
    """Path of the pinned Literata roman VF inside ``build/upstream``."""
    pins = {pin.key: pin for pin in upstream.load_pins(upstream.pin_file_for(root))}
    pin = pins.get("literata")
    if pin is None:
        raise StarsError("sources/upstream.toml has no [literata] section")
    matches = [m for m in pin.members if m.rsplit("/", 1)[-1] == LITERATA_MEMBER]
    if len(matches) != 1:
        raise StarsError(
            f"sources/upstream.toml [literata.members] must pin exactly one "
            f"{LITERATA_MEMBER}; found {len(matches)}"
        )
    path = pin.extract_dir(upstream.upstream_dir_for(root)) / matches[0]
    if not path.is_file():
        raise StarsError(f"{path} is missing — run `mise run fetch`")
    return path


def _path_data(cell: Cell) -> str:
    pen = SVGPathPen(cell.glyph_set, ntos=_format_number)
    cell.draw(pen)
    return pen.getCommands()


def _format_number(value: float) -> str:
    rounded = round(value, 1)
    return f"{rounded:g}"


def render_svg(
    glyphs: Mapping[str, StarGlyph],
    comparisons: Sequence[Cell] = (),
    *,
    style: str = "roman",
) -> str:
    """The Step-4 review artifact: every star beside Literata's own marks.

    One row of outlines at a common em size with each glyph's advance, baseline,
    cap height and bounding box drawn in, then the same run set as text at 34 px
    and 17 px — the sizes at which the small star's optical correction either
    works or does not.
    """
    cells = _svg_cells(glyphs, comparisons)
    scale = SVG_EM / 1000.0
    margin = 32.0
    gutter = 30.0
    label_height = 46.0

    top = margin + 54.0
    baseline = top + SVG_ASCENDER * scale
    row_bottom = top + (SVG_ASCENDER - SVG_DESCENDER) * scale
    parts: list[str] = []
    x = margin
    for cell in cells:
        width = cell.advance * scale
        parts.append(
            f'<g transform="translate({x:.1f} {baseline:.1f}) scale({scale:g} -{scale:g})">'
        )
        parts.append(
            f'<rect class="em" x="0" y="{SVG_DESCENDER}" width="{cell.advance}" '
            f'height="{SVG_ASCENDER - SVG_DESCENDER}"/>'
        )
        x_min, y_min, x_max, y_max = cell.bounds
        if x_max > x_min:
            parts.append(
                f'<rect class="bbox" x="{x_min}" y="{y_min}" width="{x_max - x_min}" '
                f'height="{y_max - y_min}"/>'
            )
        parts.append(
            f'<path class="glyph" d="{_path_data(cell)}"/>'
        )
        parts.append("</g>")
        parts.append(
            f'<line class="rule" x1="{x:.1f}" y1="{baseline:.1f}" '
            f'x2="{x + width:.1f}" y2="{baseline:.1f}"/>'
        )
        cap = baseline - SVG_CAP_HEIGHT * scale
        parts.append(
            f'<line class="rule cap" x1="{x:.1f}" y1="{cap:.1f}" '
            f'x2="{x + width:.1f}" y2="{cap:.1f}"/>'
        )
        text_y = row_bottom + 18.0
        parts.append(
            f'<text class="name" x="{x:.1f}" y="{text_y:.1f}">'
            f"{_escape(cell.label)}</text>"
        )
        parts.append(
            f'<text class="meta" x="{x:.1f}" y="{text_y + 15.0:.1f}">{_escape(cell.detail)}'
            f' · adv {cell.advance}</text>'
        )
        parts.append(
            f'<text class="meta" x="{x:.1f}" y="{text_y + 29.0:.1f}">'
            f'bbox {x_min} {y_min} {x_max} {y_max}</text>'
        )
        x += width + gutter

    total_width = max(x - gutter + margin, 640.0)
    y = row_bottom + label_height + 30.0
    for size in SVG_TEXT_SIZES:
        parts.append(
            f'<text class="meta" x="{margin:.1f}" y="{y:.1f}">as text, {size} px</text>'
        )
        y += 12.0 + size
        run_scale = size / 1000.0
        run_x = margin
        for cell in cells:
            parts.append(
                f'<g transform="translate({run_x:.1f} {y:.1f}) '
                f'scale({run_scale:g} -{run_scale:g})">'
                f'<path class="glyph" d="{_path_data(cell)}"/></g>'
            )
            run_x += cell.advance * run_scale + size * 0.6
        y += 34.0

    height = y + margin
    header = (
        f'<text class="title" x="{margin:.1f}" y="{margin + 22:.1f}">'
        f"Asterwell Text — six-petal star family ({_escape(style)})</text>"
        f'<text class="meta" x="{margin:.1f}" y="{margin + 40:.1f}">'
        "grey box: advance × (descender…ascender) · dashed: glyph bbox · "
        "rules: baseline and cap height (700) · outlines are identical in both styles"
        "</text>"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {total_width:.0f} {height:.0f}">'
        f"<style>{_SVG_STYLE}</style>"
        f'<rect class="page" x="0" y="0" width="{total_width:.0f}" height="{height:.0f}"/>'
        + header
        + "".join(parts)
        + "</svg>\n"
    )


_SVG_STYLE = (
    ".page{fill:#ffffff}"
    ".glyph{fill:#111111}"
    ".em{fill:#f2f0ec;stroke:#d8d3ca;stroke-width:4}"
    ".bbox{fill:none;stroke:#c0392b;stroke-width:4;stroke-dasharray:16 12}"
    ".rule{stroke:#8a8478;stroke-width:1}"
    ".cap{stroke-dasharray:4 4}"
    "text{font-family:ui-sans-serif,-apple-system,Segoe UI,Helvetica,Arial,sans-serif}"
    ".title{font-size:17px;font-weight:600;fill:#111111}"
    ".name{font-size:13px;fill:#111111}"
    ".meta{font-size:11px;fill:#6b6459}"
)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def _logger(quiet: bool) -> Log:
    def log(message: str) -> None:
        if not quiet:
            print(message)

    return log


def _report(style: str, glyphs: Mapping[str, StarGlyph], log: Log) -> None:
    log(f"{style}:")
    for star in glyphs.values():
        char = chr(star.codepoint) if star.codepoint is not None else " "
        shape = (
            f"composite ×{len(star.components)} of {star.components[0]}"
            if star.is_composite
            else f"outline, {len(star.glyph.coordinates)} points, "
            f"{star.glyph.numberOfContours} contour"
        )
        x_min, y_min, x_max, y_max = star.bounds
        log(
            f"  {star.name:<11}{char}  adv {star.advance:>4}  lsb {star.lsb:>4}  "
            f"bbox ({x_min:>4},{y_min:>5},{x_max:>4},{y_max:>4})  "
            f"{star.width}×{star.height}  {shape}"
        )


def build(
    root: Path | None = None,
    *,
    svg: Path | None = None,
    quiet: bool = False,
) -> int:
    """``asterwell-build stars`` — construct the family and report on it."""
    root = root if root is not None else upstream.default_root()
    log = _logger(quiet)

    parameters = load_parameters(parameters_path_for(root))
    per_style = {style: build_glyphs(parameters, style) for style in STYLES}
    for style, glyphs in per_style.items():
        _report(style, glyphs, log)

    if svg is not None:
        text = render_svg(per_style["roman"], literata_cells(root))
        svg.parent.mkdir(parents=True, exist_ok=True)
        svg.write_text(text, encoding="utf-8")
        log(f"wrote {svg}")
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``stars``'s own options (called from :mod:`asterwell_build.cli`)."""
    parser.add_argument(
        "--svg",
        type=Path,
        metavar="PATH",
        help=(
            "render the star family beside Literata's * ◆ ⁂ to this SVG file "
            "(needs the pinned upstream fonts: `mise run fetch`)"
        ),
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the per-glyph report"
    )


def run(args: argparse.Namespace) -> int:
    """``asterwell-build stars`` — see :func:`build`."""
    try:
        return build(svg=getattr(args, "svg", None), quiet=getattr(args, "quiet", False))
    except (StarsError, upstream.UpstreamError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
