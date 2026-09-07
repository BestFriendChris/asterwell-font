"""Unit tests for the static instances and the WOFF2 web fonts.

Like the assembly tests, everything here runs against a **synthetic** variable
font — six glyphs, two axes, eight named instances, built with
:class:`fontTools.fontBuilder.FontBuilder`. Instancing the real family is a
build, not a unit test: it is sixteen files of 2,297 glyphs and a minute of
wall clock, and what it would prove — that ``fontTools`` can apply a ``gvar``
delta — is not what breaks. What breaks is the identity written on top of the
outlines: which weight gets the BOLD bit, which styles are RIBBI and therefore
do *not* carry name IDs 16/17, and whether the PostScript name follows the
family or the italic variable font's own name ID 25. Those are what this file
pins down, one rule at a time.

The synthetic font is shaped to make each of those distinctions visible: it has
a Regular and a Bold (RIBBI), a SemiBold and a Black (not), and it is built in
both a roman and an italic flavour so the four ``fsSelection`` combinations all
appear.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.TupleVariation import TupleVariation

from asterwell_build import assemble, instances, web

# --------------------------------------------------------------------------- #
# The synthetic variable font
# --------------------------------------------------------------------------- #

UPEM = 1000
ASCENT = 1177
DESCENT = -308

#: The eight weights and the subfamily names Literata gives them, which is what
#: the family's own fvar carries (§14.1).
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
    return [left, right, top, bottom]


def _subfamily(name: str, italic: bool) -> str:
    """How the family names one instance: ``Regular`` alone becomes ``Italic``,
    everything else takes the suffix."""
    if not italic:
        return name
    return "Italic" if name == "Regular" else f"{name} Italic"


def build_synthetic_vf(path: Path, *, italic: bool) -> Path:
    """A two-axis variable font with the family's own eight named instances.

    ``A`` varies with weight; ``uni2731`` does not, standing in for the
    invariant symbols this family imports (D5) — its advance must be the same in
    every static cut from this font.
    """
    builder = FontBuilder(UPEM, isTTF=True)
    glyph_order = [".notdef", "A", "uni2731"]
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap({0x41: "A", 0x2731: "uni2731"})
    glyphs = {
        ".notdef": _box(0, 0, 100, 700),
        "A": _box(40, 0, 460, 700),
        "uni2731": _box(62, 0, 743, 700),
    }
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(
        {".notdef": (600, 0), "A": (500, 40), "uni2731": (805, 62)}
    )
    builder.setupHorizontalHeader(ascent=ASCENT, descent=DESCENT, lineGap=0)
    builder.setupNameTable(
        {
            "familyName": "Asterwell Text",
            "styleName": "Italic" if italic else "Regular",
            "uniqueFontIdentifier": "1.000;ASTW;Synthetic",
            "fullName": "Asterwell Text " + ("Italic" if italic else "Regular"),
            "psName": "AsterwellText-" + ("Italic" if italic else "Regular"),
            "version": "Version 1.000",
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
        # USE_TYPO_METRICS plus the style bit the variable font itself carries;
        # the statics rewrite the style bits and must leave bit 7 alone.
        fsSelection=(0x01 if italic else 0x40) | 0x80,
    )
    builder.setupPost()
    builder.setupFvar(
        axes=[
            ("opsz", 7, 12, 72, "Optical Size"),
            ("wght", 200, 400, 900, "Weight"),
        ],
        instances=[
            {
                "location": {"opsz": instances.STATIC_OPSZ, "wght": weight},
                "stylename": _subfamily(name, italic),
            }
            for weight, name in WEIGHTS
        ],
    )
    builder.setupGvar(
        {
            ".notdef": [],
            # Grows 60 units wider at wght 900: a prose glyph.
            "A": [
                TupleVariation(
                    {"wght": (0.0, 1.0, 1.0)},
                    [(0, 0), (60, 0), (60, 0), (0, 0)] + _phantoms(right=(60, 0)),
                )
            ],
            # No deltas at all: a symbol, steady at every weight (D5).
            "uni2731": [],
        }
    )
    builder.setupStat(
        [
            {
                "tag": "opsz",
                "name": "Optical Size",
                # Elidable, exactly as Step 6 marks it (D19): without this the
                # instancer would name the statics "12pt Bold".
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
            {
                "tag": "ital",
                "name": "Italic",
                "values": [
                    {"value": 1, "name": "Italic"}
                    if italic
                    else {"value": 0, "name": "Roman", "flags": 0x2, "linkedValue": 1}
                ],
            },
        ],
        elidedFallbackName="Italic" if italic else "Regular",
    )
    font = builder.font
    # D18, and the reason it matters here: the assembled variable fonts carry
    # Windows records only, and `getDebugName` prefers a Macintosh record when
    # both exist — a synthetic font that kept them would quietly test the wrong
    # half of the name table.
    font["name"].names = [record for record in font["name"].names if record.platformID != 1]
    assemble.regenerate_hvar(font)
    path.parent.mkdir(parents=True, exist_ok=True)
    font.save(path)
    return path


@pytest.fixture(scope="session")
def roman_vf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("roman")
    return build_synthetic_vf(directory / "AsterwellText[opsz,wght].ttf", italic=False)


@pytest.fixture(scope="session")
def italic_vf_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("italic")
    return build_synthetic_vf(
        directory / "AsterwellText-Italic[opsz,wght].ttf", italic=True
    )


@pytest.fixture
def roman_vf(roman_vf_path: Path) -> Iterator[TTFont]:
    font = assemble.open_source_font(roman_vf_path)
    yield font
    font.close()


@pytest.fixture
def italic_vf(italic_vf_path: Path) -> Iterator[TTFont]:
    font = assemble.open_source_font(italic_vf_path)
    yield font
    font.close()


def _instance(weight: int, name: str, italic: bool) -> instances.Instance:
    return instances.Instance(
        weight=weight, subfamily=_subfamily(name, italic), italic=italic
    )


# --------------------------------------------------------------------------- #
# Reading the named instances
# --------------------------------------------------------------------------- #


def test_named_instances_are_the_fvar_ones_in_order(roman_vf: TTFont) -> None:
    found = instances.named_instances(roman_vf)
    assert [(i.weight, i.subfamily) for i in found] == list(WEIGHTS)
    assert all(not i.italic for i in found)


def test_named_instances_of_the_italic_are_all_italic(italic_vf: TTFont) -> None:
    found = instances.named_instances(italic_vf)
    assert [i.subfamily for i in found] == [
        "ExtraLight Italic",
        "Light Italic",
        "Italic",
        "Medium Italic",
        "SemiBold Italic",
        "Bold Italic",
        "ExtraBold Italic",
        "Black Italic",
    ]
    assert all(i.italic for i in found)


def test_an_instance_at_another_optical_size_is_refused(roman_vf: TTFont) -> None:
    """The statics are the text optical size; a 72pt instance would need its own
    family name, not a silent third file called Bold."""
    roman_vf["fvar"].instances[5].coordinates["opsz"] = 72
    with pytest.raises(instances.InstanceError, match="opsz 72"):
        instances.named_instances(roman_vf)


def test_a_subfamily_that_contradicts_the_italic_bit_is_refused(
    roman_vf: TTFont,
) -> None:
    name_table = roman_vf["name"]
    entry = roman_vf["fvar"].instances[5]
    name_table.setName("Bold Italic", entry.subfamilyNameID, 3, 1, 0x409)
    with pytest.raises(instances.InstanceError, match="fsSelection"):
        instances.named_instances(roman_vf)


def test_a_font_with_no_fvar_is_not_a_variable_font(roman_vf: TTFont) -> None:
    del roman_vf["fvar"]
    with pytest.raises(instances.InstanceError, match="not a variable font"):
        instances.named_instances(roman_vf)


def test_two_instances_with_the_same_name_are_refused(roman_vf: TTFont) -> None:
    """Two instances resolving to one filename would silently ship seven files."""
    entry = roman_vf["fvar"].instances[4]
    roman_vf["name"].setName("Bold", entry.subfamilyNameID, 3, 1, 0x409)
    with pytest.raises(instances.InstanceError, match="Bold"):
        instances.named_instances(roman_vf)


# --------------------------------------------------------------------------- #
# The RIBBI rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("weight", "name", "italic"),
    [(400, "Regular", False), (400, "Regular", True), (700, "Bold", False), (700, "Bold", True)],
)
def test_the_four_ribbi_styles_keep_the_family_name(
    weight: int, name: str, italic: bool
) -> None:
    instance = _instance(weight, name, italic)
    assert instance.ribbi
    strings = instances.name_strings(FAMILY, instance)
    assert strings[1] == "Asterwell Text"
    assert strings[2] == instance.subfamily
    assert 16 not in strings and 17 not in strings, (
        "IDs 1 and 2 already say it; a second, agreeing pair is a chance to disagree"
    )


@pytest.mark.parametrize(
    ("weight", "name"),
    [(200, "ExtraLight"), (300, "Light"), (500, "Medium"), (600, "SemiBold"), (800, "ExtraBold"), (900, "Black")],
)
@pytest.mark.parametrize("italic", [False, True])
def test_every_other_weight_gets_its_own_legacy_family(
    weight: int, name: str, italic: bool
) -> None:
    instance = _instance(weight, name, italic)
    assert not instance.ribbi
    strings = instances.name_strings(FAMILY, instance)
    assert strings[1] == f"Asterwell Text {name}"
    assert strings[2] == ("Italic" if italic else "Regular")
    assert strings[16] == "Asterwell Text"
    assert strings[17] == instance.subfamily
    # The legacy pair and the typographic pair must describe the same font.
    assert f"{strings[1]} {strings[2]}".replace(" Regular", "") == (
        f"{strings[16]} {strings[17]}"
    )


def test_the_full_name_never_spells_out_the_optical_size() -> None:
    """§14.3 / D19: the STAT ``opsz=12`` value is elidable, so name ID 4 is
    "Asterwell Text Bold". This module writes it rather than inheriting it, so
    the name holds even if that flag is ever lost."""
    strings = instances.name_strings(FAMILY, _instance(700, "Bold", False))
    assert strings[4] == "Asterwell Text Bold"
    assert "12pt" not in strings[4]


@pytest.mark.parametrize(
    ("weight", "name", "italic", "token"),
    [
        (400, "Regular", False, "Regular"),
        (400, "Regular", True, "Italic"),
        (600, "SemiBold", False, "SemiBold"),
        (600, "SemiBold", True, "SemiBoldItalic"),
        (900, "Black", True, "BlackItalic"),
    ],
)
def test_the_postscript_name_follows_the_family_not_the_style(
    weight: int, name: str, italic: bool, token: str
) -> None:
    """Name ID 25 of the italic variable font is ``AsterwellTextItalic``, and
    the instancer would build ``AsterwellTextItalic-SemiBoldItalic`` out of it.
    The family's PostScript name is ``AsterwellText`` for both styles."""
    instance = _instance(weight, name, italic)
    strings = instances.name_strings(FAMILY, instance)
    assert instance.token == token
    assert strings[6] == f"AsterwellText-{token}"
    assert strings[3] == f"1.000;ASTW;AsterwellText-{token}"
    assert instances.output_name(FAMILY, instance) == f"AsterwellText-{token}.ttf"


