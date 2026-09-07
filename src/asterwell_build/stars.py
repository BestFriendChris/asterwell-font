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

*Orientation, and what the italic changes.* The template is built pointing up,
as spelled out above, and the i-th petal's **axis** ends up at ``orientation +
i · 360/petals`` degrees: with ``orientation = 90`` one petal points straight
up. (The template is therefore rotated by ``orientation − 90 + i · 60``, since
it starts at 90 already.) The **italic turns that template by**
``[italic] rotation`` **degrees** (30, so petals at 60° and 120° — up-right and
up-left, none straight up) — decision D21, from the user's "rotate the flower …
for italic only". The two styles therefore do *not* share one outline; the
italic's is the roman's turned, never sheared, and the roman is untouched. On
top of the turn, the **stacks** ⁑ and ⁂ lean: each star's centre moves
horizontally by ``(its centre height − the stack's mean centre height) ·
tan(stack_slant)``, so the group tilts as a whole while every star in it keeps
the plain turned shape. That is how Literata Italic leans its own stacked marks
(its colon 2.49°, its semicolon 3.30°, its ⁂ +16 units) — by displacement, not
by skewing the mark. Single stars (⁎ and ✽) turn but never lean; a stack of one
has nothing to lean about. What stays true across the two styles is D5: the
stars are invariant along the ``wght`` and ``opsz`` axes, one fixed design at
every weight and optical size.

*The star is not square.* A six-fold shape repeats every 60°, so its width and
its height are extents in directions 30° apart and cannot both be ``2R``: with a
petal pointing up, the height is exactly ``2R`` (the tips) while the width is
``2·((R − w)·cos 30° + w)``, about 89.6% of it. The box is centred on the star
in both directions, but it is not a square, and the envelopes below are the
real width. The italic's turn swaps the two over — two tips land on the
horizontal, so the star comes out wider than tall — which is why ✽'s side
bearings drop from 80 to 43 on an unchanged advance.

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
Literata's own ``*``, ``◆`` and ⁂ at the same scale — one row per style, the
roman beside Literata's roman marks and the italic beside Literata *Italic*'s,
so the turn and the lean are read against the italic's own asterisk and its own
leaning asterism: the review artifact for the proportions this module decides.
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
    "ItalicTreatment",
    "Parameters",
    "StarGlyph",
    "StarsError",
    "build_glyphs",
    "glyf_table",
    "leaned",
    "load_parameters",
    "orientation_for",
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

#: The two styles the family ships. They share the construction and every
#: parameter but two: the italic turns the template by ``[italic] rotation`` and
#: leans the stacks ⁑ ⁂ by ``[italic] stack_slant`` (D21), and each style takes
#: its own advances from Literata. Outlines are *not* shared between them; what
#: is invariant is the pair of axes (D5).
STYLES = ("roman", "italic")

#: The style whose parameters ``sources/stars.toml`` states directly; every other
#: style is that one plus its own entry in the file.
ROMAN = "roman"
ITALIC = "italic"

#: Advance of Literata's own ``*`` per style (§14.1). ⁎ and ⁑ take it so they
#: set like an asterisk in running text.
ASTERISK_ADVANCE: Mapping[str, int] = {"roman": 449, "italic": 475}

#: Advance of Literata's own ``⁂`` per style (§14.1), kept because the glyph is
#: replaced in place and its advance is already in ``HVAR``/``hmtx``.
ASTERISM_ADVANCE: Mapping[str, int] = {"roman": 889, "italic": 915}

#: Vertical centre of Literata's ``*`` (bbox y 407–782, §14.1): where a single
#: small star sits so it reads as an asterisk.
ASTERISK_CENTER_Y = 594

#: Basename of the Literata member the ``--svg`` comparison reads, per style:
#: each row of the review SVG is set beside its *own* style's marks, so the
#: italic stars are judged against the italic's asterisk and its leaning ⁂.
LITERATA_MEMBERS: Mapping[str, str] = {
    ROMAN: "Literata[opsz,wght].ttf",
    ITALIC: "Literata-Italic[opsz,wght].ttf",
}

#: Glyphs the comparison takes from that member: the two the spec asks for, plus
#: the asterism this family replaces. The labels name Literata; the style's own
#: name is substituted in when the cells are read.
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
class ItalicTreatment:
    """``[italic]`` — the two numbers that are the whole difference (D21)."""

    rotation: float
    """Degrees the template turns in the italic. 30: petals up-right and up-left."""

    stack_slant: float
    """Degrees the stacked stars of ⁑ and ⁂ lean, tops to the right."""

    @property
    def lean(self) -> float:
        """``tan(stack_slant)`` — horizontal shift per unit of height above the mean."""
        return math.tan(math.radians(self.stack_slant))


@dataclass(frozen=True)
class Parameters:
    """All of ``sources/stars.toml``."""

    petals: int
    orientation: float
    cubic_max_err: float
    full: FullStar
    small: SmallStar
    italic: ItalicTreatment


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
    italic_section = _section(path, raw, "italic")

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
        italic=ItalicTreatment(
            rotation=_number(path, "italic", italic_section, "rotation"),
            stack_slant=_number(path, "italic", italic_section, "stack_slant"),
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

    italic = parameters.italic
    if not math.isfinite(italic.rotation):
        raise StarsError(f"{path}: [italic] rotation must be a finite number")
    # A shape with `petals` petals repeats every 360/petals degrees, so a turn by
    # a multiple of that is no turn at all — the italic would come out byte for
    # byte the roman, which is precisely what D21 is undoing.
    period = 360.0 / parameters.petals
    if abs(math.remainder(italic.rotation, period)) < 1e-9:
        raise StarsError(
            f"{path}: [italic] rotation {italic.rotation:g}° is a multiple of the "
            f"{period:g}° a {parameters.petals}-petal star repeats over, so it turns "
            "the star onto itself; the italic would be indistinguishable"
        )
    if not -45.0 < italic.stack_slant < 45.0:
        raise StarsError(
            f"{path}: [italic] stack_slant must be between -45 and 45 degrees, "
            f"got {italic.stack_slant:g}"
        )


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


def orientation_for(parameters: Parameters, style: str = ROMAN) -> float:
    """Degrees the first petal's axis points, for one style.

    The roman is ``[template] orientation`` as written (90: a petal straight up);
    the italic adds ``[italic] rotation`` (30: petals at 60° and 120°, none
    straight up) — D21. The direction of the turn does not matter: with six
    petals, +30 and −30 differ by 60°, which is a symmetry of the star, and the
    two orientations produce byte-identical outlines (§14.7).
    """
    if style not in STYLES:
        raise StarsError(f"unknown style {style!r}; expected one of {', '.join(STYLES)}")
    if style == ITALIC:
        return parameters.orientation + parameters.italic.rotation
    return parameters.orientation


def _disc_path(radius: float, parameters: Parameters, orientation: float) -> pathops.Path:
    """The hub: a circle at the origin, subdivided in step with the petals.

    The valleys between the petals are hub arc, so where the circle is split
    shows up in the finished outline. Splitting it every ``180/petals`` degrees,
    keyed to the petals' own origin — which turns with the style, so the six
    valleys stay identical to each other after the italic's rotation — is what
    has all six valleys drawn the same way instead of however the quarter-arcs of
    a plain circle happened to fall.
    """
    step = math.pi / parameters.petals
    start = math.radians(orientation) - step * parameters.petals
    splits = [start + i * step for i in range(2 * parameters.petals)]
    path = pathops.Path()
    pen = path.getPen()
    pen.moveTo((radius * math.cos(start), radius * math.sin(start)))
    for first, last in _arc_spans(start, start + _TAU, splits):
        pen.curveTo(*_arc_cubic((0.0, 0.0), radius, first, last))
    pen.closePath()
    return path


def star_path(
    outline: Outline, parameters: Parameters, *, style: str = ROMAN
) -> pathops.Path:
    """The whole star, centred on the origin, as one closed contour.

    ``pathops.union`` is a ``simplify`` over every contour at once, so this both
    merges the petals with the hub and leaves a path with no self-intersections:
    running ``simplify`` on the result changes nothing (a Step-4 test).

    The style decides only which way the star faces (:func:`orientation_for`);
    every petal is redrawn in the turned frame rather than the roman being
    rotated after the fact, so extremes stay on-curve at any orientation.
    """
    orientation = orientation_for(parameters, style)
    step = 360.0 / parameters.petals
    contours = [
        _petal_path(outline, orientation + i * step) for i in range(parameters.petals)
    ]
    contours.append(_disc_path(outline.hub * outline.radius, parameters, orientation))

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


def leaned(
    centres: Sequence[tuple[float, float]], parameters: Parameters, style: str
) -> list[tuple[float, float]]:
    """Tilt a stack of star centres as a group, without touching any outline.

    Each centre moves horizontally by ``(its height − the stack's mean height) ·
    tan(stack_slant)``, so the stack leans about its own middle: the top star
    goes right, the bottom left, and the group's optical centre stays where the
    advance put it. Nothing is sheared — every star in the stack is the same
    turned outline — which is how Literata Italic leans its own colon, semicolon
    and ⁂ (D21, §14.7).

    The roman leans by nothing, and a stack of one (⁎) never moves in either
    style: its only centre *is* the mean.
    """
    if style != ITALIC or not centres:
        return list(centres)
    lean = parameters.italic.lean
    mean_y = sum(y for _, y in centres) / len(centres)
    return [(x + (y - mean_y) * lean, y) for x, y in centres]


def build_glyphs(parameters: Parameters, style: str = ROMAN) -> dict[str, StarGlyph]:
    """The five glyphs of the star family, in glyph-order, for one style.

    The two *outlines* — ``star.small`` and ``uni273D`` — are the same template
    in both styles, but not the same drawing: in the italic it is turned by
    ``[italic] rotation`` (D21), so a petal points up-left and up-right instead
    of straight up. The roman is exactly what it has always been.

    Placement changes in two ways with the style. Every composite takes the
    style's own asterisk/asterism advance from Literata, which moves the shared
    component inside it; and in the italic the *stacks* ⁑ and ⁂ additionally lean
    (:func:`leaned`) — ⁑'s two stars ±9 units about their midpoint, ⁂'s top star
    16 units right of its pair. ⁎ and ✽ are single stars: they turn, they never
    lean. Composites stay translation-only either way (D9).
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
        star_path(small, parameters, style=style),
        parameters,
        (small.radius, small.radius),
    )
    full = parameters.full
    full_glyph = outline_glyph(
        star_path(full, parameters, style=style),
        parameters,
        (full.advance / 2.0, full.center_y),
    )

    glyf: dict[str, Glyph] = {SMALL: small_glyph}
    centre = (small.radius, small.radius)

    def offsets(*centres: tuple[float, float]) -> list[tuple[float, float]]:
        """Star centres → component translations, leaning the stack on the way."""
        return [
            (x - centre[0], y - centre[1]) for x, y in leaned(centres, parameters, style)
        ]

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
    root: Path,
    names: Sequence[tuple[str, str, str]] = COMPARISON_GLYPHS,
    *,
    style: str = ROMAN,
) -> list[Cell]:
    """Comparison cells read from the pinned Literata VF of one style.

    The italic row of the review SVG is set beside Literata *Italic*'s own
    ``*``, ``◆`` and ⁂ — the last of which leans its top asterisk 16 units over
    its pair, the number ``stack_slant`` reproduces.
    """
    path = literata_path(root, style=style)
    prefix = "Literata Italic" if style == ITALIC else "Literata"
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
                label=label.replace("Literata", prefix, 1),
                detail=detail,
                advance=metrics[name][0],
                bounds=bounds,
                draw=glyph_set[name].draw,
                glyph_set=glyph_set,
            )
        )
    return cells


def literata_path(root: Path, *, style: str = ROMAN) -> Path:
    """Path of the pinned Literata VF of one style inside ``build/upstream``."""
    if style not in LITERATA_MEMBERS:
        raise StarsError(f"unknown style {style!r}; expected one of {', '.join(STYLES)}")
    member = LITERATA_MEMBERS[style]
    pins = {pin.key: pin for pin in upstream.load_pins(upstream.pin_file_for(root))}
    pin = pins.get("literata")
    if pin is None:
        raise StarsError("sources/upstream.toml has no [literata] section")
    matches = [m for m in pin.members if m.rsplit("/", 1)[-1] == member]
    if len(matches) != 1:
        raise StarsError(
            f"sources/upstream.toml [literata.members] must pin exactly one "
            f"{member}; found {len(matches)}"
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


def _style_headline(style: str, parameters: Parameters | None) -> str:
    """The line above one row: what this style does to the template."""
    if style != ITALIC:
        return "roman — the template as drawn: one petal straight up, nothing leans"
    if parameters is None:
        return "italic — template turned; ⁑ and ⁂ lean"
    italic = parameters.italic
    return (
        f"italic — template turned {italic.rotation:g}° (petals up-left and up-right, "
        f"none straight up); ⁑ and ⁂ lean {italic.stack_slant:g}° by displacing whole "
        "stars, never by shearing an outline"
    )


def render_svg(
    per_style: Mapping[str, Mapping[str, StarGlyph]],
    comparisons_by_style: Mapping[str, Sequence[Cell]] | None = None,
    *,
    parameters: Parameters | None = None,
) -> str:
    """The review artifact: one row per style, each beside its own Literata marks.

    Per style, a row of outlines at a common em size with each glyph's advance,
    baseline, cap height and bounding box drawn in, then the same run set as text
    at 34 px and 17 px — the sizes at which the small star's optical correction
    either works or does not. The roman comes first, so reading down a column is
    exactly what the italic's turn and lean do (D21).
    """
    comparisons_by_style = comparisons_by_style or {}
    scale = SVG_EM / 1000.0
    margin = 32.0

    parts: list[str] = []
    total_width = 640.0
    y = margin + 76.0
    for style, glyphs in per_style.items():
        parts.append(
            f'<text class="style" x="{margin:.1f}" y="{y:.1f}">'
            f"{_escape(_style_headline(style, parameters))}</text>"
        )
        cells = _svg_cells(glyphs, comparisons_by_style.get(style, ()))
        block, right, y = _svg_row(cells, y + 12.0, scale=scale, margin=margin)
        parts.extend(block)
        total_width = max(total_width, right)
        y += 26.0

    height = y - 26.0 + margin
    header = (
        f'<text class="title" x="{margin:.1f}" y="{margin + 22:.1f}">'
        "Asterwell Text — six-petal star family</text>"
        f'<text class="meta" x="{margin:.1f}" y="{margin + 40:.1f}">'
        "grey box: advance × (descender…ascender) · dashed: glyph bbox · "
        "rules: baseline and cap height (700)</text>"
        f'<text class="meta" x="{margin:.1f}" y="{margin + 56:.1f}">'
        "the two styles do not share outlines: the italic's are the roman's turned "
        "about their own centres, and its stacks are displaced (D21)</text>"
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


def _svg_row(
    cells: Sequence[Cell],
    top: float,
    *,
    scale: float,
    margin: float,
    gutter: float = 30.0,
    label_height: float = 46.0,
) -> tuple[list[str], float, float]:
    """One style's block: the outline row, its labels, and the text runs below it.

    Returns the SVG fragments, the width the row reached, and the y it ends at.
    """
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

    right = x - gutter + margin
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

    return parts, right, y


_SVG_STYLE = (
    ".page{fill:#ffffff}"
    ".glyph{fill:#111111}"
    ".em{fill:#f2f0ec;stroke:#d8d3ca;stroke-width:4}"
    ".bbox{fill:none;stroke:#c0392b;stroke-width:4;stroke-dasharray:16 12}"
    ".rule{stroke:#8a8478;stroke-width:1}"
    ".cap{stroke-dasharray:4 4}"
    "text{font-family:ui-sans-serif,-apple-system,Segoe UI,Helvetica,Arial,sans-serif}"
    ".title{font-size:17px;font-weight:600;fill:#111111}"
    ".style{font-size:14px;font-weight:600;fill:#111111}"
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
        comparisons = {
            style: literata_cells(root, style=style) for style in per_style
        }
        text = render_svg(per_style, comparisons, parameters=parameters)
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
