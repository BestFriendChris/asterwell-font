"""The six-petal star family: symmetry, cleanliness and the shipped envelopes.

Hermetic — everything here comes from ``sources/stars.toml`` and the geometry in
:mod:`asterwell_build.stars`; nothing reads ``build/upstream`` or a built font.
"""

from __future__ import annotations

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

#: What the family ships, per §7.3. Bounding boxes are ``(xMin, yMin, xMax,
#: yMax)`` of the rounded outline, which is what ``glyf`` stores and ``qa``
#: reads back. The widths are 89.6% of the heights because a six-fold star's
#: width and height are extents 30° apart — see the module docstring.
EXPECTED: Mapping[str, Mapping[str, tuple[int, tuple[int, int, int, int]]]] = {
    "star.small": {
        "roman": (360, (18, 0, 342, 360)),
        "italic": (360, (18, 0, 342, 360)),
    },
    "uni273D": {
        "roman": (805, (80, -10, 725, 710)),
        "italic": (805, (80, -10, 725, 710)),
    },
    "uni204E": {
        "roman": (449, (63, 414, 387, 774)),
        "italic": (475, (76, 414, 400, 774)),
    },
    "uni2051": {
        "roman": (449, (63, 0, 387, 774)),
        "italic": (475, (76, 0, 400, 774)),
    },
    "uni2042": {
        "roman": (889, (76, 0, 814, 719)),
        "italic": (915, (89, 0, 827, 719)),
    },
}


@pytest.fixture(scope="session")
def parameters(repo_root: Path) -> stars.Parameters:
    return stars.load_parameters(stars.parameters_path_for(repo_root))


@pytest.fixture(scope="session")
def glyphs(parameters: stars.Parameters) -> Mapping[str, Mapping[str, stars.StarGlyph]]:
    return {style: stars.build_glyphs(parameters, style) for style in stars.STYLES}


def centred_glyph(outline: stars.Outline, parameters: stars.Parameters):
    """The template itself: the star as built, centred on the origin."""
    return stars.outline_glyph(stars.star_path(outline, parameters), parameters, (0.0, 0.0))


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


def test_the_petal_half_angles_keep_neighbours_apart(parameters: stars.Parameters) -> None:
    # asin(w/(R−w)) must stay under 360/(2·petals) or adjacent petals would fuse
    # into a ring instead of meeting only at the hub.
    limit = 180.0 / parameters.petals
    full = math.degrees(stars.petal_half_angle(parameters.full))
    small = math.degrees(stars.petal_half_angle(parameters.small))
    assert full == pytest.approx(16.4, abs=0.05)
    assert small == pytest.approx(20.6, abs=0.05)
    assert max(full, small) < limit


def test_impossible_parameters_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "stars.toml"
    template = "[template]\npetals = 6\norientation = 90\ncubic_max_err = 1.0\n"
    full = (
        "[full]\nradius = 360\npetal_width = 0.22\nhub = 0.1\n"
        "advance = 805\ncenter_y = 350\n"
    )
    small = "[small]\nradius = 180\npetal_width = 0.26\nhub = 0.14\ngap = 54\n"

    path.write_text(template + full + small, encoding="utf-8")
    assert stars.load_parameters(path).petals == 6

    path.write_text(
        template + full + small.replace("0.26", "0.45"), encoding="utf-8"
    )
    with pytest.raises(stars.StarsError, match="half-angle"):
        stars.load_parameters(path)

    path.write_text(template + full, encoding="utf-8")
    with pytest.raises(stars.StarsError, match=r"missing \[small\]"):
        stars.load_parameters(path)

    path.write_text(
        template + full + small.replace("gap = 54", 'gap = "wide"'), encoding="utf-8"
    )
    with pytest.raises(stars.StarsError, match="gap must be a number"):
        stars.load_parameters(path)

    path.write_text("petals = ", encoding="utf-8")
    with pytest.raises(stars.StarsError, match="not valid TOML"):
        stars.load_parameters(path)

    with pytest.raises(stars.StarsError, match="missing parameter file"):
        stars.load_parameters(tmp_path / "nope.toml")