def test_the_version_string_is_the_familys_own() -> None:
    family = dataclasses.replace(FAMILY, version="2.500", vendor_id="XYZ")
    strings = instances.name_strings(family, _instance(700, "Bold", False))
    assert strings[3] == "2.500;XYZ;AsterwellText-Bold"


# --------------------------------------------------------------------------- #
# The style bits
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("weight", "name", "italic", "fs_bits", "mac_bits"),
    [
        # The four RIBBI combinations …
        (400, "Regular", False, instances.FS_REGULAR, 0),
        (400, "Regular", True, instances.FS_ITALIC, instances.MAC_ITALIC),
        (700, "Bold", False, instances.FS_BOLD, instances.MAC_BOLD),
        (700, "Bold", True, instances.FS_BOLD | instances.FS_ITALIC, instances.MAC_BOLD | instances.MAC_ITALIC),
        # … and the two shapes every other weight takes.
        (600, "SemiBold", False, instances.FS_REGULAR, 0),
        (600, "SemiBold", True, instances.FS_ITALIC, instances.MAC_ITALIC),
        (200, "ExtraLight", False, instances.FS_REGULAR, 0),
        (900, "Black", True, instances.FS_ITALIC, instances.MAC_ITALIC),
    ],
)
def test_the_style_bits_follow_the_weight_and_the_slope(
    weight: int, name: str, italic: bool, fs_bits: int, mac_bits: int
) -> None:
    instance = _instance(weight, name, italic)
    assert instance.fs_selection_bits == fs_bits
    assert instance.mac_style_bits == mac_bits


