"""Unit tests for the QA gate.

Every check here is exercised twice: once on a font that is *correct*, and once
on the same font with one thing deliberately broken. A check that only ever runs
against the real build is a check nobody has seen fail, and a check nobody has
seen fail is indistinguishable from one that cannot.

The fonts are synthetic — eleven glyphs, two axes, built with
:class:`fontTools.fontBuilder.FontBuilder` — but not arbitrary. They are shaped
like the real thing in the four ways the checks care about: a *source* font
stands in for the pinned Literata (five encoded glyphs, one of them the ⁂ the
build replaces in place), a *built* font adds the real star family from
``sources/stars.toml`` plus one stand-in DejaVu import, one glyph varies with
weight and the imports do not, and the envelope of the whole design space sits
in a glyph that varies — so a static cut from the variable font legitimately has
a different ``head`` box, which is the correction Step 7 measured and §9.1's
static clause now reflects.

fontbakery itself is never run here: it is minutes of wall clock and it is not
this repository's code. What *is* this repository's code — the argv it builds
and the report it reads back — is tested directly.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.varLib.instancer import instantiateVariableFont

from asterwell_build import allowlist, assemble, instances, qa, stars

# --------------------------------------------------------------------------- #
# The synthetic family
# --------------------------------------------------------------------------- #

UPEM = 1000
ASCENT = 1177
DESCENT = -308

#: Literata's own designer credit, which name ID 9 is built by extending.
DESIGNER = (
    "Latin by Veronika Burian and Jose Scaglione. Greek by Irene Vlachou. "
    "Cyrillic by Vera Evstafieva."
)

FAMILY = assemble.Family(
    family="Asterwell Text",
    ps_family="AsterwellText",
    version="1.000",
    vendor_id="ASTW",
    repo_url="https://example.invalid/asterwell",
    license_url="https://example.invalid/ofl",
    copyright_holder="The Asterwell Text Project Authors (https://example.invalid/asterwell)",
    copyright_year="2026",
    reserved_font_name="Asterwell",
)

#: The five glyphs the stand-in Literata has, and what they are encoded as.
SOURCE_CMAP = {
    0x48: "H",
    0x78: "x",
    0x2A: "asterisk",
    0x2022: "bullet",
    0x2042: "uni2042",
}

#: The one stand-in import, plus the star family the build appends.
IMPORTED = "uni2731"
IMPORTED_CODEPOINT = 0x2731

#: The inventory the shaping check is given in these tests. The real 43-character
#: one belongs to the real fonts; a synthetic font that had to cover it would be
#: the real font.
INVENTORY = "•⁂✽"

WEIGHTS = (
    (200, "ExtraLight"),
    (300, "Light"),
    (400, "Regular"),
    (500, "Medium"),
    (600, "SemiBold"),
    (700, "Bold"),
    (800, "ExtraBold"),
    (900, "Black"),
)


def _box(x_min: int, y_min: int, x_max: int, y_max: int):
    pen = TTGlyphPen(None)
    pen.moveTo((x_min, y_min))
    pen.lineTo((x_max, y_min))
    pen.lineTo((x_max, y_max))
    pen.lineTo((x_min, y_max))
    pen.closePath()
    glyph = pen.glyph()
    glyph.recalcBounds(glyfTable=None)
    return glyph


def _grow(width: int):
    """A ``gvar`` entry that makes a four-point box wider — and its advance
    longer — at the heavy end of the weight axis. Four coordinates then four
    phantom points; the second phantom is the advance."""
    return [
        TupleVariation(
            {"wght": (0.0, 1.0, 1.0)},
            [(0, 0), (width, 0), (width, 0), (0, 0), (0, 0), (width, 0), (0, 0), (0, 0)],
        )
    ]


def _style(italic: bool) -> assemble.Style:
    return assemble.STYLES[1] if italic else assemble.STYLES[0]


def _build(path: Path, *, italic: bool, parameters: stars.Parameters, built: bool) -> Path:
    """One synthetic font: the stand-in Literata, or the family built from it.

    ``built=False`` is the input the family is assembled from; ``built=True`` is
    what ``qa`` reads. The difference is exactly the build's: four appended
    glyphs (the star family's shared outline, ✽, ⁎, ⁑) plus one import, ⁂
    replaced in place, and the family's own name table.
    """
    style = _style(italic)
    star_glyphs = stars.build_glyphs(parameters, style.key)
    asterisk_advance = stars.ASTERISK_ADVANCE[style.key]
    asterism_advance = stars.ASTERISM_ADVANCE[style.key]

    order = [".notdef", "H", "x", "asterisk", "bullet", "uni2042"]
    glyphs = {
        ".notdef": _box(0, 0, 100, 700),
        # The envelope of the whole family lives in a glyph that *varies*, so a
        # static cut from this font has a different head box — by design.
        "H": _box(-377, -268, 1443, 1136),
        "x": _box(30, 0, 571, 507),
        "asterisk": _box(54, 407, 395, 782),
        "bullet": _box(79, 201, 293, 413),
        "uni2042": _box(54, 0, 835, 782),
    }
    metrics = {
        ".notdef": (600, 0),
        "H": (839, -377),
        "x": (601, 30),
        "asterisk": (asterisk_advance, 54),
        "bullet": (372, 79),
        "uni2042": (asterism_advance, 54),
    }
    cmap = dict(SOURCE_CMAP)
    variations: dict[str, list[TupleVariation]] = {
        name: [] for name in order
    }
    variations["H"] = _grow(60)
    variations["x"] = _grow(40)

    if built:
        order += [IMPORTED, *stars.GLYPH_ORDER[:-1]]
        glyphs[IMPORTED] = _box(62, 0, 743, 700)
        metrics[IMPORTED] = (805, 62)
        cmap[IMPORTED_CODEPOINT] = IMPORTED
        variations[IMPORTED] = []
        for name, star in star_glyphs.items():
            glyphs[name] = star.glyph
            metrics[name] = (star.advance, star.lsb)
            variations[name] = []
            if star.codepoint is not None:
                cmap[star.codepoint] = name

    builder = FontBuilder(UPEM, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(cmap)
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=ASCENT, descent=DESCENT, lineGap=0)
    builder.setupNameTable(
        {
            "familyName": "Stand-in",
            "styleName": style.subfamily,
            "uniqueFontIdentifier": "0.000;TEST;Standin",
            "fullName": f"Stand-in {style.subfamily}",
            "psName": f"Standin-{style.subfamily}",
            "version": "Version 0.000",
        }
    )
    builder.setupOS2(
        sTypoAscender=ASCENT,
        sTypoDescender=DESCENT,
        sTypoLineGap=0,
        usWinAscent=ASCENT,
        usWinDescent=-DESCENT,
        sCapHeight=700,
        sxHeight=507,
        achVendID="ASTW",
        usWeightClass=400,
        fsSelection=(0x01 if italic else 0x40) | 0x80,
    )
    builder.setupPost()
    builder.setupFvar(
        axes=[("opsz", 7, 12, 72, "Optical Size"), ("wght", 200, 400, 900, "Weight")],
        instances=[
            {
                "location": {"opsz": instances.STATIC_OPSZ, "wght": weight},
                "stylename": name if not italic else ("Italic" if name == "Regular" else f"{name} Italic"),
            }
            for weight, name in WEIGHTS
        ],
    )
    builder.setupGvar(variations)
    builder.setupStat(
        [
            {
                "tag": "opsz",
                "name": "Optical Size",
                "values": [
                    {"value": 7, "name": "7pt"},
                    {"value": instances.STATIC_OPSZ, "name": "12pt", "flags": 0x2},
                    {"value": 72, "name": "72pt"},
                ],
            },
            {
                "tag": "wght",
                "name": "Weight",
                "values": [
                    (
                        {"value": 400, "name": "Regular", "flags": 0x2, "linkedValue": 700}
                        if weight == 400
                        else {"value": weight, "name": name}
                    )
                    for weight, name in WEIGHTS
                ],
            },
        ],
        elidedFallbackName="Italic" if italic else "Regular",
    )
    font = builder.font
    # D18 again: `getDebugName` prefers a Macintosh record, so a synthetic font
    # that kept them would test the wrong half of the name table.
    font["name"].names = [record for record in font["name"].names if record.platformID != 1]
    font["name"].setName(DESIGNER, 9, *assemble.WINDOWS_ENGLISH)
    if built:
        assemble.apply_names(font, FAMILY, style)
        font["head"].fontRevision = FAMILY.font_revision
    assemble.regenerate_hvar(font)
    path.parent.mkdir(parents=True, exist_ok=True)
    font.save(path)
    return path


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def parameters(repo_root: Path) -> stars.Parameters:
    return stars.load_parameters(stars.parameters_path_for(repo_root))


@pytest.fixture(scope="session")
def family_paths(
    tmp_path_factory: pytest.TempPathFactory, parameters: stars.Parameters
) -> dict[str, Path]:
    """The four files the checks read: two sources, two built fonts."""
    directory = tmp_path_factory.mktemp("family")
    paths = {}
    for key, italic in (("roman", False), ("italic", True)):
        paths[f"source-{key}"] = _build(
            directory / f"Source-{key}.ttf", italic=italic, parameters=parameters, built=False
        )
        paths[f"built-{key}"] = _build(
            directory / f"Built-{key}.ttf", italic=italic, parameters=parameters, built=True
        )
    return paths


ROWS = (
    ("U+2022", "bullet", allowlist.SOURCE_LITERATA, "bullet"),
    ("U+2042", "uni2042", allowlist.SOURCE_CUSTOM, "uni2042"),
    ("U+204E", "uni204E", allowlist.SOURCE_CUSTOM, "uni204E"),
    ("U+2051", "uni2051", allowlist.SOURCE_CUSTOM, "uni2051"),
    ("U+273D", "uni273D", allowlist.SOURCE_CUSTOM, "uni273D"),
    ("U+2731", "uni2731", allowlist.SOURCE_DEJAVU, "uni2731"),
)


@pytest.fixture(scope="session")
def rows() -> list[allowlist.Row]:
    """A six-row stand-in manifest covering all three sources."""
    built = []
    for spelling, _name, source, glyph_name in ROWS:
        codepoint = int(spelling[2:], 16)
        built.append(
            allowlist.Row(
                codepoint=codepoint,
                char=chr(codepoint),
                unicode_name=f"TEST {spelling}",
                block="Test Block",
                source=source,
                glyph_name=glyph_name,
                group="test",
                emoji_presentation="no",
                note="",
            )
        )
    return built


@pytest.fixture(scope="session")
def source_codepoints() -> frozenset[int]:
    return frozenset(SOURCE_CMAP)


@pytest.fixture(scope="session")
def reference(
    family_paths: dict[str, Path], parameters: stars.Parameters
) -> qa.StyleReference:
    """What the roman outputs are checked against."""
    style = _style(False)
    source = TTFont(family_paths["source-roman"], lazy=True)
    try:
        frozen = assemble.invariants(source)
        names = assemble.name_strings(source, FAMILY, style)
    finally:
        source.close()
    data = family_paths["built-roman"].read_bytes()
    return qa.StyleReference(
        style=style,
        source=family_paths["source-roman"],
        invariants=frozen,
        codepoints=frozenset(SOURCE_CMAP),
        names=names,
        stars=stars.build_glyphs(parameters, style.key),
        variable_path=family_paths["built-roman"],
        instances=(),
        advances=qa.default_advances(data, UPEM),
        upem=UPEM,
    )


@pytest.fixture
def built(family_paths: dict[str, Path]) -> Iterator[TTFont]:
    """A fresh, writable copy of the built roman for a test to break."""
    font = assemble.open_source_font(family_paths["built-roman"])
    yield font
    font.close()


@pytest.fixture(scope="session")
def variable_target(family_paths: dict[str, Path]) -> qa.Target:
    return qa.Target(family_paths["built-roman"], qa.KIND_VARIABLE, "roman")


@pytest.fixture(scope="session")
def bold_instance() -> instances.Instance:
    return instances.Instance(weight=700, subfamily="Bold", italic=False)


@pytest.fixture(scope="session")
def static_path(
    tmp_path_factory: pytest.TempPathFactory,
    family_paths: dict[str, Path],
    bold_instance: instances.Instance,
) -> Path:
    """One static cut from the synthetic variable font, exactly as the build
    cuts the real ones."""
    directory = tmp_path_factory.mktemp("static")
    output = directory / "AsterwellText-Bold.ttf"
    font = assemble.open_source_font(family_paths["built-roman"])
    static = instantiateVariableFont(
        font, bold_instance.location, inplace=True, updateFontNames=True
    )
    instances.apply_style_bits(static, bold_instance)
    instances.apply_names(static, FAMILY, bold_instance)
    static.save(output)
    font.close()
    return output


@pytest.fixture(scope="session")
def static_target(static_path: Path, bold_instance: instances.Instance) -> qa.Target:
    return qa.Target(static_path, qa.KIND_STATIC, "roman", instance=bold_instance)


def _bytes(font: TTFont) -> bytes:
    buffer = BytesIO()
    font.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 9.1 Metrics freeze
# --------------------------------------------------------------------------- #


def test_metrics_freeze_passes_on_the_built_variable_font(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    assert qa.metrics_problems(built, variable_target, reference) == []


def test_metrics_freeze_catches_a_moved_ascent(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    built["hhea"].ascent += 1
    problems = qa.metrics_problems(built, variable_target, reference)
    assert any("ascent" in problem for problem in problems)


def test_metrics_freeze_catches_a_moved_bounding_box_on_a_variable_font(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    built["head"].yMax += 5
    problems = qa.metrics_problems(built, variable_target, reference)
    assert any("head_bbox" in problem for problem in problems)


def test_metrics_freeze_catches_a_lost_use_typo_metrics_bit(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    built["OS/2"].fsSelection &= ~instances.FS_USE_TYPO_METRICS
    assert qa.metrics_problems(built, variable_target, reference)


def test_a_static_keeps_its_own_envelope_and_still_passes(
    static_path: Path, static_target: qa.Target, reference: qa.StyleReference
) -> None:
    """The Step-7 correction, as a test: Bold's box is bigger than the variable
    font's and that is not drift — but the vertical metrics must still match."""
    font = TTFont(static_path)
    try:
        assert qa.metrics_problems(font, static_target, reference) == []
        measured = assemble.invariants(font)
        assert measured.head_bbox != reference.invariants.head_bbox
        assert measured.ascent == reference.invariants.ascent
        assert "head bbox" in qa.envelope(font)
    finally:
        font.close()


def test_a_static_that_moves_the_line_box_still_fails(
    static_path: Path, static_target: qa.Target, reference: qa.StyleReference
) -> None:
    font = TTFont(static_path)
    try:
        font["OS/2"].usWinDescent += 40
        assert qa.metrics_problems(font, static_target, reference)
    finally:
        font.close()


# --------------------------------------------------------------------------- #
# 9.2 Coverage
# --------------------------------------------------------------------------- #


def test_coverage_passes_on_the_built_font(
    built: TTFont, rows: list[allowlist.Row], source_codepoints: frozenset[int]
) -> None:
    assert qa.coverage_problems(built, rows, source_codepoints) == []


def _drop_codepoint(font: TTFont, codepoint: int) -> None:
    for subtable in font["cmap"].tables:
        subtable.cmap.pop(codepoint, None)


def test_coverage_catches_an_unmapped_row(
    built: TTFont, rows: list[allowlist.Row], source_codepoints: frozenset[int]
) -> None:
    _drop_codepoint(built, IMPORTED_CODEPOINT)
    problems = qa.coverage_problems(built, rows, source_codepoints)
    assert any("not in cmap" in problem for problem in problems)


def test_coverage_catches_a_row_pointed_at_another_glyph(
    built: TTFont, rows: list[allowlist.Row], source_codepoints: frozenset[int]
) -> None:
    for subtable in built["cmap"].tables:
        if IMPORTED_CODEPOINT in subtable.cmap:
            subtable.cmap[IMPORTED_CODEPOINT] = "bullet"
    problems = qa.coverage_problems(built, rows, source_codepoints)
    assert any("map to another glyph" in problem for problem in problems)


def test_coverage_catches_an_empty_glyph(
    built: TTFont, rows: list[allowlist.Row], source_codepoints: frozenset[int]
) -> None:
    built["glyf"][IMPORTED] = _box(0, 0, 0, 0)
    built["glyf"][IMPORTED].numberOfContours = 0
    problems = qa.coverage_problems(built, rows, source_codepoints)
    assert any("empty glyph" in problem for problem in problems)


def test_coverage_catches_an_unapproved_cmap_entry(
    built: TTFont, rows: list[allowlist.Row], source_codepoints: frozenset[int]
) -> None:
    for subtable in built["cmap"].tables:
        subtable.cmap[0x2732] = IMPORTED
    problems = qa.coverage_problems(built, rows, source_codepoints)
    assert any("does not approve" in problem for problem in problems)


def test_coverage_catches_a_code_point_the_source_had_and_the_build_lost(
    built: TTFont, rows: list[allowlist.Row], source_codepoints: frozenset[int]
) -> None:
    _drop_codepoint(built, 0x48)
    problems = qa.coverage_problems(built, rows, source_codepoints)
    assert any("Literata mapped are gone" in problem for problem in problems)


def test_star_small_is_a_component_and_not_a_character(built: TTFont) -> None:
    assert qa.component_only_problems(built) == []


def test_an_encoded_star_small_fails(built: TTFont) -> None:
    for subtable in built["cmap"].tables:
        subtable.cmap[0xE000] = stars.SMALL
    problems = qa.component_only_problems(built)
    assert any("unencoded component" in problem for problem in problems)


def test_a_missing_star_small_fails(built: TTFont) -> None:
    order = [name for name in built.getGlyphOrder() if name != stars.SMALL]
    built.setGlyphOrder(order)
    assert qa.component_only_problems(built) == [f"{stars.SMALL} is not in the glyph order"]


# --------------------------------------------------------------------------- #
# 9.3 Shaping
# --------------------------------------------------------------------------- #


def test_shaping_passes_on_the_built_variable_font(
    family_paths: dict[str, Path],
    variable_target: qa.Target,
    reference: qa.StyleReference,
    rows: list[allowlist.Row],
) -> None:
    data = family_paths["built-roman"].read_bytes()
    assert qa.shaping_problems(data, variable_target, reference, rows, INVENTORY) == []


def test_shaping_catches_a_character_that_reaches_notdef(
    built: TTFont,
    variable_target: qa.Target,
    reference: qa.StyleReference,
    rows: list[allowlist.Row],
) -> None:
    _drop_codepoint(built, IMPORTED_CODEPOINT)
    problems = qa.shaping_problems(
        _bytes(built), variable_target, reference, rows, INVENTORY
    )
    assert any(".notdef" in problem for problem in problems)


def test_shaping_catches_a_symbol_that_stopped_being_invariant(
    built: TTFont,
    variable_target: qa.Target,
    reference: qa.StyleReference,
    rows: list[allowlist.Row],
) -> None:
    """An import that grew ``gvar`` deltas would thicken with the prose, which is
    exactly what D5 says the symbols must not do."""
    built["gvar"].variations[IMPORTED] = _grow(90)
    assemble.regenerate_hvar(built)
    problems = qa.shaping_problems(
        _bytes(built), variable_target, reference, rows, INVENTORY
    )
    assert any("change advance across the design space" in problem for problem in problems)


def test_shaping_catches_a_prose_glyph_that_stopped_varying(
    built: TTFont,
    variable_target: qa.Target,
    reference: qa.StyleReference,
    rows: list[allowlist.Row],
) -> None:
    for name in qa.VARYING_SAMPLES:
        built["gvar"].variations[name] = []
    assemble.regenerate_hvar(built)
    problems = qa.shaping_problems(
        _bytes(built), variable_target, reference, rows, INVENTORY
    )
    assert any("do not vary" in problem for problem in problems)


def test_a_static_must_reproduce_the_variable_fonts_default_advance(
    static_path: Path,
    static_target: qa.Target,
    reference: qa.StyleReference,
    rows: list[allowlist.Row],
) -> None:
    data = static_path.read_bytes()
    assert qa.shaping_problems(data, static_target, reference, rows, INVENTORY) == []

    font = TTFont(static_path)
    try:
        font["hmtx"][IMPORTED] = (900, font["hmtx"][IMPORTED][1])
        broken = _bytes(font)
    finally:
        font.close()
    problems = qa.shaping_problems(broken, static_target, reference, rows, INVENTORY)
    assert any("default advance" in problem for problem in problems)


# --------------------------------------------------------------------------- #
# 9.4 Names and bits
# --------------------------------------------------------------------------- #


def test_names_pass_on_the_built_variable_font(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    assert qa.name_problems(built, variable_target, reference, FAMILY) == []


def test_names_come_from_the_same_function_the_build_writes_from(
    reference: qa.StyleReference,
) -> None:
    assert reference.names[6] == "AsterwellText-Regular"
    assert reference.names[25] == "AsterwellText"
    assert reference.names[14] == FAMILY.license_url
    assert DESIGNER.rstrip(".") in reference.names[9]


def test_names_catch_a_wrong_postscript_name(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    built["name"].setName("Wrong-Name", 6, *assemble.WINDOWS_ENGLISH)
    problems = qa.name_problems(built, variable_target, reference, FAMILY)
    assert any("ID 6" in problem for problem in problems)


def test_names_catch_a_macintosh_record(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    built["name"].setName("Asterwell Text", 1, 1, 0, 0)
    problems = qa.name_problems(built, variable_target, reference, FAMILY)
    assert any("Macintosh" in problem for problem in problems)


def test_names_catch_the_reserved_font_name_outside_the_copyright(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    built["name"].setName(
        'Asterwell Text with Reserved Font Name "Asterwell"',
        10,
        *assemble.WINDOWS_ENGLISH,
    )
    problems = qa.name_problems(built, variable_target, reference, FAMILY)
    assert any("Reserved Font Name" in problem for problem in problems)


def test_names_catch_an_upstream_family_name_in_the_family_name(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    built["name"].setName("Literata Text", 1, *assemble.WINDOWS_ENGLISH)
    problems = qa.name_problems(built, variable_target, reference, FAMILY)
    assert any("upstream project's name" in problem for problem in problems)
    assert any("Literata' appears outside" in problem for problem in problems)


def test_names_catch_a_surviving_trademark_record(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    built["name"].setName(
        "Literata is a trademark of Google Inc.",
        qa.TRADEMARK_NAME_ID,
        *assemble.WINDOWS_ENGLISH,
    )
    problems = qa.name_problems(built, variable_target, reference, FAMILY)
    assert any("must be absent" in problem for problem in problems)


def test_names_catch_a_missing_upstream_notice(
    built: TTFont, variable_target: qa.Target, reference: qa.StyleReference
) -> None:
    built["name"].setName(
        "Copyright 2026 The Asterwell Text Project Authors.",
        qa.COPYRIGHT_NAME_ID,
        *assemble.WINDOWS_ENGLISH,
    )
    problems = qa.name_problems(built, variable_target, reference, FAMILY)
    assert any("required upstream notice" in problem for problem in problems)


def test_a_static_carries_its_own_names_and_no_name_id_25(
    static_path: Path, static_target: qa.Target, reference: qa.StyleReference
) -> None:
    font = TTFont(static_path)
    try:
        assert qa.name_problems(font, static_target, reference, FAMILY) == []
        present = qa.windows_names(font)
        assert present[1] == "Asterwell Text"
        assert present[2] == "Bold"
        assert present[6] == "AsterwellText-Bold"
        # Inherited from the variable font, unchanged.
        assert present[0] == reference.names[0]
        assert 25 not in present
    finally:
        font.close()


def test_a_static_that_kept_name_id_25_fails(
    static_path: Path, static_target: qa.Target, reference: qa.StyleReference
) -> None:
    font = TTFont(static_path)
    try:
        font["name"].setName("AsterwellText", 25, *assemble.WINDOWS_ENGLISH)
        problems = qa.name_problems(font, static_target, reference, FAMILY)
        assert any("must be absent" in problem for problem in problems)
    finally:
        font.close()


def test_a_ribbi_static_must_not_carry_the_typographic_pair(
    static_path: Path, static_target: qa.Target, reference: qa.StyleReference
) -> None:
    font = TTFont(static_path)
    try:
        font["name"].setName("Asterwell Text", 16, *assemble.WINDOWS_ENGLISH)
        problems = qa.name_problems(font, static_target, reference, FAMILY)
        assert any("must be absent" in problem for problem in problems)
    finally:
        font.close()


def test_bits_pass_on_the_built_variable_font(
    built: TTFont, variable_target: qa.Target
) -> None:
    assert qa.bit_problems(built, variable_target, FAMILY) == []


def test_bits_catch_a_wrong_font_revision(
    built: TTFont, variable_target: qa.Target
) -> None:
    built["head"].fontRevision = 2.5
    assert any(
        "fontRevision" in problem
        for problem in qa.bit_problems(built, variable_target, FAMILY)
    )


def test_bits_catch_a_wrong_vendor_id(built: TTFont, variable_target: qa.Target) -> None:
    built["OS/2"].achVendID = "TT  "
    assert any(
        "achVendID" in problem
        for problem in qa.bit_problems(built, variable_target, FAMILY)
    )


def test_a_statics_style_bits_follow_the_instance(
    static_path: Path, static_target: qa.Target
) -> None:
    font = TTFont(static_path)
    try:
        assert qa.bit_problems(font, static_target, FAMILY) == []
    finally:
        font.close()


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda font: setattr(font["OS/2"], "usWeightClass", 400), "usWeightClass"),
        (
            lambda font: setattr(
                font["OS/2"], "fsSelection", instances.FS_REGULAR | instances.FS_USE_TYPO_METRICS
            ),
            "fsSelection style bits",
        ),
        (
            lambda font: setattr(
                font["OS/2"],
                "fsSelection",
                font["OS/2"].fsSelection & ~instances.FS_USE_TYPO_METRICS,
            ),
            "USE_TYPO_METRICS",
        ),
        (lambda font: setattr(font["head"], "macStyle", 0), "macStyle"),
    ],
)
def test_a_static_with_the_wrong_bits_fails(
    static_path: Path, static_target: qa.Target, mutate, expected: str
) -> None:
    font = TTFont(static_path)
    try:
        mutate(font)
        problems = qa.bit_problems(font, static_target, FAMILY)
        assert any(expected in problem for problem in problems), problems
    finally:
        font.close()


# --------------------------------------------------------------------------- #
# 9.5 Stars
# --------------------------------------------------------------------------- #


def test_stars_pass_on_the_built_font(built: TTFont, reference: qa.StyleReference) -> None:
    assert qa.star_problems(built, reference.stars) == []


def test_stars_catch_a_changed_advance(
    built: TTFont, reference: qa.StyleReference
) -> None:
    built["hmtx"][stars.FULL] = (999, built["hmtx"][stars.FULL][1])
    assert any(
        "advance" in problem for problem in qa.star_problems(built, reference.stars)
    )


def test_stars_catch_a_bounding_box_outside_the_tolerance(
    built: TTFont, reference: qa.StyleReference
) -> None:
    glyph = built["glyf"][stars.FULL]
    glyph.yMax += qa.STAR_BBOX_TOLERANCE + 1
    assert any("bbox" in problem for problem in qa.star_problems(built, reference.stars))


def test_a_bounding_box_inside_the_tolerance_is_allowed(
    built: TTFont, reference: qa.StyleReference
) -> None:
    built["glyf"][stars.FULL].yMax += qa.STAR_BBOX_TOLERANCE
    assert qa.star_problems(built, reference.stars) == []


def test_stars_catch_a_composite_built_from_the_wrong_glyph(
    built: TTFont, reference: qa.StyleReference
) -> None:
    built["glyf"][stars.TWO].components[0].glyphName = "bullet"
    assert any(
        "is built from" in problem for problem in qa.star_problems(built, reference.stars)
    )


def test_stars_catch_a_missing_ornament(
    built: TTFont, reference: qa.StyleReference
) -> None:
    built.setGlyphOrder([n for n in built.getGlyphOrder() if n != stars.FULL])
    assert f"{stars.FULL} is missing" in qa.star_problems(built, reference.stars)


def _signatures(paths: dict[str, Path]) -> dict[str, qa.StarSignature]:
    signatures = {}
    for key in ("roman", "italic"):
        font = assemble.open_source_font(paths[f"built-{key}"])
        try:
            signatures[key] = qa.star_signature(
                font, {name: font["hmtx"][name][0] for name in stars.GLYPH_ORDER}
            )
        finally:
            font.close()
    return signatures


def test_the_two_styles_share_one_star_drawing(family_paths: dict[str, Path]) -> None:
    """The outlines are byte-identical; the composites differ only by the
    asterisk advance each style inherits, which the signature normalises away."""
    signatures = _signatures(family_paths)
    assert qa.cross_style_star_problems(signatures, qa.KIND_VARIABLE) == []
    assert signatures["roman"].outlines == signatures["italic"].outlines
    assert signatures["roman"].components == signatures["italic"].components


def test_a_star_that_leans_in_the_italic_fails(family_paths: dict[str, Path]) -> None:
    signatures = _signatures(family_paths)
    outlines = dict(signatures["italic"].outlines)
    coordinates, ends, flags = outlines[stars.FULL]
    outlines[stars.FULL] = (tuple(value + 3 for value in coordinates), ends, flags)
    signatures["italic"] = dataclasses.replace(signatures["italic"], outlines=outlines)
    problems = qa.cross_style_star_problems(signatures, qa.KIND_VARIABLE)
    assert any(stars.FULL in problem for problem in problems)


def test_a_composite_arranged_differently_in_the_italic_fails(
    family_paths: dict[str, Path],
) -> None:
    signatures = _signatures(family_paths)
    components = dict(signatures["italic"].components)
    components[stars.THREE] = tuple(
        (name, x + 20, y) for name, x, y in components[stars.THREE]
    )
    signatures["italic"] = dataclasses.replace(
        signatures["italic"], components=components
    )
    problems = qa.cross_style_star_problems(signatures, qa.KIND_VARIABLE)
    assert any("arranged differently" in problem for problem in problems)


# --------------------------------------------------------------------------- #
# 9.6 Licensing
# --------------------------------------------------------------------------- #

OFL_HEADER = [
    "Copyright 2017 The Upstream Project Authors\n",
    "\n",
    "This Font Software is licensed under the SIL Open Font License, Version 1.1.\n",
    "This license is copied below, and is also available with a FAQ at:\n",
    "http://example.invalid/OFL\n",
    "\n",
    "\n",
]
OFL_BODY = ["-" * 59 + "\n"] + [f"licence line {n}\n" for n in range(85)]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def licensed_root(tmp_path: Path) -> Path:
    """A repository-shaped tree whose licence files are correct."""
    upstream_ofl = "".join(OFL_HEADER + OFL_BODY)
    dejavu_license = "DejaVu Fonts License v1.00\n"
    (tmp_path / "build" / "upstream" / "literata").mkdir(parents=True)
    (tmp_path / "build" / "upstream" / "literata" / "OFL.txt").write_text(
        upstream_ofl, encoding="utf-8"
    )
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "upstream.toml").write_text(
        "[literata]\n"
        'name = "Literata"\nversion = "3.103"\n'
        'url = "https://example.invalid/literata.zip"\n'
        f'sha256 = "{"0" * 64}"\nsize = 1\n'
        "[literata.members]\n"
        f'"OFL.txt" = "{_digest(upstream_ofl)}"\n'
        "\n[dejavu]\n"
        'name = "DejaVu Sans"\nversion = "2.37"\n'
        'url = "https://example.invalid/dejavu.tar.bz2"\n'
        f'sha256 = "{"1" * 64}"\nsize = 1\n'
        "[dejavu.members]\n"
        f'"dejavu-fonts-ttf-2.37/LICENSE" = "{_digest(dejavu_license)}"\n',
        encoding="utf-8",
    )
    (tmp_path / "OFL.txt").write_text(
        "Copyright 2026 The Asterwell Text Project Authors, with Reserved Font "
        'Name "Asterwell".\n'
        "Copyright 2017 The Upstream Project Authors\n"
        "\n"
        "This Font Software is licensed under the SIL Open Font License, Version 1.1.\n"
        "This license is copied below, and is also available with a FAQ at:\n"
        "https://example.invalid/ofl\n"
        "\n"
        "\n" + "".join(OFL_BODY),
        encoding="utf-8",
    )
    (tmp_path / "DEJAVU-LICENSE.txt").write_text(dejavu_license, encoding="utf-8")
    (tmp_path / "FONTLOG.txt").write_text(
        "FONTLOG for Asterwell Text\n1.000 (2026-09-07): initial release\n",
        encoding="utf-8",
    )
    return tmp_path


def test_licensing_passes_on_a_correct_tree(licensed_root: Path) -> None:
    assert qa.license_problems(licensed_root, FAMILY) == []


def test_licensing_catches_an_edited_licence_body(licensed_root: Path) -> None:
    path = licensed_root / "OFL.txt"
    path.write_text(
        path.read_text(encoding="utf-8").replace("licence line 40", "licence line FORTY"),
        encoding="utf-8",
    )
    problems = qa.license_problems(licensed_root, FAMILY)
    assert any("licence body" in problem for problem in problems)


def test_licensing_catches_a_changed_dejavu_notice(licensed_root: Path) -> None:
    (licensed_root / "DEJAVU-LICENSE.txt").write_text("tampered\n", encoding="utf-8")
    problems = qa.license_problems(licensed_root, FAMILY)
    assert any("sha256" in problem for problem in problems)


def test_licensing_catches_a_fontlog_that_never_mentions_the_version(
    licensed_root: Path,
) -> None:
    (licensed_root / "FONTLOG.txt").write_text("FONTLOG\n", encoding="utf-8")
    problems = qa.license_problems(licensed_root, FAMILY)
    assert any("never mentions version" in problem for problem in problems)


# --------------------------------------------------------------------------- #
# 9.7 fontbakery — the wrapper, not the tool
# --------------------------------------------------------------------------- #


def test_the_fontbakery_command_carries_the_exclusions_and_the_reports(
    tmp_path: Path,
) -> None:
    command = qa.fontbakery_command(
        config=tmp_path / "fontbakery.yml",
        paths=[tmp_path / "A.ttf", tmp_path / "B.ttf"],
        report_stem=tmp_path / "fontbakery-variable",
        jobs=None,
    )
    assert qa.FONTBAKERY_PROFILE in command
    # fontbakery 1.1.0 spells it --configuration. §9.7's `--config` works only
    # as an argparse prefix abbreviation, which any future `--config…` option
    # would end — and a dropped configuration means no exclusions at all.
    index = command.index("--configuration")
    assert command[index + 1] == str(tmp_path / "fontbakery.yml")
    for flag, suffix in (("--json", ".json"), ("--ghmarkdown", ".md"), ("--html", ".html")):
        assert command[command.index(flag) + 1].endswith(suffix)
    assert command[-2:] == [str(tmp_path / "A.ttf"), str(tmp_path / "B.ttf")]
    assert "--jobs" not in command


def test_the_fontbakery_command_only_asks_for_workers_when_told_to(
    tmp_path: Path,
) -> None:
    command = qa.fontbakery_command(
        config=tmp_path / "c.yml",
        paths=[tmp_path / "A.ttf"],
        report_stem=tmp_path / "r",
        jobs=4,
    )
    assert command[command.index("--jobs") + 1] == "4"


def test_the_fontbakery_environment_is_seeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unseeded, fontbakery orders its own messages differently on every run
    (``interpolation_issues`` iterates a set), and the reports it writes differ
    byte for byte between two runs over identical fonts. The release ships
    those reports, so the seed is what keeps the zip reproducible."""
    monkeypatch.setenv("ASTERWELL_TEST_MARKER", "kept")
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    env = qa.fontbakery_env()
    assert env["PYTHONHASHSEED"] == "0"
    # Seeded, not replaced: the subprocess still needs PATH, the virtualenv and
    # everything else the parent runs with.
    assert env["ASTERWELL_TEST_MARKER"] == "kept"
    assert os.environ.get("PYTHONHASHSEED") is None, "the parent's env is untouched"


def test_the_shipped_exclusions_are_the_four_documented_ones(repo_root: Path) -> None:
    """The config is data, and a fifth exclusion appearing without a review is
    exactly what §9.7 says must not happen quietly."""
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load((repo_root / qa.FONTBAKERY_CONFIG).read_text(encoding="utf-8"))
    assert config["exclude_checks"] == [
        "base_has_width",
        "case_mapping",
        "fontdata_namecheck",
        "family/win_ascent_and_descent",
    ]


def _check(identifier: str, result: str, filename: str | None, message: str) -> dict:
    return {
        "key": ["<Section: X>", f"<FontBakeryCheck:{identifier}>", []],
        "filename": filename,
        "result": result,
        "logs": [{"status": result, "message": {"code": "c", "message": message}}],
    }


def test_a_clean_fontbakery_report_produces_no_problems() -> None:
    payload = {
        "result": {"(not finished)": 0, "PASS": 3, "WARN": 1},
        "sections": [
            {
                "checks": [
                    _check("unwanted_tables", "PASS", "A.ttf", "ok"),
                    _check("soft_hyphen", "WARN", "A.ttf", "has one"),
                ]
            }
        ],
    }
    counts, problems, warnings = qa.parse_fontbakery(payload)
    assert counts == {"PASS": 3, "WARN": 1}
    assert problems == []
    assert warnings == {"soft_hyphen": 1}


def test_a_fontbakery_fail_becomes_one_line_naming_the_file() -> None:
    payload = {
        "result": {"FAIL": 1, "ERROR": 1},
        "sections": [
            {
                "checks": [
                    _check("opentype/fsselection", "FAIL", "A.ttf", "bad REGULAR bit"),
                    _check("family/single_directory", "ERROR", None, "boom"),
                ]
            }
        ],
    }
    counts, problems, warnings = qa.parse_fontbakery(payload)
    assert counts == {"FAIL": 1, "ERROR": 1}
    assert warnings == {}
    assert problems == [
        "FAIL opentype/fsselection on A.ttf: bad REGULAR bit",
        "ERROR family/single_directory on family: boom",
    ]


def test_the_report_reader_survives_a_report_it_has_not_seen_before() -> None:
    counts, problems, warnings = qa.parse_fontbakery(json.loads("{}"))
    assert (counts, problems, warnings) == ({}, [], {})


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #


def test_a_stray_file_in_a_shipped_directory_is_reported(tmp_path: Path) -> None:
    variable = tmp_path / "fonts" / "variable"
    variable.mkdir(parents=True)
    kept = variable / "AsterwellText[opsz,wght].ttf"
    kept.write_bytes(b"")
    (variable / "AsterwellText-Old.ttf").write_bytes(b"")
    targets = [qa.Target(kept, qa.KIND_VARIABLE, "roman")]
    assert qa.unexpected_files(tmp_path, targets) == ["variable/AsterwellText-Old.ttf"]


def test_a_reason_line_counts_rather_than_spelling_out_a_long_list() -> None:
    assert qa.summarize(["a", "b"], limit=6) == "a, b"
    assert qa.summarize(list("abcdefghij"), limit=3) == "a, b, c … and 7 more"


def test_a_report_is_a_failure_as_soon_as_one_check_fails() -> None:
    report = qa.Report()
    report.add("coverage", "A.ttf")
    assert report.ok
    report.add("coverage", "B.ttf", reasons=["one row is missing"])
    assert not report.ok
    assert [result.subject for result in report.failures] == ["B.ttf"]
    assert "FAIL  coverage" in "\n".join(qa.report_lines(report))


def test_a_webfont_is_read_as_the_font_it_contains(
    tmp_path: Path, family_paths: dict[str, Path]
) -> None:
    from fontTools.ttLib import woff2

    output = tmp_path / "AsterwellText[opsz,wght].woff2"
    woff2.compress(str(family_paths["built-roman"]), str(output))
    target = qa.Target(output, qa.KIND_WEBFONT, "roman")
    font = qa.open_font(qa.font_bytes(target))
    try:
        assert stars.FULL in font.getGlyphOrder()
        assert "fvar" in font
    finally:
        font.close()


def test_qa_is_no_longer_a_cli_stub() -> None:
    from asterwell_build import cli

    assert not getattr(cli.COMMANDS["qa"], "is_stub", False)
    assert not getattr(cli.COMMANDS["specimen"], "is_stub", False)