# --------------------------------------------------------------------------- #
# The template: symmetry and cleanliness
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("size", ["full", "small"])
def test_the_template_is_six_fold_symmetric(size: str, parameters: stars.Parameters) -> None:
    """Turn the star a sixth of a turn and it lands on itself.

    Measured as area, not as points: the petal arcs are split at their extremes,
    which fall in different places within each petal, so the six petals are the
    same *shape* without being the same *point sequence*.
    """
    outline = getattr(parameters, size)
    step = 360.0 / parameters.petals

    built = stars.star_path(outline, parameters)
    # The construction itself, before anything is rounded: symmetric to within
    # skia's own arithmetic (a misplaced petal would differ by ~15% of the area).
    assert difference_area(built, rotated(built, step)) < 0.001 * abs(built.area)

    # And the shipped, integer-rounded outline, where rounding is free to move
    # the boundary by half a unit all the way round the perimeter.
    glyph = outline_path(centred_glyph(outline, parameters))
    assert difference_area(glyph, rotated(glyph, step)) < 0.02 * abs(glyph.area)
    # Half a turn and the mirror take integers to integers, so those are exact.
    assert difference_area(glyph, rotated(glyph, 180.0)) == 0.0
    assert difference_area(glyph, mirrored(glyph)) == 0.0


@pytest.mark.parametrize("size", ["full", "small"])
def test_the_template_is_centred_on_the_origin(size: str, parameters: stars.Parameters) -> None:
    outline = getattr(parameters, size)
    glyph = centred_glyph(outline, parameters)
    coordinates = points(glyph)

    centroid = (
        sum(x for x, _ in coordinates) / len(coordinates),
        sum(y for _, y in coordinates) / len(coordinates),
    )
    assert centroid[0] == pytest.approx(0.0, abs=TOLERANCE)
    assert centroid[1] == pytest.approx(0.0, abs=TOLERANCE)

    # The bounding box is centred on the origin too — the property the "square
    # bbox" shorthand was after. It is not square: a shape that repeats every
    # 60° has its width and height 30° apart, so with a petal pointing up the
    # height is 2R (tip to tip) and the width is 2·((R−w)·cos30° + w).
    step = 360.0 / parameters.petals
    w = outline.petal_width * outline.radius
    half_width = (outline.radius - w) * math.cos(math.radians(step / 2.0)) + w
    assert glyph.yMax == pytest.approx(outline.radius, abs=TOLERANCE)
    assert glyph.yMin == pytest.approx(-outline.radius, abs=TOLERANCE)
    assert glyph.xMax == pytest.approx(half_width, abs=TOLERANCE)
    assert glyph.xMin == pytest.approx(-half_width, abs=TOLERANCE)


@pytest.mark.parametrize("size", ["full", "small"])
def test_the_union_leaves_one_contour(size: str, parameters: stars.Parameters) -> None:
    path = stars.star_path(getattr(parameters, size), parameters)
    assert len(list(path.contours)) == 1
    assert path.clockwise, "TrueType fills clockwise outer contours"
    assert centred_glyph(getattr(parameters, size), parameters).numberOfContours == 1


@pytest.mark.parametrize("size", ["full", "small"])
def test_the_union_leaves_no_overlapping_segments(
    size: str, parameters: stars.Parameters
) -> None:
    """A second pass over an already-clean path has nothing left to do."""
    path = stars.star_path(getattr(parameters, size), parameters)
    again = pathops.simplify(path, fix_winding=True, keep_starting_points=True, clockwise=True)
    assert segments(again) == segments(path)
    assert again.area == path.area
    assert difference_area(again, path) == 0.0


@pytest.mark.parametrize("size", ["full", "small"])
def test_the_outline_is_quadratic_only(size: str, parameters: stars.Parameters) -> None:
    glyph = centred_glyph(getattr(parameters, size), parameters)
    assert not any(flag & CUBIC_FLAG for flag in glyph.flags), "a cubic survived cu2qu"

    pen = RecordingPen()
    glyph.draw(pen, None)
    operations = {operation for operation, _arguments in pen.value}
    assert "curveTo" not in operations
    assert operations == {"moveTo", "lineTo", "qCurveTo", "closePath"}


@pytest.mark.parametrize("size", ["full", "small"])
def test_the_extremes_are_on_curve_points(size: str, parameters: stars.Parameters) -> None:
    """So the ``glyf`` box, which fontTools takes over control points too, is tight."""
    glyph = centred_glyph(getattr(parameters, size), parameters)
    on_curve = [
        point for point, flag in zip(points(glyph), glyph.flags, strict=True) if flag & 1
    ]
    assert min(x for x, _ in on_curve) == glyph.xMin
    assert max(x for x, _ in on_curve) == glyph.xMax
    assert min(y for _, y in on_curve) == glyph.yMin
    assert max(y for _, y in on_curve) == glyph.yMax


# --------------------------------------------------------------------------- #
# The four glyphs
# --------------------------------------------------------------------------- #