def test_every_upright_that_is_not_bold_is_regular() -> None:
    """Bit 6 is "not italic, not bold, not oblique" — the OpenType definition,
    which says nothing about weight. Black upright is a Regular; the italic at
    400 is not, because it is the family's Italic. Only Bold and the italics
    give the bit up."""
    upright = [_instance(w, n, False) for w, n in WEIGHTS]
    assert [bool(i.fs_selection_bits & instances.FS_REGULAR) for i in upright] == [
        True, True, True, True, True, False, True, True
    ]
    assert all(
        not _instance(w, n, True).fs_selection_bits & instances.FS_REGULAR
        for w, n in WEIGHTS
    )


def test_the_bold_bit_belongs_to_the_bold_slot_not_to_a_heavy_weight() -> None:
    """ExtraBold and Black are heavier than Bold and still must not claim the
    bit: it is what a host's bold button reaches for, and only one weight per
    family can answer."""
    assert not _instance(800, "ExtraBold", False).fs_selection_bits & instances.FS_BOLD
    assert not _instance(900, "Black", False).mac_style_bits & instances.MAC_BOLD
    assert _instance(700, "Bold", False).fs_selection_bits & instances.FS_BOLD


def test_applying_the_bits_leaves_every_other_bit_alone(roman_vf: TTFont) -> None:
    """USE_TYPO_METRICS decides whose vertical metrics a host believes; losing
    it would quietly reflow every document."""
    roman_vf["OS/2"].fsSelection |= instances.FS_USE_TYPO_METRICS | 0x100  # WWS
    roman_vf["head"].macStyle |= 0x40  # "extended"
    instances.apply_style_bits(roman_vf, _instance(700, "Bold", True))
    assert roman_vf["OS/2"].usWeightClass == 700
    assert roman_vf["OS/2"].fsSelection & instances.FS_USE_TYPO_METRICS
    assert roman_vf["OS/2"].fsSelection & 0x100
    assert not roman_vf["OS/2"].fsSelection & instances.FS_REGULAR
    assert roman_vf["OS/2"].fsSelection & instances.FS_BOLD
    assert roman_vf["OS/2"].fsSelection & instances.FS_ITALIC
    assert roman_vf["head"].macStyle == 0x40 | instances.MAC_BOLD | instances.MAC_ITALIC


