"""The six-petal star family: symmetry, cleanliness and the shipped envelopes.

Every property here is checked in **both styles**, because the two no longer
share one drawing: D21 turns the template 30° in the italic and leans the
stacked stars of ⁑ and ⁂ by 2.5°. The pair of tests that pins that relationship
down is :func:`test_the_italic_stars_are_the_roman_turned` (the italic outline
is the roman rotated about its own centre, to within rounding noise — never
sheared) and :func:`test_the_italic_stacks_lean_and_the_single_stars_do_not`
(the lean is a displacement of whole stars, by the mean-height rule).

The template tests are parametrised over all six outline tables, because D24
draws ✽'s four siblings from the same one: what makes ✻ ✼ ✾ ❃ a family with ✽
rather than four separate drawings is precisely that symmetry, centring,
cleanliness and the italic's turn hold for every one of them. The two properties
that do *not* generalise are called out where they are asserted: a pinwheel has
no mirror axis (that is what a pinwheel is), and a star with counters has more
than one contour.

Hermetic — everything here comes from ``sources/stars.toml`` and the geometry in
:mod:`asterwell_build.stars`; nothing reads ``build/upstream`` or a built font.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from pathlib import Path

import pathops
import pytest
from fontTools.pens.recordingPen import RecordingPen

from asterwell_build import cli, stars

#: ``glyf`` flag for a cubic off-curve point: fontTools' TTGlyphPen sets it when
#: it is handed a ``curveTo``, so its absence proves cu2qu ran.
CUBIC_FLAG = 0x80

#: Round-off allowance, in font units. Coordinates are integers and the cu2qu
#: tolerance is 1 unit, so every geometric identity below holds to well under it.
TOLERANCE = 1.0

#: What the family ships, per §7.3 (roman) and §15.1.4 (italic). Bounding boxes
#: are ``(xMin, yMin, xMax, yMax)`` of the rounded outline, which is what ``glyf``
#: stores and ``qa`` reads back. One dimension is 89.6% of the other because a
#: six-fold star's width and height are extents 30° apart — see the module
#: docstring. The italic turns the template 30°, which swaps those two: the
#: italic star is wider than tall, which is why ✽'s side bearings drop from 80
#: to 43 on an unchanged advance. The **roman rows are exactly what Step 4
#: shipped** — this revision must not move them by a unit.
EXPECTED: Mapping[str, Mapping[str, tuple[int, tuple[int, int, int, int]]]] = {
    "star.small": {
        "roman": (360, (18, 0, 342, 360)),
        "italic": (360, (0, 18, 360, 342)),
    },
    "uni273D": {
        "roman": (805, (80, -10, 725, 710)),
        "italic": (805, (43, 28, 763, 672)),
    },
    # ✽'s four siblings, all on ✽'s advance and centre (D24, §15.3.4). ✻ and ✼
    # share a box — ✼ *is* ✻ with a hole. ✾ has ✽'s box to the unit, because its
    # outer contour is ✽'s. ❃'s is a little wider and shorter than ✽'s: the
    # blades lean out of the axis directions, and the 1/√(1+s²) compensation
    # brings their reach back to ✽'s rather than to its bounding box.
    "uni273B": {
        "roman": (805, (83, -10, 722, 710)),
        "italic": (805, (43, 31, 763, 669)),
    },
    "uni273C": {
        "roman": (805, (83, -10, 722, 710)),
        "italic": (805, (43, 31, 763, 669)),
    },
    "uni273E": {
        "roman": (805, (80, -10, 725, 710)),
        "italic": (805, (43, 28, 763, 672)),
    },
    "uni2743": {
        "roman": (805, (43, 5, 762, 695)),
        "italic": (805, (58, -9, 747, 709)),
    },
    "uni204E": {
        "roman": (449, (63, 414, 387, 774)),
        "italic": (475, (58, 432, 418, 756)),
    },
    "uni2051": {
        "roman": (449, (63, 0, 387, 774)),
        "italic": (475, (48, 18, 427, 756)),
    },
    "uni2042": {
        "roman": (889, (76, 0, 814, 719)),
        "italic": (915, (65, 18, 839, 701)),
    },
}

#: Lean the italic's stacks are expected to show, as the x-difference between
#: two of their stars (§14.7, §15.1.4). ⁑: top minus bottom, 414 units apart.
#: ⁂: top minus the midpoint of its pair, 358.5 units apart — the same +16
#: Literata Italic's own ⁂ uses.
STACK_LEAN = {"uni2051": 19, "uni2042": 16}

#: Every table of ``stars.toml`` that describes an outline, and the glyph it is
#: drawn into. ``star.small`` also feeds the three composites.
TEMPLATES: Mapping[str, str] = {
    "full": "uni273D",
    "small": "star.small",
    "light": "uni273B",
    "open": "uni273C",
    "florette": "uni273E",
    "pinwheel": "uni2743",
}


def _table_of(glyph_name: str) -> str:
    """The ``stars.toml`` table a glyph is drawn from — :data:`TEMPLATES` inverted."""
    return next(table for table, name in TEMPLATES.items() if name == glyph_name)


#: Contours each template comes out as: the outer one, plus a counter apiece for
#: ✼'s hole and ✾'s three hollow petals (§15.3.5). ❃ is one contour — the
#: recommended sheared-blade construction, not DejaVu's slit-in-every-petal one.
CONTOURS = {"full": 1, "small": 1, "light": 1, "open": 2, "florette": 4, "pinwheel": 1}


@pytest.fixture(scope="session")
def parameters(repo_root: Path) -> stars.Parameters:
    return stars.load_parameters(stars.parameters_path_for(repo_root))


@pytest.fixture(scope="session")
def glyphs(parameters: stars.Parameters) -> Mapping[str, Mapping[str, stars.StarGlyph]]:
    return {style: stars.build_glyphs(parameters, style) for style in stars.STYLES}


def centred_glyph(
    outline: stars.Outline, parameters: stars.Parameters, style: str = "roman"
):
    """The template itself: the star as built for a style, centred on the origin."""
    return stars.outline_glyph(
        stars.star_path(outline, parameters, style=style), parameters, (0.0, 0.0)
    )


def about_its_centre(star: stars.StarGlyph) -> pathops.Path:
    """A built glyph's outline moved so its bounding box is centred on the origin.

    What the roman/italic comparison needs: the two stars sit at different
    places in different advances, and the claim under test is about their
    *shapes*.
    """
    x_min, y_min, x_max, y_max = star.bounds
    return outline_path(star.glyph).transform(
        1.0, 0.0, 0.0, 1.0, -(x_min + x_max) / 2.0, -(y_min + y_max) / 2.0
    )


def half_extent(
    outline: stars.Outline, parameters: stars.Parameters, style: str, direction: float
) -> float:
    """How far the centred template reaches in ``direction`` degrees.

    A six-fold star repeats every ``360/petals``, so only two extents exist:
    ``R`` along a petal axis, and ``(R − w)·cos(30°) + w`` in the valley halfway
    between two of them. Which one is the width and which the height is exactly
    what the italic's turn swaps over.
    """
    w = outline.petal_width * outline.radius
    step = 360.0 / parameters.petals
    across = (outline.radius - w) * math.cos(math.radians(step / 2.0)) + w
    off = abs(math.remainder(direction - stars.orientation_for(parameters, style), step))
    if off < 1e-9:
        return outline.radius
    assert off == pytest.approx(step / 2.0, abs=1e-9), (
        f"{direction}° is neither a petal axis nor a valley of this orientation"
    )
    return across


def points(glyph) -> list[tuple[int, int]]:
    return [tuple(point) for point in glyph.coordinates]


def outline_path(glyph) -> pathops.Path:
    """The finished, rounded outline back in :mod:`pathops` terms."""
    path = pathops.Path()
    glyph.draw(path.getPen(), None)
    return path


def rotated(path: pathops.Path, degrees: float) -> pathops.Path:
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    return path.transform(cos, sin, -sin, cos, 0.0, 0.0)


def mirrored(path: pathops.Path) -> pathops.Path:
    return path.transform(-1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mirrored_about(path: pathops.Path, degrees: float) -> pathops.Path:
    """Reflected across the line through the origin at ``degrees``.

    Which line matters: ✾'s mirror axes run through its hollow petals, so in the
    italic they are turned 30° along with everything else. ``mirrored`` is this
    with ``degrees = 90``.
    """
    angle = math.radians(2 * degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    return path.transform(cos, sin, sin, -cos, 0.0, 0.0)


def segments(path: pathops.Path) -> list[tuple[str, tuple]]:
    """A path's segments with the redundant explicit closing line dropped.

    skia writes the last edge back to the starting point as a ``lineTo`` the
    first time it builds a contour and leaves ``closePath`` to draw the same
    line the next time; the two spell one outline.
    """
    kept: list[tuple[str, tuple]] = []
    start: tuple[float, float] | None = None
    for verb, coordinates in path.segments:
        if verb == "moveTo":
            start = coordinates[0]
        elif verb == "closePath" and kept and kept[-1][0] == "lineTo":
            if kept[-1][1][0] == start:
                kept.pop()
        kept.append((verb, coordinates))
    return kept


def difference_area(one: pathops.Path, other: pathops.Path) -> float:
    """Area covered by exactly one of the two shapes: 0 if they coincide."""
    out = pathops.Path()
    pathops.xor([one], [other], out.getPen())
    return abs(out.area)


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #


def test_the_checked_in_parameters_are_the_design_s(parameters: stars.Parameters) -> None:
    assert parameters.petals == 6
    assert parameters.orientation == 90
    assert parameters.cubic_max_err == 1.0
    assert (parameters.full.radius, parameters.full.petal_width, parameters.full.hub) == (
        360,
        0.22,
        0.10,
    )
    assert (parameters.full.advance, parameters.full.center_y) == (805, 350)
    assert (parameters.small.radius, parameters.small.petal_width, parameters.small.hub) == (
        180,
        0.26,
        0.14,
    )
    # The small star is not the full one scaled: its petals and hub are
    # proportionally heavier, which is the design's optical correction.
    assert parameters.small.petal_width > parameters.full.petal_width
    assert parameters.small.hub > parameters.full.hub
    assert parameters.small.separation == 414  # 2·180 + 54, the ⁑/⁂ centre distance

    # D21: the italic turns the template a half-petal — 30° on a six-fold star,
    # so petals point up-left and up-right and none straight up — and leans the
    # stacked stars of ⁑ and ⁂ by the house 2.5°, which is where Literata
    # Italic's own colon (2.49°) and asterism (2.25°) sit.
    assert parameters.italic.rotation == 30
    assert parameters.italic.stack_slant == 2.5
    assert parameters.italic.lean == pytest.approx(math.tan(math.radians(2.5)))
    assert stars.orientation_for(parameters, "roman") == parameters.orientation
    assert stars.orientation_for(parameters, "italic") == parameters.orientation + 30


def test_the_siblings_of_the_full_star_are_it_with_one_number_changed(
    parameters: stars.Parameters,
) -> None:
    """D24: ✻ ✼ ✾ ❃ are ✽'s template, each with its own single technique.

    The checked-in values are Q8a–Q8d's recommendations. What matters as much as
    the numbers is what is *not* stated: a sibling takes ✽'s radius, advance and
    centre from ``[full]``, so the five cannot drift apart.
    """
    full, light = parameters.full, parameters.light
    open_star, florette, pinwheel = (
        parameters.open,
        parameters.florette,
        parameters.pinwheel,
    )

    # ✻ is ✽ made lighter — Unicode's own reading, which calls ✽ the heavy one.
    assert light.petal_width == 0.16 < full.petal_width
    assert light.hub == full.hub

    # ✼ is ✻ with a hole, and a ring wide enough to keep the petals joined
    # around it: 86 − 54 = 32 units, one stroke of the petal it meets.
    assert open_star.petal_width == light.petal_width
    assert (open_star.ring, open_star.open_centre) == (0.24, 0.15)
    assert (open_star.ring - open_star.open_centre) * open_star.radius == pytest.approx(32.4)

    # ✾ is ✽ with three alternate petals hollowed to a 43-unit wall.
    assert florette.petal_width == full.petal_width
    assert florette.wall == 0.12
    assert florette.hollow == (0, 2, 4)
    assert len(florette.hollow) * 2 == parameters.petals, "every other petal"

    # ❃ is ✽ with each blade leaning 17°, and the whole star pulled back in by
    # 1/√(1 + s²) so it reaches exactly as far as ✽ does.
    assert pinwheel.petal_width == full.petal_width
    assert pinwheel.shear == 0.30
    assert math.degrees(math.atan(pinwheel.shear)) == pytest.approx(16.7, abs=0.05)
    assert pinwheel.drawn_radius == pytest.approx(full.radius / math.sqrt(1.09))
    assert pinwheel.drawn_radius < full.radius == full.drawn_radius

    for name in stars.VARIANTS.values():
        variant = parameters.outline_for(name)
        assert isinstance(variant, stars.VariantStar)
        assert (variant.radius, variant.advance, variant.center_y) == (
            full.radius,
            full.advance,
            full.center_y,
        ), f"[{name}] is ✽'s size and placement"

    # Only the two that say so carry counters or a shear.
    assert [n for n in stars.VARIANTS.values() if parameters.outline_for(n).open_centre] == [
        "open"
    ]
    assert [n for n in stars.VARIANTS.values() if parameters.outline_for(n).hollow] == [
        "florette"
    ]
    assert [n for n in stars.VARIANTS.values() if parameters.outline_for(n).shear] == [
        "pinwheel"
    ]


def test_the_petal_half_angles_keep_neighbours_apart(parameters: stars.Parameters) -> None:
    # asin(w/(R−w)) must stay under 360/(2·petals) or adjacent petals would fuse
    # into a ring instead of meeting only at the hub.
    limit = 180.0 / parameters.petals
    full = math.degrees(stars.petal_half_angle(parameters.full))
    small = math.degrees(stars.petal_half_angle(parameters.small))
    light = math.degrees(stars.petal_half_angle(parameters.light))
    assert full == pytest.approx(16.4, abs=0.05)
    assert small == pytest.approx(20.6, abs=0.05)
    assert light == pytest.approx(11.0, abs=0.05)  # the lighter petal is a narrower one
    assert max(full, small, light) < limit


#: The sections of a well-formed ``stars.toml``, as text, for the rejection
#: tests below to leave out or spoil one at a time.
SECTIONS = {
    "template": "[template]\npetals = 6\norientation = 90\ncubic_max_err = 1.0\n",
    "full": (
        "[full]\nradius = 360\npetal_width = 0.22\nhub = 0.1\n"
        "advance = 805\ncenter_y = 350\n"
    ),
    "small": "[small]\nradius = 180\npetal_width = 0.26\nhub = 0.14\ngap = 54\n",
    "italic": "[italic]\nrotation = 30\nstack_slant = 2.5\n",
    "light": "[light]\npetal_width = 0.16\nhub = 0.1\n",
    "open": "[open]\npetal_width = 0.16\nring = 0.24\nopen_centre = 0.15\n",
    "florette": (
        "[florette]\npetal_width = 0.22\nhub = 0.1\nwall = 0.12\nhollow = [0, 2, 4]\n"
    ),
    "pinwheel": "[pinwheel]\npetal_width = 0.22\nhub = 0.1\nshear = 0.3\n",
}


def written(path: Path, *, drop: str = "", **edits: tuple[str, str]) -> Path:
    """Write a ``stars.toml`` that is the real one bar one section dropped or edited."""
    text = ""
    for name, section in SECTIONS.items():
        if name == drop:
            continue
        if name in edits:
            section = section.replace(*edits[name])
        text += section
    path.write_text(text, encoding="utf-8")
    return path


def test_impossible_parameters_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "stars.toml"

    assert stars.load_parameters(written(path)).petals == 6

    with pytest.raises(stars.StarsError, match="half-angle"):
        stars.load_parameters(written(path, small=("0.26", "0.45")))

    with pytest.raises(stars.StarsError, match=r"missing \[small\]"):
        stars.load_parameters(written(path, drop="small"))

    with pytest.raises(stars.StarsError, match="gap must be a number"):
        stars.load_parameters(written(path, small=("gap = 54", 'gap = "wide"')))

    # The italic treatment is not optional: without it there is no answer to
    # what the italic stars do, and silently falling back to the roman is the
    # bug D21 exists to undo.
    with pytest.raises(stars.StarsError, match=r"missing \[italic\]"):
        stars.load_parameters(written(path, drop="italic"))

    # A turn by a whole petal is no turn at all on a six-fold star.
    for degrees in ("60", "0", "-120"):
        with pytest.raises(stars.StarsError, match="onto itself"):
            stars.load_parameters(
                written(path, italic=("rotation = 30", f"rotation = {degrees}"))
            )

    with pytest.raises(stars.StarsError, match="stack_slant must be between"):
        stars.load_parameters(written(path, italic=("2.5", "60")))

    with pytest.raises(stars.StarsError, match="rotation must be a number"):
        stars.load_parameters(written(path, italic=("rotation = 30", 'rotation = "a bit"')))

    path.write_text("petals = ", encoding="utf-8")
    with pytest.raises(stars.StarsError, match="not valid TOML"):
        stars.load_parameters(path)

    with pytest.raises(stars.StarsError, match="missing parameter file"):
        stars.load_parameters(tmp_path / "nope.toml")


def test_a_sibling_that_would_not_be_a_sibling_is_rejected(tmp_path: Path) -> None:
    """The four variant tables state a shape and nothing else (D24, §15.3.2)."""
    path = tmp_path / "stars.toml"

    for table in stars.VARIANTS.values():
        with pytest.raises(stars.StarsError, match=rf"missing \[{table}\]"):
            stars.load_parameters(written(path, drop=table))

    # Size and placement are ✽'s, and stating them again is how two members of
    # one family quietly stop matching.
    for inherited in ("radius = 100", "advance = 700", "center_y = 400"):
        with pytest.raises(stars.StarsError, match="must not state"):
            stars.load_parameters(
                written(path, light=("hub = 0.1", f"hub = 0.1\n{inherited}"))
            )

    # ✼: the hole has to fit inside the ring that joins the petals around it…
    with pytest.raises(stars.StarsError, match="open_centre < ring"):
        stars.load_parameters(written(path, open=("open_centre = 0.15", "open_centre = 0.3")))
    # …and both are needed, since a hole with no ring is six loose petals.
    with pytest.raises(stars.StarsError, match="only one of ring and open_centre"):
        stars.load_parameters(written(path, open=("ring = 0.24\n", "")))

    # ✾: there is no hollowing a petal with a wall as thick as the petal.
    with pytest.raises(stars.StarsError, match="wall"):
        stars.load_parameters(written(path, florette=("wall = 0.12", "wall = 0.25")))
    with pytest.raises(stars.StarsError, match="only one of wall and hollow"):
        stars.load_parameters(written(path, florette=("wall = 0.12\n", "")))
    with pytest.raises(stars.StarsError, match="distinct petals"):
        stars.load_parameters(written(path, florette=("[0, 2, 4]", "[0, 2, 9]")))
    with pytest.raises(stars.StarsError, match="list of petal indices"):
        stars.load_parameters(written(path, florette=("[0, 2, 4]", '["top"]')))

    # ❃: a blade may lean, but not so far that it reaches the next one round.
    with pytest.raises(stars.StarsError, match="shear must be at least 0"):
        stars.load_parameters(written(path, pinwheel=("shear = 0.3", "shear = 0.7")))
    with pytest.raises(stars.StarsError, match="reaches its neighbour"):
        stars.load_parameters(
            written(path, pinwheel=("petal_width = 0.22", "petal_width = 0.35"))
        )


def test_an_open_centre_wider_than_its_ring_falls_apart(
    parameters: stars.Parameters,
) -> None:
    """Why ✼ has a ring at all, asserted on the geometry rather than the file.

    The six petals meet at the centre, so cutting a hole there is what could
    separate them; the ring around the hole is what keeps ✼ one shape. Rules that
    out at the file level (``open_centre < ring``), so this reaches past the
    loader to the construction, where the guard has to hold for anything else
    that builds a star.
    """
    apart = dataclasses.replace(parameters.open, ring=0.10, open_centre=0.15)
    for style in stars.STYLES:
        with pytest.raises(stars.StarsError, match="the ring does not join the petals"):
            stars.star_path(apart, parameters, style=style)


# --------------------------------------------------------------------------- #
# The template: symmetry and cleanliness
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("style", stars.STYLES)
@pytest.mark.parametrize("table", list(TEMPLATES))
def test_the_template_is_six_fold_symmetric(
    table: str, style: str, parameters: stars.Parameters
) -> None:
    """Turn the star a sixth of a turn and it lands on itself.

    Measured as area, not as points: the petal arcs are split at their extremes,
    which fall in different places within each petal, so the six petals are the
    same *shape* without being the same *point sequence*. True of the italic's
    turned template too — the hub's splits turn with it, so its six valleys stay
    identical to each other — and of ✾, whose three hollow petals are every
    *other* petal and so repeat every 120°, not every 60°.
    """
    outline = parameters.outline_for(table)
    step = 360.0 / parameters.petals
    # ✾ hollows alternate petals, so its own period is two petals wide.
    period = step * (2 if outline.hollow else 1)

    built = stars.star_path(outline, parameters, style=style)
    # The construction itself, before anything is rounded: turn the whole
    # template by that period and build it again, and the path is the same to
    # the last bit. (Comparing a *transformed* copy of it instead measures
    # skia's coincidence handling rather than the star — two float paths sharing
    # long stretches of boundary xor into spurious regions.)
    turned = dataclasses.replace(parameters, orientation=parameters.orientation + period)
    assert difference_area(built, stars.star_path(outline, turned, style=style)) == 0.0

    # And the shipped, integer-rounded outline, where rounding is free to move
    # the boundary by half a unit all the way round the perimeter.
    glyph = outline_path(centred_glyph(outline, parameters, style))
    assert difference_area(glyph, rotated(glyph, period)) < 0.02 * abs(glyph.area)
    # A half turn takes integers to integers, so it is exact — where it is a
    # symmetry at all. It is not one of ✾, whose period is 120°.
    if 180.0 % period == 0:
        assert difference_area(glyph, rotated(glyph, 180.0)) == 0.0

    # Every one of these but the pinwheel has mirror axes, and they run along
    # the petals — so the one to reflect in is the petal on the orientation
    # axis, which turns with the style (it is plain x → −x in the roman). ❃ is
    # the one shape here with no mirror axis at all: asserting that it *fails*
    # to mirror is what keeps a shear of 0 from quietly turning it back into ✽.
    mirror = difference_area(
        glyph, mirrored_about(glyph, stars.orientation_for(parameters, style))
    ) / abs(glyph.area)
    if outline.shear:
        assert mirror > 0.1, "a pinwheel is not its own mirror"
    else:
        assert mirror < 0.02
        if style == "roman":
            # The roman's axis is the vertical, where the reflection is exact.
            assert difference_area(glyph, mirrored(glyph)) == 0.0


@pytest.mark.parametrize("style", stars.STYLES)
@pytest.mark.parametrize("table", list(TEMPLATES))
def test_the_template_is_centred_on_the_origin(
    table: str, style: str, parameters: stars.Parameters
) -> None:
    outline = parameters.outline_for(table)
    glyph = centred_glyph(outline, parameters, style)
    # The silhouette's own points: ✾'s three counters carry 12, 11 and 11 points
    # apiece, so an average taken over those as well is not the centre of
    # anything. Where the counters sit is :func:`test_the_florette_is_the_full_
    # star_with_three_petals_hollowed`'s business.
    coordinates = points(glyph)[: glyph.endPtsOfContours[0] + 1]

    centroid = (
        sum(x for x, _ in coordinates) / len(coordinates),
        sum(y for _, y in coordinates) / len(coordinates),
    )
    assert centroid[0] == pytest.approx(0.0, abs=TOLERANCE)
    assert centroid[1] == pytest.approx(0.0, abs=TOLERANCE)

    # The box is centred on the origin too — the property the "square bbox"
    # shorthand was after — which holds for every one of these shapes, since all
    # six repeat under a half turn.
    assert glyph.xMin == pytest.approx(-glyph.xMax, abs=TOLERANCE)
    assert glyph.yMin == pytest.approx(-glyph.yMax, abs=TOLERANCE)

    if outline.shear:
        # ❃'s blades lean out of the axis directions, so its extents are not the
        # template's two; what is asked of it is that the compensation keeps its
        # reach at ✽'s rather than √(1+s²) beyond it.
        reach = max(math.dist((0, 0), point) for point in coordinates)
        assert reach == pytest.approx(outline.radius, rel=0.05)
        return

    # It is not square: a shape that repeats every 60° has its extents 30°
    # apart, so along a petal axis the half-extent is R and in the valley
    # between two petals it is (R−w)·cos30° + w. Which of the two is the height
    # is decided by the orientation: with the roman's 90 a petal points up and
    # the star is taller than wide; the italic's 120 turns that a half-petal and
    # the star comes out wider than tall.
    half_width = half_extent(outline, parameters, style, 0.0)
    half_height = half_extent(outline, parameters, style, 90.0)
    # Halfway between two petal axes, whichever way the style faces.
    valley = half_extent(
        outline,
        parameters,
        style,
        stars.orientation_for(parameters, style) + 180.0 / parameters.petals,
    )
    assert sorted((half_width, half_height)) == pytest.approx(
        sorted((outline.radius, valley))
    ), "the two extents are R and the valley half-width, in one order or the other"
    assert (half_width > half_height) == (style == "italic")

    assert glyph.yMax == pytest.approx(half_height, abs=TOLERANCE)
    assert glyph.yMin == pytest.approx(-half_height, abs=TOLERANCE)
    assert glyph.xMax == pytest.approx(half_width, abs=TOLERANCE)
    assert glyph.xMin == pytest.approx(-half_width, abs=TOLERANCE)


@pytest.mark.parametrize("style", stars.STYLES)
@pytest.mark.parametrize("table", list(TEMPLATES))
def test_the_union_leaves_one_contour_and_its_counters(
    table: str, style: str, parameters: stars.Parameters
) -> None:
    """One outer contour, plus one per counter, wound the way TrueType fills.

    ✽ ✻ ❃ are single contours; ✼ adds its round hole and ✾ its three hollowed
    petals. A counter that came out clockwise would be filled in rather than cut
    out, so the winding is as much of the shape as the points are.
    """
    outline = parameters.outline_for(table)
    path = stars.star_path(outline, parameters, style=style)
    contours = list(path.contours)
    assert len(contours) == CONTOURS[table]
    assert contours[0].clockwise, "TrueType fills clockwise outer contours"
    assert not any(counter.clockwise for counter in contours[1:]), (
        "a counter is wound the other way round"
    )
    glyph = centred_glyph(outline, parameters, style)
    assert glyph.numberOfContours == CONTOURS[table]


@pytest.mark.parametrize("style", stars.STYLES)
@pytest.mark.parametrize("table", list(TEMPLATES))
def test_the_union_leaves_no_overlapping_segments(
    table: str, style: str, parameters: stars.Parameters
) -> None:
    """A second pass over an already-clean path has nothing left to do."""
    path = stars.star_path(parameters.outline_for(table), parameters, style=style)
    again = pathops.simplify(path, fix_winding=True, keep_starting_points=True, clockwise=True)
    assert segments(again) == segments(path)
    assert again.area == path.area
    assert difference_area(again, path) == 0.0


@pytest.mark.parametrize("style", stars.STYLES)
@pytest.mark.parametrize("table", list(TEMPLATES))
def test_the_outline_is_quadratic_only(
    table: str, style: str, parameters: stars.Parameters
) -> None:
    glyph = centred_glyph(parameters.outline_for(table), parameters, style)
    assert not any(flag & CUBIC_FLAG for flag in glyph.flags), "a cubic survived cu2qu"

    pen = RecordingPen()
    glyph.draw(pen, None)
    operations = {operation for operation, _arguments in pen.value}
    assert "curveTo" not in operations
    assert operations == {"moveTo", "lineTo", "qCurveTo", "closePath"}


@pytest.mark.parametrize("style", stars.STYLES)
@pytest.mark.parametrize("table", list(TEMPLATES))
def test_the_extremes_are_on_curve_points(
    table: str, style: str, parameters: stars.Parameters
) -> None:
    """So the ``glyf`` box, which fontTools takes over control points too, is tight.

    The petal arcs are split where the *finished* outline is extreme, so this
    holds at the italic's orientation as much as at the roman's — the point of
    splitting there rather than in the petal's own frame — and for ❃, whose
    sheared bulb is an ellipse and whose extremes are therefore nowhere near the
    circle's cardinal angles.
    """
    glyph = centred_glyph(parameters.outline_for(table), parameters, style)
    on_curve = [
        point for point, flag in zip(points(glyph), glyph.flags, strict=True) if flag & 1
    ]
    assert min(x for x, _ in on_curve) == glyph.xMin
    assert max(x for x, _ in on_curve) == glyph.xMax
    assert min(y for _, y in on_curve) == glyph.yMin
    assert max(y for _, y in on_curve) == glyph.yMax


@pytest.mark.parametrize("table", list(TEMPLATES))
def test_turning_by_a_whole_petal_is_no_turn_at_all(
    table: str, parameters: stars.Parameters
) -> None:
    """Which way the italic turns does not matter: ±30° is one and the same star.

    A six-fold shape repeats every 60°, so every rotation in ``30 + k·60`` gives
    the same drawing — not merely the same shape, but the very same points, to
    the unit, because the construction turns the frame the petals, the hub
    splits and the counters are all keyed to. (Which point a contour *starts*
    at is skia's business, and a half-turn of the frame can move it, so the
    points are compared as a set.) That is what makes "petals up-left and
    up-right" a complete instruction, and why ``rotation`` may as well be
    written 30 as −30.
    """
    step = 360.0 / parameters.petals
    rotation = parameters.italic.rotation
    outline = parameters.outline_for(table)
    # ✾ is the exception: hollowing alternate petals takes two petals to come
    # back round, so a 60° turn genuinely hollows the *other* three.
    period = step * (2 if outline.hollow else 1)

    def drawn(degrees: float):
        turned = dataclasses.replace(
            parameters,
            italic=dataclasses.replace(parameters.italic, rotation=degrees),
        )
        glyph = centred_glyph(turned.outline_for(table), turned, "italic")
        return sorted(zip(points(glyph), glyph.flags, strict=True))

    base = drawn(rotation)
    for multiple in (-3, -2, -1, 1, 2, 3):
        assert drawn(rotation + multiple * period) == base
    if period == step:
        assert drawn(-rotation) == base  # −30 is 30 − 60
    else:
        assert drawn(-rotation) != base, (
            "−30° would hollow ✾'s other three petals; +30° is the one that puts "
            "them on the axis its `hollow` indices name"
        )
    assert drawn(rotation + step / 2.0) != base, "a quarter-petal is a real change"


# --------------------------------------------------------------------------- #
# The nine glyphs
# --------------------------------------------------------------------------- #


def test_the_family_is_the_nine_glyphs_in_assembly_order(
    glyphs: Mapping[str, Mapping[str, stars.StarGlyph]]
) -> None:
    """The component first, then every glyph that is drawn, then the composites.

    ✽'s four siblings sit straight after it (D24): they are outlines of their
    own, appended by the same path ✽ takes, and the order is the one
    ``assemble`` inserts them in.
    """
    for built in glyphs.values():
        assert list(built) == [
            "star.small",
            "uni273D",
            "uni273B",
            "uni273C",
            "uni273E",
            "uni2743",
            "uni204E",
            "uni2051",
            "uni2042",
        ]
        assert list(built) == list(stars.GLYPH_ORDER)
        assert built["star.small"].codepoint is None  # unencoded component
        encoded = [name for name in built if name != "star.small"]
        assert [built[name].codepoint for name in encoded] == [
            0x273D,
            0x273B,
            0x273C,
            0x273E,
            0x2743,
            0x204E,
            0x2051,
            0x2042,
        ]
        # The teardrop-spoked family is U+273B–U+273E plus U+2743, and every one
        # of the five is now drawn here rather than imported (D24).
        family = ("uni273B", "uni273C", "uni273D", "uni273E")
        assert {built[name].codepoint for name in family} == {0x273B, 0x273C, 0x273D, 0x273E}


@pytest.mark.parametrize("style", stars.STYLES)
@pytest.mark.parametrize("name", list(EXPECTED))
def test_every_glyph_has_its_envelope(
    name: str, style: str, glyphs: Mapping[str, Mapping[str, stars.StarGlyph]]
) -> None:
    advance, bounds = EXPECTED[name][style]
    star = glyphs[style][name]
    assert star.advance == advance
    assert star.bounds == bounds
    assert star.lsb == bounds[0], "lsb is xMin, the TrueType convention"


@pytest.mark.parametrize("style", stars.STYLES)
def test_the_envelopes_sit_where_the_design_asks(
    style: str,
    glyphs: Mapping[str, Mapping[str, stars.StarGlyph]],
    parameters: stars.Parameters,
) -> None:
    built = glyphs[style]
    radius = parameters.small.radius
    # Half-heights of the two templates in this style: R with a petal pointing
    # up (roman), the valley half-width when the italic has turned it.
    tall_full = half_extent(parameters.full, parameters, style, 90.0)
    tall_small = half_extent(parameters.small, parameters, style, 90.0)

    # ✽ is centred on its advance and on the cap-height midpoint; upright it
    # fills the cap height plus a 10-unit overshoot either side, and turned it
    # keeps that centre while trading height for width.
    full = built["uni273D"]
    assert full.height == pytest.approx(2 * tall_full, abs=TOLERANCE)
    assert full.width == pytest.approx(
        2 * half_extent(parameters.full, parameters, style, 0.0), abs=TOLERANCE
    )
    assert (full.bounds[1] + full.bounds[3]) / 2 == pytest.approx(
        parameters.full.center_y, abs=TOLERANCE
    )
    assert full.bounds[0] == pytest.approx(
        full.advance - full.bounds[2], abs=TOLERANCE
    ), "✽ is centred on its advance"

    # ✻ ✼ ✾ ❃ sit exactly where ✽ sits: same advance, same centre, both ways.
    # That is what makes them a family rather than four separate ornaments (D24).
    for name in stars.VARIANTS:
        sibling = built[name]
        assert sibling.advance == full.advance
        assert (sibling.bounds[0] + sibling.bounds[2]) / 2 == pytest.approx(
            full.advance / 2, abs=TOLERANCE
        )
        assert (sibling.bounds[1] + sibling.bounds[3]) / 2 == pytest.approx(
            parameters.full.center_y, abs=TOLERANCE
        )

    # ✻ is the lighter star, so it is narrower across the valleys than ✽ but
    # reaches exactly as far along its petals; ✼ is ✻ with a hole cut in it, so
    # its box is ✻'s to the unit.
    light, open_star = built["uni273B"], built["uni273C"]
    assert light.bounds == open_star.bounds
    assert light.width <= full.width and light.height <= full.height
    assert min(light.width, light.height) < min(full.width, full.height)
    assert max(light.width, light.height) == max(full.width, full.height), (
        "✻ reaches as far as ✽ along a petal; what is lighter is the valley"
    )

    # star.small is centred in its 360-unit advance box, vertically and across.
    small = built["star.small"]
    assert small.advance == 2 * radius
    assert small.bounds[1::2] == pytest.approx(
        (radius - tall_small, radius + tall_small), abs=TOLERANCE
    )
    assert small.bounds[0] == small.advance - small.bounds[2]

    # ⁎ and ⁑ set like the style's own asterisk; ⁑ hangs its lower star in
    # star.small's own box on the baseline.
    for name in ("uni204E", "uni2051"):
        assert built[name].advance == stars.ASTERISK_ADVANCE[style]
    assert built["uni204E"].bounds[1::2] == pytest.approx(
        (
            stars.ASTERISK_CENTER_Y - tall_small,
            stars.ASTERISK_CENTER_Y + tall_small,
        ),
        abs=TOLERANCE,
    )
    assert built["uni2051"].bounds[1::2] == pytest.approx(
        (radius - tall_small, stars.ASTERISK_CENTER_Y + tall_small), abs=TOLERANCE
    )

    # ⁂ keeps Literata's advance, one star above two.
    asterism = built["uni2042"]
    assert asterism.advance == stars.ASTERISM_ADVANCE[style]
    rise = parameters.small.separation * math.sin(math.radians(60.0))
    assert asterism.bounds[1] == pytest.approx(radius - tall_small, abs=TOLERANCE)
    assert asterism.bounds[3] == pytest.approx(
        radius + rise + tall_small, abs=TOLERANCE
    )

    # Nothing the turn or the lean does pushes a star outside its advance or
    # past Literata's line metrics (§15.1.4).
    for star in built.values():
        assert 0 <= star.bounds[0] and star.bounds[2] <= star.advance
        assert -10 <= star.bounds[1] and star.bounds[3] <= 774


@pytest.mark.parametrize("style", stars.STYLES)
def test_the_florette_is_the_full_star_with_three_petals_hollowed(
    style: str,
    glyphs: Mapping[str, Mapping[str, stars.StarGlyph]],
    parameters: stars.Parameters,
) -> None:
    """✾ is ✽ *with counters cut into it*, not a second drawing of a star.

    Its outer contour is ✽'s point for point — which is how DejaVu builds its
    own ✾ too (§14.8) — and the three counters sit on the petal at the
    orientation axis and its two ±120° siblings, so they turn with the star: the
    top petal in the roman, 0/120/240 after the italic's 30° (§15.3.3).
    """
    full, florette = glyphs[style]["uni273D"].glyph, glyphs[style]["uni273E"].glyph
    outer = full.endPtsOfContours[0] + 1
    assert florette.numberOfContours == 4
    assert points(florette)[:outer] == points(full), "✾'s silhouette is ✽'s exactly"
    assert glyphs[style]["uni273E"].bounds == glyphs[style]["uni273D"].bounds

    centre = (parameters.full.advance / 2.0, parameters.full.center_y)
    starts = [outer, *(end + 1 for end in florette.endPtsOfContours[1:-1])]
    directions = sorted(
        math.degrees(
            math.atan2(
                sum(y for _, y in points(florette)[start : end + 1]) / (end + 1 - start)
                - centre[1],
                sum(x for x, _ in points(florette)[start : end + 1]) / (end + 1 - start)
                - centre[0],
            )
        )
        % 360
        for start, end in zip(starts, florette.endPtsOfContours[1:])
    )
    expected = sorted(
        angle % 360
        for index, angle in enumerate(stars.petal_axes(parameters, style))
        if index in parameters.florette.hollow
    )
    assert directions == pytest.approx(expected, abs=0.5)
    assert expected == sorted(
        (parameters.orientation + (30 if style == "italic" else 0) + turn) % 360
        for turn in (0, 120, 240)
    )


@pytest.mark.parametrize("style", stars.STYLES)
def test_the_pinwheel_leans_every_blade_the_same_way(
    style: str,
    glyphs: Mapping[str, Mapping[str, stars.StarGlyph]],
    parameters: stars.Parameters,
) -> None:
    """❃'s six blades all sweep clockwise, which is what makes it a pinwheel.

    Measured on the finished outline, not on the construction: within each
    petal's own 60° sector, the point that reaches furthest from the centre sits
    on the *clockwise* side of that petal's axis, by roughly the ``atan(shear)``
    the blade was sheared by. If a shear ever came out with the wrong sign, or
    only some blades leaned, this is what would catch it.
    """
    star = glyphs[style]["uni2743"]
    centre = (parameters.full.advance / 2.0, parameters.full.center_y)
    step = 360.0 / parameters.petals
    leans = []
    for axis in stars.petal_axes(parameters, style):
        in_sector = [
            (math.dist(centre, point), math.remainder(
                math.degrees(math.atan2(point[1] - centre[1], point[0] - centre[0])) - axis,
                360.0,
            ))
            for point in points(star.glyph)
            if abs(
                math.remainder(
                    math.degrees(math.atan2(point[1] - centre[1], point[0] - centre[0]))
                    - axis,
                    360.0,
                )
            )
            < step / 2
        ]
        _reach, lean = max(in_sector)
        leans.append(lean)

    assert all(lean < -10.0 for lean in leans), (
        f"every blade leans clockwise off its axis; got {leans}"
    )
    assert max(leans) - min(leans) < 5.0, "and they all lean by the same amount"
    assert leans[0] == pytest.approx(
        -math.degrees(math.atan(parameters.pinwheel.shear)), abs=5.0
    )


@pytest.mark.parametrize("style", stars.STYLES)
def test_the_composites_are_translations_of_the_one_small_star(
    style: str,
    glyphs: Mapping[str, Mapping[str, stars.StarGlyph]],
    parameters: stars.Parameters,
) -> None:
    built = glyphs[style]
    counts = {"uni204E": 1, "uni2051": 2, "uni2042": 3}
    for name, count in counts.items():
        glyph = built[name].glyph
        assert glyph.isComposite()
        assert built[name].components == ("star.small",) * count
        assert len(glyph.components) == count
        for component in glyph.components:
            assert component.glyphName == "star.small"
            assert component.flags == 0x04  # ROUND_XY_TO_GRID, translation only
            assert not hasattr(component, "transform"), "no scale or rotation"
            assert (component.x, component.y) == (int(component.x), int(component.y))

    # Stood back up, ⁂ is still an equilateral triangle and ⁑ still a pair one
    # separation apart: the lean displaces the stack, it does not redraw it.
    centres = _centres(built["uni2042"].glyph.components, parameters, style)
    for a, b in itertools.combinations(centres, 2):
        assert math.dist(a, b) == pytest.approx(
            parameters.small.separation, abs=TOLERANCE
        ), "⁂ is an equilateral triangle of stars"

    pair = _centres(built["uni2051"].glyph.components, parameters, style)
    assert math.dist(pair[0], pair[1]) == pytest.approx(
        parameters.small.separation, abs=TOLERANCE
    )

    # In the italic the stack is tilted: ⁑'s two stars sit at different x, and
    # ⁂'s top star is right of the midpoint of its pair. In the roman neither is.
    leans_by = 0 if style == "roman" else 1
    two = built["uni2051"].glyph.components
    assert (two[0].x != two[1].x) == bool(leans_by)
    three = sorted(built["uni2042"].glyph.components, key=lambda c: c.y)
    midpoint = (three[0].x + three[1].x) / 2
    assert (three[2].x > midpoint) == bool(leans_by)


def _centres(
    components: Sequence[object],
    parameters: stars.Parameters,
    style: str = "roman",
) -> list[tuple[float, float]]:
    """Component offsets read back as star centres, with any lean undone.

    The inverse of :func:`stars.leaned`: subtract each star's share of the
    stack's tilt and the group is back to the upright arrangement the roman
    ships, which is the thing worth asserting shapes about.
    """
    radius = parameters.small.radius
    centres = [(component.x + radius, component.y + radius) for component in components]
    if style != "italic" or not centres:
        return centres
    lean = parameters.italic.lean
    mean_y = sum(y for _, y in centres) / len(centres)
    return [(x - (y - mean_y) * lean, y) for x, y in centres]


def test_the_italic_stars_are_the_roman_turned(
    glyphs: Mapping[str, Mapping[str, stars.StarGlyph]], parameters: stars.Parameters
) -> None:
    """D21: the italic's outline is the roman's rotated — never sheared, never redrawn.

    Measured as xor area against the roman turned about its own centre, both
    ways round: a six-fold star is symmetric under 60°, so +30 and −30 have to
    match equally well. What is left over is integer-rounding noise (about 1% of
    the area); a shear or a redraw is an order of magnitude more, which is what
    the 2% gate is set to catch.

    ✾ is the one that only turns one way: hollowing alternate petals halves the
    symmetry, so −30° would put the hollows on the other three petals. The turn
    it ships is +30, which is what keeps ``hollow``'s indices meaning the same
    petals in both styles — and this test pins that down rather than letting
    either direction pass.
    """
    roman, italic = glyphs["roman"], glyphs["italic"]
    rotation = parameters.italic.rotation
    drawn = [name for name in stars.GLYPH_ORDER if not roman[name].is_composite]
    assert sorted(drawn) == sorted(TEMPLATES.values()), (
        "every outline in the family is checked, not just ✽ and the small star"
    )

    for name in drawn:
        assert roman[name].advance == italic[name].advance, "the turn costs no width"
        assert points(roman[name].glyph) != points(italic[name].glyph), (
            "the two styles no longer share one drawing"
        )
        assert len(points(roman[name].glyph)) == len(points(italic[name].glyph))

        upright = about_its_centre(roman[name])
        turned = about_its_centre(italic[name])
        area = abs(turned.area)
        assert difference_area(upright, turned) > 0.2 * area, (
            "an unturned roman is nowhere near the italic"
        )
        alternating = bool(parameters.outline_for(_table_of(name)).hollow)
        for direction in (rotation, -rotation):
            ratio = difference_area(rotated(upright, direction), turned) / area
            if alternating and direction < 0:
                assert ratio > 0.1, "✾ turns one way only: the hollows have a side"
            else:
                assert ratio < 0.02, f"{name} turned {direction:+g}°: xor area {ratio:.4f}"

        # And it really is a rotation and not a skew that happens to land near
        # one: shearing the roman by the stacks' own slant does not pass.
        slant = math.tan(math.radians(parameters.italic.stack_slant))
        sheared = rotated(upright, rotation).transform(1.0, 0.0, slant, 1.0, 0.0, 0.0)
        assert difference_area(sheared, turned) > 0.02 * area


def test_the_italic_stacks_lean_and_the_single_stars_do_not(
    glyphs: Mapping[str, Mapping[str, stars.StarGlyph]], parameters: stars.Parameters
) -> None:
    """D21's second half: ⁑ and ⁂ tilt as groups; ⁎ (a stack of one) does not.

    Every star keeps the plain turned outline — what moves is where the stack
    puts it: x shifts by (its centre height − the stack's mean height) · tan
    2.5°. Over ⁑'s 414-unit separation that is 19 units between its two stars,
    and over ⁂'s 358.5-unit rise 16 between its top star and its pair — the very
    offset Literata Italic's own ⁂ uses.
    """
    roman, italic = glyphs["roman"], glyphs["italic"]
    lean = parameters.italic.lean

    def raw_centres(star: stars.StarGlyph) -> list[tuple[float, float]]:
        radius = parameters.small.radius
        return [(c.x + radius, c.y + radius) for c in star.glyph.components]

    # ⁎ is a stack of one: it moves with its advance and with nothing else, so
    # its star sits in the same place relative to that advance in both styles.
    for style, built in (("roman", roman), ("italic", italic)):
        (centre,) = raw_centres(built["uni204E"])
        assert centre[0] - built["uni204E"].advance / 2.0 == pytest.approx(
            0.0, abs=TOLERANCE
        ), f"⁎ stays centred on its advance in the {style}"
        assert centre[1] == stars.ASTERISK_CENTER_Y

    # ⁑: top minus bottom. ⁂: top minus the midpoint of the pair it stands on.
    for name, expected in STACK_LEAN.items():
        for style, built in (("roman", roman), ("italic", italic)):
            ordered = sorted(raw_centres(built[name]), key=lambda centre: centre[1])
            top = ordered[-1]
            below = ordered[:-1]
            base_x = sum(x for x, _ in below) / len(below)
            base_y = sum(y for _, y in below) / len(below)
            shift = top[0] - base_x
            if style == "roman":
                assert shift == pytest.approx(0.0, abs=TOLERANCE), f"{name} is upright"
            else:
                assert shift == pytest.approx(expected, abs=TOLERANCE)
                # …which is the mean-height rule, not a number typed in twice.
                assert shift == pytest.approx((top[1] - base_y) * lean, abs=TOLERANCE)

    # The lean is a displacement of whole stars: each one is still the very same
    # component, placed by an integer translation.
    for name in STACK_LEAN:
        for component in italic[name].glyph.components:
            assert component.glyphName == "star.small"
            assert not hasattr(component, "transform")

    # ✽'s siblings are single stars like ✽ and ⁎: they turn with the italic and
    # they never lean, so each stays centred on its own advance in both styles.
    for style, built in (("roman", roman), ("italic", italic)):
        for name in stars.VARIANTS:
            star = built[name]
            assert star.bounds[0] + star.bounds[2] == pytest.approx(
                star.advance, abs=2 * TOLERANCE
            ), f"{name} does not lean in the {style}"


# --------------------------------------------------------------------------- #
# The review artifact and the command
# --------------------------------------------------------------------------- #


def test_the_svg_draws_every_glyph_in_both_styles(parameters: stars.Parameters) -> None:
    per_style = {style: stars.build_glyphs(parameters, style) for style in stars.STYLES}
    svg = stars.render_svg(per_style, parameters=parameters)
    root = ElementTree.fromstring(svg)  # also proves it is well-formed XML
    assert root.tag == "{http://www.w3.org/2000/svg}svg"

    paths = root.findall(".//{http://www.w3.org/2000/svg}path")
    # per style: one outline row plus one run per text size, each covering all
    # nine glyphs.
    assert len(paths) == sum(
        len(built) * (1 + len(stars.SVG_TEXT_SIZES)) for built in per_style.values()
    )
    for element in paths:
        assert element.get("d", "").startswith("M")

    text = "".join(element.text or "" for element in root.iter())
    for style, built in per_style.items():
        assert f"{style} —" in text, "every row says which style it is"
        for name in built:
            assert name in text
        for star in built.values():
            if star.codepoint is not None:
                assert f"U+{star.codepoint:04X}" in text

    # The header states the treatment in the numbers the file actually carries,
    # and no longer claims the two styles share their outlines.
    assert f"turned {parameters.italic.rotation:g}°" in text
    assert f"lean {parameters.italic.stack_slant:g}°" in text
    assert "identical" not in text

    # The italic row's bboxes are the italic's, not a second copy of the roman's.
    for name, envelopes in EXPECTED.items():
        for advance, bounds in envelopes.values():
            assert f"bbox {bounds[0]} {bounds[1]} {bounds[2]} {bounds[3]}" in text
            assert f"adv {advance}" in text


def test_the_command_reports_without_touching_upstream(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stars.build(repo_root) == 0
    out = capsys.readouterr().out
    for style in stars.STYLES:
        assert f"{style}:" in out
    for name in stars.GLYPH_ORDER:
        assert name in out


def test_the_command_is_wired_into_the_cli() -> None:
    assert cli.COMMANDS["stars"] is stars.run
    assert not getattr(cli.COMMANDS["stars"], "is_stub", False)
    arguments = cli.build_parser().parse_args(["stars", "--svg", "build/stars.svg"])
    assert arguments.svg == Path("build/stars.svg")


@pytest.mark.parametrize("style", stars.STYLES)
def test_a_missing_upstream_font_names_the_fetch_task(
    style: str, tmp_path: Path, repo_root: Path
) -> None:
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "upstream.toml").write_text(
        (repo_root / "sources" / "upstream.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(stars.StarsError, match="mise run fetch"):
        stars.literata_path(tmp_path, style=style)
    with pytest.raises(stars.StarsError, match="mise run fetch"):
        stars.dejavu_path(tmp_path)


def test_the_review_compares_against_the_dejavu_glyphs_it_replaces(
    parameters: stars.Parameters,
) -> None:
    """The SVG's last five cells are DejaVu's own ✻ ✼ ✽ ✾ ❃ at the import scale.

    Four of those were the shipped outlines until D24 flipped their allowlist
    rows from `dejavu` to `custom`, so the review artifact shows what was
    replaced beside what replaced it. The scale is the importer's own
    ``k = 700/1493`` (D6), which is why DejaVu's 1716-unit dingbat advance lands
    on ✽'s 805.
    """
    names = [name for name, _label, _detail in stars.DEJAVU_COMPARISON_GLYPHS]
    assert names == ["uni273B", "uni273C", "uni273D", "uni273E", "uni2743"]
    # The four that changed hands are exactly the four new custom glyphs.
    assert set(names) - {"uni273D"} == set(stars.VARIANTS)
    assert stars.DEJAVU_SCALE == pytest.approx(700 / 1493)
    assert round(1716 * stars.DEJAVU_SCALE) == parameters.full.advance == 805


def test_each_style_compares_against_its_own_literata() -> None:
    """The italic row is judged beside Literata *Italic*, not beside the roman."""
    assert set(stars.LITERATA_MEMBERS) == set(stars.STYLES)
    assert "Italic" in stars.LITERATA_MEMBERS["italic"]
    assert "Italic" not in stars.LITERATA_MEMBERS["roman"]
    with pytest.raises(stars.StarsError, match="unknown style"):
        stars.literata_path(Path("."), style="oblique")
    with pytest.raises(stars.StarsError, match="unknown style"):
        stars.orientation_for(  # type: ignore[call-overload]
            stars.Parameters(
                petals=6,
                orientation=90.0,
                cubic_max_err=1.0,
                full=stars.FullStar(360, 0.22, 0.10, 805, 350),
                small=stars.SmallStar(180, 0.26, 0.14, 54),
                light=stars.VariantStar(360, 0.16, 0.10, 805, 350),
                open=stars.VariantStar(
                    360, 0.16, 0.10, 805, 350, ring=0.24, open_centre=0.15
                ),
                florette=stars.VariantStar(
                    360, 0.22, 0.10, 805, 350, wall=0.12, hollow=(0, 2, 4)
                ),
                pinwheel=stars.VariantStar(360, 0.22, 0.10, 805, 350, shear=0.30),
                italic=stars.ItalicTreatment(30.0, 2.5),
            ),
            "oblique",
        )