def test_the_family_is_the_five_glyphs_in_assembly_order(
    glyphs: Mapping[str, Mapping[str, stars.StarGlyph]]
) -> None:
    for built in glyphs.values():
        assert list(built) == ["star.small", "uni273D", "uni204E", "uni2051", "uni2042"]
        assert built["star.small"].codepoint is None  # unencoded component
        encoded = ("uni273D", "uni204E", "uni2051", "uni2042")
        assert [built[name].codepoint for name in encoded] == [
            0x273D,
            0x204E,
            0x2051,
            0x2042,
        ]


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

    # ✽ fills the cap height plus a 10-unit overshoot either side, centred on
    # its advance.
    full = built["uni273D"]
    assert full.height == 2 * parameters.full.radius
    assert (full.bounds[1] + full.bounds[3]) / 2 == pytest.approx(
        parameters.full.center_y, abs=TOLERANCE
    )
    assert full.bounds[0] == pytest.approx(
        full.advance - full.bounds[2], abs=TOLERANCE
    ), "✽ is centred on its advance"

    # star.small fills its 360-unit advance box top to bottom, centred across it.
    small = built["star.small"]
    assert small.advance == 2 * parameters.small.radius
    assert (small.bounds[1], small.bounds[3]) == (0, 2 * parameters.small.radius)
    assert small.bounds[0] == small.advance - small.bounds[2]

    # ⁎ and ⁑ set like the style's own asterisk; ⁑ stands on the baseline.
    for name in ("uni204E", "uni2051"):
        assert built[name].advance == stars.ASTERISK_ADVANCE[style]
    assert built["uni204E"].bounds[1::2] == (
        stars.ASTERISK_CENTER_Y - parameters.small.radius,
        stars.ASTERISK_CENTER_Y + parameters.small.radius,
    )
    assert built["uni2051"].bounds[1::2] == (
        0,
        stars.ASTERISK_CENTER_Y + parameters.small.radius,
    )

    # ⁂ keeps Literata's advance and stands on the baseline, one star above two.
    asterism = built["uni2042"]
    assert asterism.advance == stars.ASTERISM_ADVANCE[style]
    rise = parameters.small.separation * math.sin(math.radians(60.0))
    assert asterism.bounds[1] == 0
    assert asterism.bounds[3] == pytest.approx(
        2 * parameters.small.radius + rise, abs=TOLERANCE
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

    centres = _centres(built["uni2042"].glyph.components, parameters)
    for a, b in itertools.combinations(centres, 2):
        assert math.dist(a, b) == pytest.approx(
            parameters.small.separation, abs=TOLERANCE
        ), "⁂ is an equilateral triangle of stars"

    pair = _centres(built["uni2051"].glyph.components, parameters)
    assert math.dist(pair[0], pair[1]) == pytest.approx(
        parameters.small.separation, abs=TOLERANCE
    )


def _centres(
    components: Sequence[object], parameters: stars.Parameters
) -> list[tuple[float, float]]:
    """Component offsets read back as star centres."""
    radius = parameters.small.radius
    return [(component.x + radius, component.y + radius) for component in components]


def test_both_styles_share_one_set_of_outlines(
    glyphs: Mapping[str, Mapping[str, stars.StarGlyph]]
) -> None:
    """The stars stay upright in the italic (D5): same outlines, different advances."""
    roman, italic = glyphs["roman"], glyphs["italic"]
    for name in ("star.small", "uni273D"):
        assert points(roman[name].glyph) == points(italic[name].glyph)
        assert list(roman[name].glyph.flags) == list(italic[name].glyph.flags)
        assert roman[name].advance == italic[name].advance
    for name in ("uni204E", "uni2051", "uni2042"):
        # Composites differ only in where the shared component is placed, which
        # follows the style's own advance.
        assert roman[name].advance != italic[name].advance
        assert roman[name].width == italic[name].width
        assert roman[name].height == italic[name].height


# --------------------------------------------------------------------------- #
# The review artifact and the command
# --------------------------------------------------------------------------- #


def test_the_svg_draws_every_glyph(parameters: stars.Parameters) -> None:
    built = stars.build_glyphs(parameters)
    svg = stars.render_svg(built)
    root = ElementTree.fromstring(svg)  # also proves it is well-formed XML
    assert root.tag == "{http://www.w3.org/2000/svg}svg"

    paths = root.findall(".//{http://www.w3.org/2000/svg}path")
    # one outline row plus one run per text size, each covering all five glyphs
    assert len(paths) == len(built) * (1 + len(stars.SVG_TEXT_SIZES))
    for element in paths:
        assert element.get("d", "").startswith("M")

    text = "".join(element.text or "" for element in root.iter())
    for name in built:
        assert name in text
    for star in built.values():
        if star.codepoint is not None:
            assert f"U+{star.codepoint:04X}" in text


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


def test_a_missing_upstream_font_names_the_fetch_task(tmp_path: Path, repo_root: Path) -> None:
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "upstream.toml").write_text(
        (repo_root / "sources" / "upstream.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(stars.StarsError, match="mise run fetch"):
        stars.literata_path(tmp_path)