def test_applying_the_bits_clears_the_ones_that_no_longer_apply(roman_vf: TTFont) -> None:
    roman_vf["OS/2"].fsSelection |= instances.FS_BOLD | instances.FS_ITALIC
    roman_vf["head"].macStyle |= instances.MAC_BOLD | instances.MAC_ITALIC
    instances.apply_style_bits(roman_vf, _instance(700, "Bold", False))
    assert not roman_vf["OS/2"].fsSelection & (instances.FS_REGULAR | instances.FS_ITALIC)
    assert roman_vf["head"].macStyle == instances.MAC_BOLD


def test_apply_names_removes_the_typographic_pair_for_a_ribbi_style(
    roman_vf: TTFont,
) -> None:
    name_table = roman_vf["name"]
    name_table.setName("Asterwell Text", 16, 3, 1, 0x409)
    name_table.setName("SemiBold", 17, 3, 1, 0x409)
    instances.apply_names(roman_vf, FAMILY, _instance(700, "Bold", False))
    assert name_table.getDebugName(16) is None
    assert name_table.getDebugName(17) is None
    assert name_table.getDebugName(2) == "Bold"


@pytest.mark.parametrize("italic", [False, True])
def test_apply_names_drops_the_variations_postscript_prefix(
    roman_vf: TTFont, italic: bool
) -> None:
    """Name ID 25 describes a variable font's PostScript naming; a static has no
    variations to prefix, and Literata's own statics carry no such record."""
    roman_vf["name"].setName("AsterwellTextItalic", 25, 3, 1, 0x409)
    instances.apply_names(roman_vf, FAMILY, _instance(600, "SemiBold", italic))
    assert roman_vf["name"].getDebugName(25) is None


