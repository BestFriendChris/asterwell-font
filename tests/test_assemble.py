"""Unit tests for the variable-font assembly.

Everything here runs against a **synthetic** three-glyph variable font built
with :class:`fontTools.fontBuilder.FontBuilder` and a hand-written ``gvar``,
plus an equally small stand-in donor. The real 500-glyph import is a build, not
a unit test: it needs the pinned upstream archives, it takes seconds rather than
milliseconds, and what it would prove — that fontTools can scale an outline —
is not what breaks. What breaks is the bookkeeping around a growing glyph order,
so that is what these fonts are shaped to exercise.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.transform import Transform
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.varLib import hvar as hvar_module

from asterwell_build import allowlist, assemble
from asterwell_build.cli import COMMANDS

# --------------------------------------------------------------------------- #
# The synthetic variable font
# --------------------------------------------------------------------------- #

UPEM = 1000
ASCENT = 1177
DESCENT = -308
CAP_HEIGHT = 700
VERTICAL_ADVANCE = 1485


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


def _phantoms(left=(0, 0), right=(0, 0), top=(0, 0), bottom=(0, 0)):
    """The four phantom points every ``gvar`` tuple ends with, in ``gvar`` order."""
    return [left, right, top, bottom]


def build_synthetic_vf(path: Path) -> Path:
    """A two-axis variable font with three glyphs, ``gvar`` deltas on ``A`` and
    every table :mod:`asterwell_build.assemble` touches.

    ``A`` grows 60 units wider and 40 taller at ``wght`` 900; ``B`` does not
    vary at all, which is what a glyph this build *adds* looks like.
    """
    builder = FontBuilder(UPEM, isTTF=True)
    glyph_order = [".notdef", "A", "B"]
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap({0x41: "A", 0x42: "B"})
    glyphs = {
        ".notdef": _box(0, 0, 100, CAP_HEIGHT),
        "A": _box(40, 0, 460, CAP_HEIGHT),
        "B": _box(50, 0, 450, 500),
    }
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(
        {name: (glyphs[name].xMax + 40, glyphs[name].xMin) for name in glyph_order}
    )
    builder.setupHorizontalHeader(ascent=ASCENT, descent=DESCENT, lineGap=0)
    builder.setupVerticalHeader(ascent=500, descent=-500, lineGap=0)
    builder.setupVerticalMetrics(
        {
            name: (VERTICAL_ADVANCE, ASCENT - glyphs[name].yMax)
            for name in glyph_order
        }
    )
    builder.setupNameTable(
        {
            "familyName": "Synthetic",
            "styleName": "Regular",
            "uniqueFontIdentifier": "3.000;TT;Synthetic-Regular",
            "fullName": "Synthetic Regular",
            "psName": "Synthetic-Regular",
            "version": "Version 3.000",
            "trademark": "Synthetic is a trademark of Nobody Inc.",
            "manufacturer": "Nobody",
            "copyright": "Copyright 2017 Nobody.",
            "designer": "Latin by Someone.",
            "description": "A test face.",
            "vendorURL": "https://example.invalid/vendor",
            "designerURL": "https://example.invalid/designer",
            "licenseDescription": "Anything goes.",
            "licenseInfoURL": "https://example.invalid/license",
        }
    )
    builder.setupOS2(
        sTypoAscender=ASCENT,
        sTypoDescender=DESCENT,
        sTypoLineGap=0,
        usWinAscent=ASCENT,
        usWinDescent=-DESCENT,
        sCapHeight=CAP_HEIGHT,
        sxHeight=507,
        achVendID="TT  ",
        fsSelection=0x40 | 0x80,
    )
    builder.setupPost()
    builder.setupFvar(
        axes=[
            ("opsz", 7, 12, 72, "Optical Size"),
            ("wght", 200, 400, 900, "Weight"),
        ],
        instances=[{"location": {"opsz": 12, "wght": 400}, "stylename": "Regular"}],
    )
    npoints = 4
    builder.setupGvar(
        {
            "A": [
                TupleVariation(
                    {"wght": (0.0, 1.0, 1.0)},
                    [(0, 0), (60, 0), (60, 40), (0, 40)]
                    + _phantoms(right=(60, 0), bottom=(0, -40)),
                )
            ],
            "B": [],
            ".notdef": [],
        }
    )
    builder.setupStat(
        [
            {
                "tag": "opsz",
                "name": "Optical Size",
                "values": [
                    {"value": 7, "name": "7pt"},
                    {"value": 12, "name": "12pt"},
                    {"value": 72, "name": "72pt"},
                ],
            },
            {
                "tag": "wght",
                "name": "Weight",
                "values": [
                    {"value": 400, "name": "Regular", "flags": 0x2, "linkedValue": 700},
                    {"value": 900, "name": "Black"},
                ],
            },
        ]
    )
    font = builder.font
    assert npoints == len(glyphs["A"].coordinates)
    font["GDEF"] = _gdef({name: 1 for name in ("A", "B")})
    assemble.regenerate_hvar(font)
    assemble.regenerate_vvar(font)
    path.parent.mkdir(parents=True, exist_ok=True)
    font.save(path)
    return path


def _gdef(class_defs: dict[str, int]):
    table = newTable("GDEF")
    table.table = otTables.GDEF()
    table.table.Version = 0x00010000
    table.table.GlyphClassDef = otTables.GlyphClassDef()
    table.table.GlyphClassDef.classDefs = dict(class_defs)
    table.table.AttachList = None
    table.table.LigCaretList = None
    table.table.MarkAttachClassDef = None
    return table


def build_synthetic_donor(path: Path) -> Path:
    """A stand-in for DejaVu Sans: a different em, an ``H`` whose height sets
    the import scale, one plain glyph and one composite."""
    builder = FontBuilder(2048, isTTF=True)
    glyph_order = [".notdef", "H", "ring", "double"]
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap({0x48: "H", 0x25CB: "ring", 0x25CE: "double"})
    ring = _box(300, 400, 900, 1000)
    composite = TTGlyphPen({"ring": ring})
    composite.addComponent("ring", Transform())
    composite.addComponent("ring", Transform().translate(700, 0))
    double = composite.glyph()
    glyphs = {
        ".notdef": _box(0, 0, 100, 100),
        # 1493 units tall, exactly as DejaVu's own H is: the import scale is
        # measured from it.
        "H": _box(200, 0, 1340, 1493),
        "ring": ring,
        "double": double,
    }
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(
        {
            ".notdef": (600, 0),
            "H": (1540, 200),
            "ring": (1208, 300),
            "double": (1908, 300),
        }
    )
    builder.setupHorizontalHeader(ascent=1901, descent=-483, lineGap=0)
    builder.setupNameTable({"familyName": "Donor", "styleName": "Regular"})
    builder.setupOS2(sCapHeight=0, sxHeight=0)
    builder.setupPost()
    path.parent.mkdir(parents=True, exist_ok=True)
    builder.save(path)
    return path


@pytest.fixture(scope="session")
def synthetic_vf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_synthetic_vf(tmp_path_factory.mktemp("vf") / "Synthetic.ttf")


@pytest.fixture(scope="session")
def donor_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_synthetic_donor(tmp_path_factory.mktemp("donor") / "Donor.ttf")


@pytest.fixture
def vf(synthetic_vf_path: Path) -> Iterator[TTFont]:
    """A freshly opened synthetic VF, force-read the way the build opens one."""
    font = assemble.open_source_font(synthetic_vf_path)
    yield font
    font.close()


@pytest.fixture
def donor(donor_path: Path) -> Iterator[TTFont]:
    font = TTFont(donor_path)
    yield font
    font.close()


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
ROMAN = assemble.STYLES[0]
ITALIC = assemble.STYLES[1]


# --------------------------------------------------------------------------- #
# D11: read every table before the glyph order grows
# --------------------------------------------------------------------------- #


def test_open_source_font_leaves_no_table_undecompiled(synthetic_vf_path: Path) -> None:
    font = assemble.open_source_font(synthetic_vf_path)
    try:
        unread = [tag for tag in font.keys() if tag != "GlyphOrder" and not font.isLoaded(tag)]
    finally:
        font.close()
    assert unread == []


def test_gvar_asserts_when_the_glyph_order_grew_before_it_was_read(
    synthetic_vf_path: Path,
) -> None:
    """The reason D11 exists.

    ``gvar.decompile`` checks that the glyph count it was compiled for still
    matches the glyph order. Appending a glyph first — the natural way to write
    this code — makes that assert fire, and it fires deep inside fontTools with
    no mention of glyph order.
    """
    font = TTFont(synthetic_vf_path)
    try:
        font.setGlyphOrder([*font.getGlyphOrder(), "star.small"])
        with pytest.raises(AssertionError):
            font["gvar"]  # noqa: B018 - decompiling is what raises
    finally:
        font.close()


def test_force_reading_first_makes_the_same_edit_safe(synthetic_vf_path: Path) -> None:
    font = assemble.open_source_font(synthetic_vf_path)
    try:
        assemble.append_glyph(font, "star.small", _box(0, 0, 300, 300), 360, 0)
        assert "star.small" not in font["gvar"].variations
    finally:
        font.close()


def test_open_source_font_does_not_recalculate_the_timestamp(
    synthetic_vf_path: Path,
) -> None:
    font = assemble.open_source_font(synthetic_vf_path)
    try:
        assert font.recalcTimestamp is False
    finally:
        font.close()


# --------------------------------------------------------------------------- #
# Growing the glyph order
# --------------------------------------------------------------------------- #


def test_append_glyph_updates_order_glyf_hmtx_and_maxp(vf: TTFont) -> None:
    before = len(vf.getGlyphOrder())
    glyph = _box(10, 0, 340, 360)
    assemble.append_glyph(vf, "star.small", glyph, 360, 10)

    assert vf.getGlyphOrder()[-1] == "star.small"
    assert vf["glyf"].glyphOrder[-1] == "star.small"
    assert vf["maxp"].numGlyphs == before + 1
    assert vf["hmtx"]["star.small"] == (360, 10)
    assert vf.getGlyphID("star.small") == before


def test_append_glyph_refuses_a_name_the_font_already_uses(vf: TTFont) -> None:
    with pytest.raises(assemble.AssembleError, match="already in the font"):
        assemble.append_glyph(vf, "A", _box(0, 0, 10, 10), 10, 0)


def test_finish_new_glyphs_writes_vmtx_gdef_and_every_cmap_subtable(vf: TTFont) -> None:
    glyph = _box(20, -10, 300, 610)
    assemble.append_glyph(vf, "uni273D", glyph, 400, 20)
    assemble.finish_new_glyphs(vf, ["uni273D"], {"uni273D": 0x273D})

    assert vf["vmtx"]["uni273D"] == (VERTICAL_ADVANCE, ASCENT - 610)
    assert vf["GDEF"].table.GlyphClassDef.classDefs["uni273D"] == 1
    subtables = [s for s in vf["cmap"].tables if s.isUnicode()]
    assert subtables, "the fixture should have at least one Unicode subtable"
    for subtable in subtables:
        assert subtable.cmap[0x273D] == "uni273D"


def test_an_unencoded_glyph_reaches_no_cmap(vf: TTFont) -> None:
    assemble.append_glyph(vf, "star.small", _box(0, 0, 300, 300), 360, 0)
    assemble.finish_new_glyphs(vf, ["star.small"], {})
    for subtable in vf["cmap"].tables:
        assert "star.small" not in subtable.cmap.values()


def test_a_supplementary_code_point_is_refused_by_name(vf: TTFont) -> None:
    assemble.append_glyph(vf, "u1F650", _box(0, 0, 300, 300), 360, 0)
    with pytest.raises(assemble.AssembleError, match="outside the BMP"):
        assemble.finish_new_glyphs(vf, ["u1F650"], {"u1F650": 0x1F650})


def test_a_blank_glyph_gets_the_zero_vertical_advance_literata_gives_one(
    vf: TTFont,
) -> None:
    blank = TTGlyphPen(None).glyph()
    blank.recalcBounds(glyfTable=None)
    assemble.append_glyph(vf, "blank", blank, 240, 0)
    assemble.finish_new_glyphs(vf, ["blank"], {})
    assert vf["vmtx"]["blank"] == (0, ASCENT)


def test_vertical_advance_is_read_from_the_font(vf: TTFont) -> None:
    assert assemble.vertical_advance(vf) == VERTICAL_ADVANCE


def test_vertical_advance_refuses_an_inconsistent_font(vf: TTFont) -> None:
    vf["vmtx"]["B"] = (1400, 10)
    with pytest.raises(assemble.AssembleError, match="one vertical advance"):
        assemble.vertical_advance(vf)


def test_check_side_bearings_catches_a_shifted_origin(vf: TTFont) -> None:
    assemble.append_glyph(vf, "shifted", _box(30, 0, 300, 300), 360, 0)
    with pytest.raises(assemble.AssembleError, match="left side bearing"):
        assemble.check_side_bearings(vf, ["shifted"])


# --------------------------------------------------------------------------- #
# D8: replacing a glyph in place
# --------------------------------------------------------------------------- #


def test_replace_glyph_keeps_the_glyph_id_and_drops_its_gvar_entry(vf: TTFont) -> None:
    gid = vf.getGlyphID("A")
    count = len(vf.getGlyphOrder())
    assert vf["gvar"].variations["A"], "the fixture's A should vary"

    assemble.replace_glyph(vf, "A", _box(76, 0, 814, 719), 889, 76)

    assert vf.getGlyphID("A") == gid
    assert len(vf.getGlyphOrder()) == count
    assert vf["gvar"].variations.get("A", []) == []
    assert vf["hmtx"]["A"] == (889, 76)
    assert vf["cmap"].getBestCmap()[0x41] == "A"


def test_replace_glyph_refuses_a_glyph_the_font_does_not_have(vf: TTFont) -> None:
    with pytest.raises(assemble.AssembleError, match="cannot be replaced"):
        assemble.replace_glyph(vf, "uni2042", _box(0, 0, 10, 10), 10, 0)


def test_a_replaced_glyph_gets_its_top_side_bearing_recomputed(vf: TTFont) -> None:
    assemble.replace_glyph(vf, "A", _box(0, 0, 700, 719), 889, 0)
    assemble.finish_new_glyphs(vf, ["A"], {"A": 0x41})
    assert vf["vmtx"]["A"] == (VERTICAL_ADVANCE, ASCENT - 719)


# --------------------------------------------------------------------------- #
# D6/D7: importing donor outlines
# --------------------------------------------------------------------------- #


def test_import_scale_is_measured_from_the_two_fonts(vf: TTFont, donor: TTFont) -> None:
    assert assemble.import_scale(vf, donor) == pytest.approx(700 / 1493)


def test_import_scale_refuses_a_pin_whose_proportions_moved(
    vf: TTFont, donor: TTFont
) -> None:
    vf["OS/2"].sCapHeight = 690
    with pytest.raises(assemble.AssembleError, match="re-review the pins"):
        assemble.import_scale(vf, donor)


def _dejavu_row(codepoint: int) -> allowlist.Row:
    return allowlist.Row(
        codepoint=codepoint,
        char=chr(codepoint),
        unicode_name="TEST",
        block="Geometric Shapes",
        source=allowlist.SOURCE_DEJAVU,
        glyph_name=allowlist.production_name(codepoint),
        group="Geometric Shapes",
        emoji_presentation="no",
        note="",
    )


def test_imports_are_scaled_decomposed_and_appended_in_row_order(
    vf: TTFont, donor: TTFont
) -> None:
    scale = assemble.import_scale(vf, donor)
    rows = [_dejavu_row(0x25CE), _dejavu_row(0x25CB)]

    names, codepoints = assemble.import_dejavu_glyphs(vf, donor, rows, scale, {})

    assert names == ["uni25CE", "uni25CB"], "file order, not sorted order"
    assert codepoints == {"uni25CE": 0x25CE, "uni25CB": 0x25CB}
    ring = vf["glyf"]["uni25CB"]
    assert ring.numberOfContours == 1
    assert (ring.xMin, ring.yMin, ring.xMax, ring.yMax) == (
        round(300 * scale),
        round(400 * scale),
        round(900 * scale),
        round(1000 * scale),
    )
    assert vf["hmtx"]["uni25CB"] == (round(1208 * scale), ring.xMin)
    # D7: the composite arrived as two plain contours, not as components.
    doubled = vf["glyf"]["uni25CE"]
    assert not doubled.isComposite()
    assert doubled.numberOfContours == 2


def test_an_import_that_collides_with_an_existing_glyph_name_is_refused(
    vf: TTFont, donor: TTFont
) -> None:
    row = _dejavu_row(0x25CB)
    object.__setattr__(row, "glyph_name", "A")
    with pytest.raises(assemble.AssembleError, match="a glyph name the font already uses"):
        assemble.import_dejavu_glyphs(vf, donor, [row], 0.5, {})


def test_an_import_of_a_code_point_the_base_already_maps_is_refused(
    vf: TTFont, donor: TTFont
) -> None:
    for subtable in vf["cmap"].tables:
        if subtable.isUnicode():
            subtable.cmap[0x25CB] = "B"  # the base font covers it after all
    with pytest.raises(assemble.AssembleError, match="Literata already"):
        assemble.import_dejavu_glyphs(vf, donor, [_dejavu_row(0x25CB)], 0.5, {})


def test_an_import_the_donor_does_not_have_is_refused(vf: TTFont, donor: TTFont) -> None:
    with pytest.raises(assemble.AssembleError, match="does not map it"):
        assemble.import_dejavu_glyphs(vf, donor, [_dejavu_row(0x2BFF)], 0.5, {})


def test_match_metrics_fits_the_donor_outline_to_the_reference_box(
    vf: TTFont, donor: TTFont
) -> None:
    recording = DecomposingRecordingPen(donor.getGlyphSet())
    donor.getGlyphSet()["ring"].draw(recording)
    reference = (79, 201, 293, 413)

    glyph = assemble.replay_transformed(
        recording, assemble.match_metrics_transform(recording, reference)
    )

    assert glyph.yMax - glyph.yMin == reference[3] - reference[1]
    centre = ((glyph.xMin + glyph.xMax) / 2, (glyph.yMin + glyph.yMax) / 2)
    assert centre == pytest.approx(
        ((reference[0] + reference[2]) / 2, (reference[1] + reference[3]) / 2), abs=1
    )


def test_the_override_copies_the_reference_advance(vf: TTFont, donor: TTFont) -> None:
    rules = allowlist.Rules(
        custom=(), ranges={}, extras={}, overrides={0x25CB: {"match_metrics_of": "U+0041"}}
    )
    rows = [
        allowlist.Row(
            codepoint=0x41,
            char="A",
            unicode_name="LATIN CAPITAL LETTER A",
            block="Basic Latin",
            source=allowlist.SOURCE_LITERATA,
            glyph_name="A",
            group="required-inventory",
            emoji_presentation="no",
            note="",
        ),
        _dejavu_row(0x25CB),
    ]
    overrides = assemble.resolve_overrides(vf, rules, rows)
    assert overrides[0x25CB].reference_name == "A"

    assemble.import_dejavu_glyphs(vf, donor, rows, 0.5, overrides)

    reference_advance = vf["hmtx"]["A"][0]
    assert vf["hmtx"]["uni25CB"][0] == reference_advance
    glyph = vf["glyf"]["uni25CB"]
    assert glyph.yMax - glyph.yMin == CAP_HEIGHT


def test_an_override_naming_an_absent_reference_is_refused(vf: TTFont) -> None:
    rules = allowlist.Rules(
        custom=(), ranges={}, extras={}, overrides={0x25CB: {"match_metrics_of": "U+2022"}}
    )
    with pytest.raises(assemble.AssembleError, match="not in sources/allowlist.tsv"):
        assemble.resolve_overrides(vf, rules, [])


def test_an_unknown_override_key_is_refused(vf: TTFont) -> None:
    rules = allowlist.Rules(
        custom=(), ranges={}, extras={}, overrides={0x25CB: {"scale_to": "U+2022"}}
    )
    with pytest.raises(assemble.AssembleError, match="unsupported override"):
        assemble.resolve_overrides(vf, rules, [])


# --------------------------------------------------------------------------- #
# D10: HVAR and VVAR
# --------------------------------------------------------------------------- #


def _hvar_advance_deltas(font: TTFont, glyph_name: str) -> list[int]:
    """The advance deltas HVAR records for one glyph, at every region."""
    table = font["HVAR"].table
    index = table.AdvWidthMap.mapping[glyph_name]
    store = table.VarStore
    outer, inner = index >> 16, index & 0xFFFF
    data = store.VarData[outer]
    return list(data.Item[inner])


def test_regenerated_hvar_covers_every_glyph_and_leaves_new_ones_invariant(
    vf: TTFont,
) -> None:
    assemble.append_glyph(vf, "uni2731", _box(0, 0, 400, 700), 805, 0)
    assemble.finish_new_glyphs(vf, ["uni2731"], {"uni2731": 0x2731})
    assemble.regenerate_hvar(vf)

    mapping = vf["HVAR"].table.AdvWidthMap.mapping
    assert set(mapping) == set(vf.getGlyphOrder())
    # The base glyph still varies; the added one cannot, because it has no gvar.
    assert any(delta != 0 for delta in _hvar_advance_deltas(vf, "A"))
    assert all(delta == 0 for delta in _hvar_advance_deltas(vf, "uni2731"))


def test_regenerated_vvar_covers_every_glyph(vf: TTFont) -> None:
    assemble.append_glyph(vf, "uni2731", _box(0, 0, 400, 700), 805, 0)
    assemble.finish_new_glyphs(vf, ["uni2731"], {"uni2731": 0x2731})
    assemble.regenerate_vvar(vf)

    assert set(vf["VVAR"].table.AdvHeightMap.mapping) == set(vf.getGlyphOrder())


def test_upstream_add_vvar_is_still_broken() -> None:
    """Sentinel for the workaround in :func:`assemble.regenerate_vvar`.

    ``fontTools.varLib.hvar.add_VVAR`` builds the ``partial`` capturing
    ``axisTags`` one line before assigning it. When a fontTools upgrade fixes
    that, this test fails — and that failure is the signal to delete the
    workaround and call ``add_VVAR`` directly.
    """
    # An empty mapping is font enough: the call fails before it looks at one.
    with pytest.raises(UnboundLocalError, match="axisTags"):
        hvar_module.add_VVAR({})


# --------------------------------------------------------------------------- #
# Font-wide tables
# --------------------------------------------------------------------------- #


def test_the_optical_size_axis_value_becomes_elidable(vf: TTFont) -> None:
    stat = vf["STAT"].table
    axes = [axis.AxisTag for axis in stat.DesignAxisRecord.Axis]
    values = {
        (axes[v.AxisIndex], v.Value): v for v in stat.AxisValueArray.AxisValue
    }
    assert values[("opsz", 12.0)].Flags == 0

    assert assemble.set_optical_size_elidable(vf) == 1

    assert values[("opsz", 12.0)].Flags & assemble.ELIDABLE_AXIS_VALUE_NAME
    assert values[("opsz", 7.0)].Flags == 0
    assert values[("opsz", 72.0)].Flags == 0
    # The weight axis is untouched: its Regular was already elidable.
    assert values[("wght", 400.0)].Flags == assemble.ELIDABLE_AXIS_VALUE_NAME


def test_a_missing_optical_size_axis_value_is_an_error(vf: TTFont) -> None:
    with pytest.raises(assemble.AssembleError, match="exactly one STAT opsz"):
        assemble.set_optical_size_elidable(vf, value=9.0)


def test_update_os2_recomputes_coverage_and_stamps_the_vendor(vf: TTFont) -> None:
    assemble.append_glyph(vf, "uni2731", _box(0, 0, 400, 700), 805, 0)
    assemble.finish_new_glyphs(vf, ["uni2731"], {"uni2731": 0x2731})
    before = vf["OS/2"].ulUnicodeRange2

    assemble.update_os2(vf, FAMILY)

    assert vf["OS/2"].achVendID == "ASTW"
    assert vf["OS/2"].usLastCharIndex == 0x2731
    assert vf["OS/2"].ulUnicodeRange2 != before, "Dingbats should now be claimed"


def test_the_timestamp_follows_source_date_epoch(
    vf: TTFont, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = vf["head"].modified
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    assemble.stamp_timestamp(vf)
    assert vf["head"].modified == original

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1000000000")
    assemble.stamp_timestamp(vf)
    assert vf["head"].modified != original


def test_a_non_numeric_source_date_epoch_is_an_error(
    vf: TTFont, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "yesterday")
    with pytest.raises(assemble.AssembleError, match="SOURCE_DATE_EPOCH"):
        assemble.stamp_timestamp(vf)


# --------------------------------------------------------------------------- #
# The invariants
# --------------------------------------------------------------------------- #


def test_invariants_of_an_untouched_font_match_themselves(vf: TTFont) -> None:
    assert assemble.invariants(vf).differences(assemble.invariants(vf)) == []


def test_invariants_report_every_value_that_moved(vf: TTFont) -> None:
    before = assemble.invariants(vf)
    vf["hhea"].ascent = 1200
    vf["head"].yMax = 1200
    differences = before.differences(assemble.invariants(vf))
    assert len(differences) == 2
    assert any(line.startswith("ascent:") for line in differences)
    assert any(line.startswith("head_bbox:") for line in differences)


def test_growing_the_glyph_order_is_not_a_drift(vf: TTFont) -> None:
    """The glyph count is *meant* to change; the frozen values are not."""
    before = assemble.invariants(vf)
    assemble.append_glyph(vf, "uni2731", _box(0, 0, 400, 700), 805, 0)
    assemble.finish_new_glyphs(vf, ["uni2731"], {"uni2731": 0x2731})
    assert before.differences(assemble.invariants(vf)) == []


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


def test_name_strings_follow_the_family_metadata(vf: TTFont) -> None:
    strings = assemble.name_strings(vf, FAMILY, ROMAN)
    assert set(strings) == set(assemble.NAME_IDS)
    assert strings[1] == "Asterwell Text"
    assert strings[2] == "Regular"
    assert strings[3] == "1.000;ASTW;AsterwellText-Regular"
    assert strings[4] == "Asterwell Text Regular"
    assert strings[5] == "Version 1.000"
    assert strings[6] == "AsterwellText-Regular"
    assert strings[8] == "The Asterwell Text Project Authors"
    assert strings[11] == FAMILY.repo_url
    assert strings[14] == FAMILY.license_url
    assert strings[25] == "AsterwellText"
    assert 7 not in strings, "the trademark record is deleted, never rewritten"


def test_the_italic_differs_only_where_the_style_does(vf: TTFont) -> None:
    roman = assemble.name_strings(vf, FAMILY, ROMAN)
    italic = assemble.name_strings(vf, FAMILY, ITALIC)
    assert {k for k in roman if roman[k] != italic[k]} == {2, 3, 4, 6, 25}
    assert italic[2] == "Italic"
    assert italic[6] == "AsterwellText-Italic"
    assert italic[25] == "AsterwellTextItalic"


def test_the_copyright_carries_our_notice_and_both_upstream_ones(vf: TTFont) -> None:
    notice = assemble.name_strings(vf, FAMILY, ROMAN)[0]
    assert notice.startswith(
        "Copyright 2026 The Asterwell Text Project Authors "
        "(https://example.invalid/asterwell), with Reserved Font Name \"Asterwell\"."
    )
    assert assemble.LITERATA_NOTICE in notice
    assert assemble.DEJAVU_NOTICE in notice


def test_no_reserved_font_name_clause_when_none_is_reserved() -> None:
    family = assemble.Family(**{**FAMILY.__dict__, "reserved_font_name": ""})
    assert "Reserved Font Name" not in family.copyright


def test_the_designer_credit_keeps_the_upstream_one_and_adds_the_stars(
    vf: TTFont,
) -> None:
    credit = assemble.name_strings(vf, FAMILY, ROMAN)[9]
    # The fixture's designer string ends in a full stop, as Literata's roman
    # does; the suffix must not produce ".; star ornaments…".
    assert credit == "Latin by Someone; star ornaments by The Asterwell Text Project Authors"


def test_the_description_names_the_upstreams_and_points_at_the_repository(
    vf: TTFont,
) -> None:
    description = assemble.name_strings(vf, FAMILY, ROMAN)[10]
    assert "Literata" in description and "DejaVu Sans" in description
    assert description.endswith(FAMILY.repo_url)


def test_the_license_record_mentions_the_bundled_dejavu_license(vf: TTFont) -> None:
    assert "DEJAVU-LICENSE.txt" in assemble.name_strings(vf, FAMILY, ROMAN)[13]


def test_apply_names_drops_mac_records_and_the_trademark(vf: TTFont) -> None:
    assert any(record.platformID == 1 for record in vf["name"].names)

    assemble.apply_names(vf, FAMILY, ROMAN)

    assert [r for r in vf["name"].names if r.platformID == 1] == []
    assert vf["name"].getDebugName(7) is None
    assert vf["name"].getDebugName(1) == "Asterwell Text"
    assert vf["name"].getDebugName(0).startswith("Copyright 2026")
    written = {r.nameID for r in vf["name"].names}
    assert set(assemble.NAME_IDS) <= written


def test_apply_names_leaves_the_style_names_fvar_and_stat_point_at(vf: TTFont) -> None:
    style_ids = {r.nameID for r in vf["name"].names if r.nameID >= 256}
    assert style_ids, "the fixture's STAT should have named its axis values"

    assemble.apply_names(vf, FAMILY, ROMAN)

    assert style_ids <= {r.nameID for r in vf["name"].names}
    for name_id in style_ids:
        assert vf["name"].getDebugName(name_id) is not None


def test_apply_names_leaves_the_upstream_designer_url(vf: TTFont) -> None:
    before = vf["name"].getDebugName(12)
    assemble.apply_names(vf, FAMILY, ROMAN)
    assert vf["name"].getDebugName(12) == before


# --------------------------------------------------------------------------- #
# sources/family.toml
# --------------------------------------------------------------------------- #


#: What `sources/family.toml` carries — identity and notices, and no version:
#: since §15.4 the version is the newest FONTLOG.txt ChangeLog entry, handed to
#: `load_family` by the build.
FAMILY_TOML = """
family      = "Asterwell Text"
ps_family   = "AsterwellText"
vendor_id   = "ASTW"
repo_url    = "https://example.invalid/asterwell"
license_url = "https://example.invalid/ofl"
copyright_holder = "The Asterwell Text Project Authors (https://example.invalid/asterwell)"
copyright_year   = "2026"
reserved_font_name = "Asterwell"
"""


def _family_file(tmp_path: Path, text: str = FAMILY_TOML) -> Path:
    path = tmp_path / "family.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_family_reads_every_field(tmp_path: Path) -> None:
    family = assemble.load_family(_family_file(tmp_path), "1.000")
    assert family == FAMILY
    assert family.font_revision == 1.0
    assert family.holder == "The Asterwell Text Project Authors"
    assert family.vendor_bytes() == "ASTW"


def test_load_family_stamps_the_version_it_is_handed(tmp_path: Path) -> None:
    """The version is the build's, not the file's — 0.000 for a dev build."""
    assert assemble.load_family(_family_file(tmp_path), "0.000").version == "0.000"
    assert assemble.load_family(_family_file(tmp_path), "0.002").font_revision == 0.002


def test_load_family_refuses_a_version_key_in_the_file(tmp_path: Path) -> None:
    """Two sources of truth is the failure §15.4 exists to prevent."""
    path = _family_file(tmp_path, FAMILY_TOML + 'version     = "1.000"\n')
    with pytest.raises(assemble.AssembleError, match="remove it from sources/family.toml"):
        assemble.load_family(path, "1.000")


@pytest.mark.parametrize("version", ["1.0", "0.1", "1.0000", "01.000", "one", ""])
def test_load_family_refuses_a_version_that_is_not_the_one_spelling(
    tmp_path: Path, version: str
) -> None:
    with pytest.raises(assemble.AssembleError, match="must be M.mmm"):
        assemble.load_family(_family_file(tmp_path), version)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ('vendor_id   = "ASTW"', "at most four"),
        ('ps_family   = "AsterwellText"', "cannot contain spaces"),
    ],
)
def test_load_family_rejects_malformed_metadata(
    tmp_path: Path, edit: str, message: str
) -> None:
    replacements = {
        'vendor_id   = "ASTW"': 'vendor_id   = "TOOLONG"',
        'ps_family   = "AsterwellText"': 'ps_family   = "Asterwell Text"',
    }
    path = _family_file(tmp_path, FAMILY_TOML.replace(edit, replacements[edit]))
    with pytest.raises(assemble.AssembleError, match=message):
        assemble.load_family(path, "1.000")


def test_load_family_requires_the_copyright_year(tmp_path: Path) -> None:
    path = _family_file(
        tmp_path,
        "\n".join(
            line for line in FAMILY_TOML.splitlines() if "copyright_year" not in line
        ),
    )
    with pytest.raises(assemble.AssembleError, match="copyright_year"):
        assemble.load_family(path, "1.000")


def test_a_missing_family_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(assemble.AssembleError, match="missing family file"):
        assemble.load_family(tmp_path / "nowhere.toml", "1.000")


def test_the_checked_in_family_file_loads(repo_root: Path) -> None:
    """The real metadata is what the build reads; it has to parse — and it no
    longer carries a version of its own."""
    family = assemble.load_family(assemble.family_path_for(repo_root), "0.000")
    assert family.family == "Asterwell Text"
    assert family.vendor_id == "ASTW"
    assert family.font_revision == pytest.approx(0.0)


def test_output_names_are_the_two_the_spec_promises(repo_root: Path) -> None:
    family = assemble.load_family(assemble.family_path_for(repo_root), "0.000")
    assert [assemble.output_name(family, style) for style in assemble.STYLES] == [
        "AsterwellText[opsz,wght].ttf",
        "AsterwellText-Italic[opsz,wght].ttf",
    ]


def test_build_is_no_longer_a_stub() -> None:
    assert not getattr(COMMANDS["build"], "is_stub", False)
    assert COMMANDS["build"] is assemble.run