def test_apply_names_writes_the_typographic_pair_for_the_others(
    roman_vf: TTFont,
) -> None:
    strings = instances.apply_names(roman_vf, FAMILY, _instance(600, "SemiBold", True))
    name_table = roman_vf["name"]
    assert strings == {nid: name_table.getDebugName(nid) for nid in strings}
    assert name_table.getDebugName(17) == "SemiBold Italic"


# --------------------------------------------------------------------------- #
# The metrics freeze
# --------------------------------------------------------------------------- #


def test_a_font_compared_with_itself_has_not_drifted(roman_vf: TTFont) -> None:
    frozen = assemble.invariants(roman_vf)
    assert instances.frozen_differences(frozen, frozen) == []


def test_a_moved_vertical_metric_is_a_drift(roman_vf: TTFont) -> None:
    frozen = assemble.invariants(roman_vf)
    roman_vf["OS/2"].sTypoDescender = -300
    differences = instances.frozen_differences(frozen, assemble.invariants(roman_vf))
    assert len(differences) == 1
    assert differences[0].startswith("typo_descender:")


def test_the_tables_instancing_removes_are_not_a_drift(roman_vf: TTFont) -> None:
    """``fvar``, ``avar`` and ``MVAR`` are gone from a static by definition; the
    same reader measures both, so their absence must not read as a change."""
    frozen = assemble.invariants(roman_vf)
    del roman_vf["fvar"]
    assert instances.frozen_differences(frozen, assemble.invariants(roman_vf)) == []


def test_the_style_bits_are_not_a_drift_but_the_other_bits_are(
    roman_vf: TTFont,
) -> None:
    frozen = assemble.invariants(roman_vf)
    instances.apply_style_bits(roman_vf, _instance(700, "Bold", True))
    assert instances.frozen_differences(frozen, assemble.invariants(roman_vf)) == []

    roman_vf["OS/2"].fsSelection &= ~instances.FS_USE_TYPO_METRICS
    differences = instances.frozen_differences(frozen, assemble.invariants(roman_vf))
    assert len(differences) == 1
    assert "outside the style bits" in differences[0]


def test_a_bigger_bounding_box_is_reported_not_frozen(roman_vf: TTFont) -> None:
    """Literata's own design space moves the box — Bold really is wider than
    Regular — so the box is recorded per static rather than asserted."""
    frozen = assemble.invariants(roman_vf)
    roman_vf["head"].xMax += 200
    roman_vf["hhea"].advanceWidthMax += 200
    assert instances.frozen_differences(frozen, assemble.invariants(roman_vf)) == []


# --------------------------------------------------------------------------- #
# End to end on the synthetic font
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def roman_statics(roman_vf_path: Path, tmp_path_factory: pytest.TempPathFactory):
    directory = tmp_path_factory.mktemp("statics")
    return instances.build_style_statics(
        roman_vf_path, family=FAMILY, output_dir=directory, log=lambda _message: None
    )


def test_every_named_instance_becomes_a_file(roman_statics) -> None:
    assert [report.output.name for report in roman_statics] == [
        f"AsterwellText-{name}.ttf" for _weight, name in WEIGHTS
    ]
    assert all(report.output.is_file() for report in roman_statics)
    assert all(report.style == "roman" for report in roman_statics)


def test_a_written_static_carries_the_names_and_bits_it_was_given(roman_statics) -> None:
    bold = next(r for r in roman_statics if r.instance.weight == 700)
    font = TTFont(bold.output)
    try:
        name_table = font["name"]
        assert name_table.getDebugName(4) == "Asterwell Text Bold"
        assert name_table.getDebugName(6) == "AsterwellText-Bold"
        assert font["OS/2"].usWeightClass == 700
        assert font["OS/2"].fsSelection & instances.FS_BOLD
        assert font["OS/2"].fsSelection & instances.FS_USE_TYPO_METRICS
        assert font["head"].macStyle == instances.MAC_BOLD
        assert "fvar" not in font and "gvar" not in font
        assert "STAT" in font, "a static still has to say where it sits in the family"
    finally:
        font.close()


def test_a_prose_glyph_varies_across_the_statics_and_a_symbol_does_not(
    roman_statics,
) -> None:
    """D5, seen from the output side: instancing must move ``A`` and must not
    move the invariant symbol."""
    advances = {}
    for report in roman_statics:
        font = TTFont(report.output)
        try:
            advances[report.instance.weight] = (
                font["hmtx"]["A"][0],
                font["hmtx"]["uni2731"][0],
            )
        finally:
            font.close()
    assert advances[200][0] != advances[900][0]
    assert {symbol for _prose, symbol in advances.values()} == {805}


def test_a_static_is_refused_when_the_vertical_metrics_move(
    roman_vf_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The freeze is enforced on the written file, so break the reader and the
    build has to notice."""
    real = assemble.invariants

    def shorter(font: TTFont):
        return dataclasses.replace(real(font), ascent=1000)

    variable = assemble.open_source_font(roman_vf_path)
    frozen = real(variable)
    variable.close()
    monkeypatch.setattr(assemble, "invariants", shorter)
    with pytest.raises(instances.InstanceError, match="ascent"):
        instances.build_static(
            roman_vf_path,
            _instance(700, "Bold", False),
            family=FAMILY,
            frozen=frozen,
            glyph_count=3,
            output=tmp_path / "Bold.ttf",
        )


def test_a_missing_variable_font_names_the_command_that_makes_it(
    tmp_path: Path,
) -> None:
    with pytest.raises(instances.InstanceError, match="mise run build"):
        instances.build_style_statics(
            tmp_path / "absent.ttf", family=FAMILY, output_dir=tmp_path
        )


# --------------------------------------------------------------------------- #
# WOFF2
# --------------------------------------------------------------------------- #


def test_the_web_font_is_the_variable_font_compressed(
    roman_vf_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "web" / web.output_name(roman_vf_path)
    report = web.compress_font(roman_vf_path, output)
    assert output.name == "AsterwellText[opsz,wght].woff2"
    assert report.output_size < report.source_size
    assert 0 < report.ratio < 1

    reopened = TTFont(output)
    try:
        assert reopened.flavor == "woff2"
        # The axes are the point: a subsetted or statically-instanced web font
        # would lose them, and §7.6 forbids both.
        assert [axis.axisTag for axis in reopened["fvar"].axes] == ["opsz", "wght"]
        assert len(reopened["fvar"].instances) == len(WEIGHTS)
        assert "gvar" in reopened
        assert reopened.getGlyphOrder() == [".notdef", "A", "uni2731"]
    finally:
        reopened.close()


def test_the_web_font_round_trips_to_the_same_tables(
    roman_vf_path: Path, tmp_path: Path
) -> None:
    """WOFF2 transforms ``glyf`` and ``loca`` and must reconstruct them exactly."""
    output = tmp_path / "AsterwellText[opsz,wght].woff2"
    web.compress_font(roman_vf_path, output)
    source, reopened = TTFont(roman_vf_path), TTFont(output)
    try:
        assert set(source.keys()) == set(reopened.keys())
        for name in reopened.getGlyphOrder():
            assert source["glyf"][name].getCoordinates(source["glyf"])[0] == (
                reopened["glyf"][name].getCoordinates(reopened["glyf"])[0]
            )
            assert source["hmtx"][name] == reopened["hmtx"][name]
    finally:
        source.close()
        reopened.close()


def test_a_missing_source_names_the_command_that_makes_it(tmp_path: Path) -> None:
    with pytest.raises(web.WebError, match="mise run build"):
        web.compress_font(tmp_path / "absent.ttf", tmp_path / "absent.woff2")


def test_the_web_stage_is_one_file_per_variable_font(
    roman_vf_path: Path, italic_vf_path: Path, tmp_path: Path
) -> None:
    reports = web.build_webfonts(
        [
            assemble.StyleReport("roman", roman_vf_path, roman_vf_path, 3, 3),
            assemble.StyleReport("italic", italic_vf_path, italic_vf_path, 3, 3),
        ],
        root=tmp_path,
        log=lambda _message: None,
    )
    assert [report.output.name for report in reports] == [
        "AsterwellText[opsz,wght].woff2",
        "AsterwellText-Italic[opsz,wght].woff2",
    ]
    assert all(report.output.parent == tmp_path / "fonts" / "webfonts" for report in reports)


# --------------------------------------------------------------------------- #
# BUILD-INFO.json
# --------------------------------------------------------------------------- #


@pytest.fixture
def build_info_root(tmp_path: Path, repo_root: Path) -> Path:
    """A throwaway root carrying just what the manifest reads: the checked-in
    allowlist and a ``.verified`` manifest of the shape ``fetch`` writes."""
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "allowlist.tsv").write_bytes(
        (repo_root / "sources" / "allowlist.tsv").read_bytes()
    )
    verified = tmp_path / "build" / "upstream"
    verified.mkdir(parents=True)
    (verified / ".verified").write_text(
        json.dumps(
            {
                "schema": 1,
                "archives": {
                    "literata": {
                        "dir": "literata",
                        "members": {"fonts/variable/Literata[opsz,wght].ttf": "b4" * 32},
                    },
                    "dejavu": {"dir": "dejavu", "members": {"ttf/DejaVuSans.ttf": "7d" * 32}},
                },
            }
        )
    )
    return tmp_path


def test_the_manifest_records_the_run(build_info_root: Path, tmp_path: Path) -> None:
    output = assemble.fonts_dir_for(build_info_root) / "variable" / "One.ttf"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"not really a font")

    info = assemble.build_info(build_info_root, FAMILY, [output])
    assert info["version"] == "1.000"
    assert set(info["tools"]) == {"python", "fonttools", "uharfbuzz", "fontbakery"}
    assert info["inputs"] == {
        "dejavu/ttf/DejaVuSans.ttf": "7d" * 32,
        "literata/fonts/variable/Literata[opsz,wght].ttf": "b4" * 32,
    }
    assert len(info["allowlist_sha256"]) == 64
    # Output paths are relative to `fonts/`, which is the root of the release
    # zip the manifest ships in.
    assert list(info["outputs"]) == ["variable/One.ttf"]


def test_the_manifest_reads_source_date_epoch(
    build_info_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1788767836")
    assert assemble.build_info(build_info_root, FAMILY, [])["source_date_epoch"] == 1788767836
    monkeypatch.delenv("SOURCE_DATE_EPOCH")
    assert assemble.build_info(build_info_root, FAMILY, [])["source_date_epoch"] is None


def test_a_non_numeric_source_date_epoch_is_still_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "yesterday")
    with pytest.raises(assemble.AssembleError, match="SOURCE_DATE_EPOCH"):
        assemble.source_date_epoch()


def test_the_manifest_is_written_where_the_release_zip_expects_it(
    build_info_root: Path,
) -> None:
    path = assemble.write_build_info(build_info_root, FAMILY, [], log=lambda _m: None)
    assert path == build_info_root / "fonts" / "BUILD-INFO.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload) == [
        "version",
        "source_date_epoch",
        "tools",
        "inputs",
        "allowlist_sha256",
        "outputs",
    ]
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_a_missing_fetch_is_named_as_such(tmp_path: Path) -> None:
    with pytest.raises(assemble.upstream.UpstreamError, match="mise run fetch"):
        assemble.input_hashes(tmp_path)
