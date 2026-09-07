"""Expanding the allowlist rules into the checked-in code-point manifest.

Nothing here opens a pinned font. Each test builds the two donors it needs with
:class:`~fontTools.fontBuilder.FontBuilder` — a handful of glyphs and a cmap is
all the generator reads — and lays them out in a throwaway directory shaped like
the repository, so the real ``build/upstream/.verified`` lookup, the real cmap
reading and the real file writing are all exercised against bytes the test
controls. The two exceptions are marked: they read the checked-in
``sources/allowlist.tsv`` and ``qa/vera-1.10-codepoints.txt``, which are
committed artifacts, and one of them additionally skips itself unless the
upstream archives happen to be on disk.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from asterwell_build import allowlist
from asterwell_build.allowlist import AllowlistError
from asterwell_build.cli import COMMANDS, build_parser

INVENTORY = tuple(ord(char) for char in allowlist.REQUIRED_INVENTORY)

#: The four original outlines, as the real rules name them.
CUSTOM = (0x273D, 0x204E, 0x2051, 0x2042)

#: Stand-in for Literata: the inventory characters a real Literata draws, under
#: its own glyph names, plus ■ — which sits inside the test range below, so
#: every rule set here exercises "preserved, not imported".
LITERATA: Mapping[int, str] = {
    0x00D7: "multiply",
    0x2022: "bullet",
    0x2190: "arrowleft",
    0x2191: "arrowup",
    0x2192: "arrowright",
    0x2193: "arrowdown",
    0x2205: "emptyset",
    0x2212: "minus",
    0x2248: "approxequal",
    0x2264: "lessequal",
    0x2265: "greaterequal",
    0x25A0: "filledbox",
    0x25C6: "uni25C6",
    0x25C7: "uni25C7",
}

#: Stand-in for DejaVu Sans: everything the inventory needs, the four code
#: points of the test range, and the one selected marker.
DEJAVU: frozenset[int] = frozenset(INVENTORY) | {0x25A0, 0x25A1, 0x25A2, 0x25A3, 0x2055}

#: Rules exercising all four tables at a size a test can count.
RULES = """
[custom]
codepoints = ["U+273D", "U+204E", "U+2051", "U+2042"]

[ranges]
"Geometric Shapes" = ["U+25A0", "U+25A3"]

[extras]
markers = ["U+2055"]

[overrides]
"U+25E6" = { match_metrics_of = "U+2022" }
"""

#: What :data:`RULES` resolves to against :data:`LITERATA` / :data:`DEJAVU`:
#: 4 custom, 13 inventory + 1 range preserved from Literata, 26 inventory + 3
#: range imported from DejaVu.
EXPECTED_TOTAL = 47


# --------------------------------------------------------------------------- #
# Synthetic repository
# --------------------------------------------------------------------------- #


def make_font(path: Path, cmap: Mapping[int, str]) -> None:
    """A minimal but real TTF mapping ``cmap``; one square glyph does for all."""
    order = [".notdef", *dict.fromkeys(cmap.values())]
    pen = TTGlyphPen(None)
    pen.moveTo((50, 0))
    pen.lineTo((50, 500))
    pen.lineTo((550, 500))
    pen.lineTo((550, 0))
    pen.closePath()
    square = pen.glyph()

    builder = FontBuilder(unitsPerEm=1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(dict(cmap))
    builder.setupGlyf({name: square for name in order})
    builder.setupHorizontalMetrics({name: (600, 50) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Fixture", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    path.parent.mkdir(parents=True, exist_ok=True)
    builder.save(path)


@dataclass
class Repo:
    """A throwaway checkout: rules, donor fonts, upstream manifest, fixture."""

    root: Path

    @property
    def tsv(self) -> Path:
        return allowlist.tsv_path_for(self.root)

    @property
    def rules(self) -> Path:
        return allowlist.rules_path_for(self.root)

    def generate(self, *, check: bool = False, quiet: bool = True) -> int:
        return allowlist.generate(self.root, check=check, quiet=quiet)

    def rows(self) -> list[allowlist.Row]:
        return allowlist.read_tsv(self.tsv)

    def by_codepoint(self) -> dict[int, allowlist.Row]:
        return {row.codepoint: row for row in self.rows()}


def build_repo(
    root: Path,
    *,
    rules: str = RULES,
    literata: Mapping[int, str] | None = None,
    italic: Mapping[int, str] | None = None,
    dejavu: Iterable[int] | None = None,
    vera: Iterable[int] = (0x0041, 0x00D7, 0x2022),
) -> Repo:
    """Lay out ``root`` the way the generator expects to find a checkout."""
    literata = LITERATA if literata is None else literata
    italic = literata if italic is None else italic
    dejavu = DEJAVU if dejavu is None else dejavu

    allowlist.rules_path_for(root).parent.mkdir(parents=True, exist_ok=True)
    allowlist.rules_path_for(root).write_text(rules, encoding="utf-8")

    fixture = allowlist.vera_path_for(root)
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(
        "# synthetic Bitstream Vera fixture\n"
        + "".join(f"U+{cp:04X}\n" for cp in sorted(vera)),
        encoding="utf-8",
    )

    upstream_dir = root / "build" / "upstream"
    roman_member = "fonts/variable/Literata[opsz,wght].ttf"
    italic_member = "fonts/variable/Literata-Italic[opsz,wght].ttf"
    dejavu_member = "dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf"
    make_font(upstream_dir / "literata" / roman_member, literata)
    make_font(upstream_dir / "literata" / italic_member, italic)
    make_font(
        upstream_dir / "dejavu" / dejavu_member,
        {cp: allowlist.production_name(cp) for cp in sorted(dejavu)},
    )

    manifest = {
        "schema": 1,
        "archives": {
            "literata": {
                "name": "Literata",
                "version": "3.103",
                "url": "https://example.invalid/literata.zip",
                "dir": "literata",
                "members": {roman_member: "00" * 32, italic_member: "11" * 32},
            },
            "dejavu": {
                "name": "DejaVu Sans",
                "version": "2.37",
                "url": "https://example.invalid/dejavu.tar.bz2",
                "dir": "dejavu",
                "members": {dejavu_member: "22" * 32},
            },
        },
    }
    (upstream_dir / ".verified").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return Repo(root)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return build_repo(tmp_path)


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def test_the_checked_in_rules_parse_into_the_documented_selection(repo_root: Path) -> None:
    rules = allowlist.load_rules(repo_root / "sources" / "allowlist.toml")

    assert rules.custom == CUSTOM
    assert rules.ranges == {
        "Dingbats": (0x2700, 0x27BF),
        "Miscellaneous Symbols": (0x2600, 0x26FF),
        "Geometric Shapes": (0x25A0, 0x25FF),
        "Miscellaneous Symbols and Arrows": (0x2B00, 0x2BFF),
    }
    assert list(rules.extras) == ["ui-required", "markers", "ui-reserve"]
    assert sum(len(group) for group in rules.extras.values()) == 24
    assert rules.overrides == {0x25E6: {"match_metrics_of": "U+2022"}}


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ('[custom]\ncodepoints = ["2042"]\n', re.escape('does not start with "U+"')),
        ('[custom]\ncodepoints = ["U+ZZZZ"]\n', "not a hex code point"),
        ('[custom]\ncodepoints = ["U+2042", "U+2042"]\n', "twice"),
        ('[ranges]\n"Bad" = ["U+2700"]\n', r"\[first, last\]"),
        ('[ranges]\n"Bad" = ["U+27BF", "U+2700"]\n', "is above"),
        ('[extras]\nmarkers = "U+2055"\n', "expected a list"),
        ('[overrides]\n"U+25E6" = { match_advance_of = "U+2022" }\n', "unknown adjustment"),
        ('[overrides]\n"U+25E6" = {}\n', "non-empty table"),
        ("[nonsense]\nx = 1\n", "unknown table"),
    ],
)
def test_a_malformed_rule_file_is_rejected(tmp_path: Path, rules: str, message: str) -> None:
    path = tmp_path / "allowlist.toml"
    path.write_text(rules, encoding="utf-8")
    with pytest.raises(AllowlistError, match=message):
        allowlist.load_rules(path)


def test_a_missing_rule_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(AllowlistError, match="missing rules file"):
        allowlist.load_rules(tmp_path / "nope.toml")


# --------------------------------------------------------------------------- #
# Precedence and expansion
# --------------------------------------------------------------------------- #


def test_precedence_is_custom_then_literata_then_dejavu(repo: Repo) -> None:
    """All three donors cover ✽ ◆ ✱; each resolves to a different source."""
    assert repo.generate() == 0
    rows = repo.by_codepoint()

    # ✽ is in DejaVu (and inside a real Dingbats range) but is drawn here.
    assert rows[0x273D].source == "custom"
    assert rows[0x273D].glyph_name == "uni273D"
    # ◆ is in both Literata and DejaVu: preserved, under Literata's own name.
    assert rows[0x25C6].source == "literata"
    assert rows[0x25C6].glyph_name == "uni25C6"
    assert rows[0x2022].glyph_name == "bullet"
    # ✱ is only in DejaVu: imported, under a production name.
    assert rows[0x2731].source == "dejavu"
    assert rows[0x2731].glyph_name == "uni2731"


def test_a_range_lists_what_a_font_covers_and_nothing_else(repo: Repo) -> None:
    assert repo.generate() == 0
    rows = repo.by_codepoint()
    swept = {0x25A0, 0x25A1, 0x25A2, 0x25A3}

    in_block = {cp for cp in rows if 0x25A0 <= cp <= 0x25FF}
    # The range itself reaches only U+25A3; the rest of the block is here
    # because the required inventory asks for it.
    assert in_block == swept | {cp for cp in INVENTORY if 0x25A0 <= cp <= 0x25FF}
    assert {cp for cp in in_block if rows[cp].group == "Geometric Shapes"} == swept
    # U+25A4..U+25B7 are inside the range's block but covered by no donor here,
    # so the manifest simply does not mention them.
    assert not [cp for cp in rows if 0x25A4 <= cp <= 0x25B7]


def test_a_range_codepoint_literata_already_draws_is_preserved(repo: Repo) -> None:
    assert repo.generate() == 0
    row = repo.by_codepoint()[0x25A0]
    assert (row.source, row.glyph_name, row.group) == (
        "literata",
        "filledbox",
        "Geometric Shapes",
    )


def test_rows_are_sorted_by_code_point_and_counted_by_source(repo: Repo) -> None:
    assert repo.generate() == 0
    rows = repo.rows()

    assert [row.codepoint for row in rows] == sorted(row.codepoint for row in rows)
    assert len(rows) == EXPECTED_TOTAL
    counts = {source: sum(row.source == source for row in rows) for source in allowlist.SOURCES}
    assert counts == {"custom": 4, "literata": 14, "dejavu": 29}


def test_every_required_inventory_character_gets_a_row(repo: Repo) -> None:
    assert repo.generate() == 0
    rows = repo.by_codepoint()
    assert set(INVENTORY) <= set(rows)


def test_the_required_inventory_is_the_43_characters_the_design_names() -> None:
    assert len(allowlist.REQUIRED_INVENTORY) == 43
    assert len(set(allowlist.REQUIRED_INVENTORY)) == 43
    for char in "⁎⁑⁕✽•":
        assert char in allowlist.REQUIRED_INVENTORY


# --------------------------------------------------------------------------- #
# Hard errors
# --------------------------------------------------------------------------- #


def test_an_extra_missing_from_dejavu_is_a_hard_error(tmp_path: Path) -> None:
    """An extra is a hand-picked import; silently dropping it would be a lie."""
    repo = build_repo(
        tmp_path,
        rules=RULES.replace('markers = ["U+2055"]', 'markers = ["U+2055", "U+2E19"]'),
    )
    with pytest.raises(AllowlistError, match=r"\[extras\] markers: U\+2E19"):
        repo.generate()


def test_an_extra_missing_from_dejavu_is_an_error_even_if_literata_has_it(
    tmp_path: Path,
) -> None:
    """Extras name DejaVu imports; a Literata glyph is not what was asked for."""
    repo = build_repo(
        tmp_path,
        rules=RULES.replace('markers = ["U+2055"]', 'markers = ["U+2055", "U+2E19"]'),
        literata={**LITERATA, 0x2E19: "palmbranch"},
    )
    with pytest.raises(AllowlistError, match=r"U\+2E19"):
        repo.generate()


def test_a_required_character_missing_from_both_fonts_is_a_hard_error(
    tmp_path: Path,
) -> None:
    repo = build_repo(tmp_path, dejavu=DEJAVU - {0x2318})  # ⌘
    with pytest.raises(AllowlistError, match=r"required inventory: U\+2318"):
        repo.generate()


def test_a_custom_character_needs_no_donor_font(tmp_path: Path) -> None:
    """The stars are drawn, not imported: neither donor has to cover them."""
    repo = build_repo(tmp_path, dejavu=DEJAVU - set(CUSTOM))
    assert repo.generate() == 0
    assert {repo.by_codepoint()[cp].source for cp in CUSTOM} == {"custom"}


def test_the_two_literata_styles_must_agree_about_a_selected_glyph(
    tmp_path: Path,
) -> None:
    repo = build_repo(tmp_path, italic={**LITERATA, 0x2022: "bullet.alt"})
    with pytest.raises(AllowlistError, match=r"U\+2022 .* Literata styles"):
        repo.generate()


def test_the_styles_may_differ_outside_the_allowlist(tmp_path: Path) -> None:
    repo = build_repo(tmp_path, italic={**LITERATA, 0x0041: "A"})
    assert repo.generate() == 0


def test_a_supplementary_plane_code_point_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AllowlistError, match="outside the BMP"):
        allowlist.production_name(0x1F650)


def test_a_missing_upstream_tree_points_at_the_fetch_task(tmp_path: Path) -> None:
    repo = build_repo(tmp_path)
    (tmp_path / "build" / "upstream" / "dejavu" / "dejavu-fonts-ttf-2.37" / "ttf").rename(
        tmp_path / "build" / "upstream" / "dejavu" / "gone"
    )
    with pytest.raises(AllowlistError, match="mise run fetch"):
        repo.generate()


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #


def test_an_override_is_recorded_in_the_note_column(repo: Repo) -> None:
    assert repo.generate() == 0
    row = repo.by_codepoint()[0x25E6]
    assert row.source == "dejavu"
    assert row.note == "metrics of U+2022"
    assert all(other.note == "" for other in repo.rows() if other.codepoint != 0x25E6)


def test_an_override_on_a_preserved_glyph_is_refused(tmp_path: Path) -> None:
    """Nothing is imported for ◆, so there are no import metrics to adjust."""
    repo = build_repo(
        tmp_path,
        rules=RULES.replace('"U+25E6" = ', '"U+25C6" = ').replace(
            "{ match_metrics_of = \"U+2022\" }", '{ match_metrics_of = "U+2022" }'
        ),
    )
    with pytest.raises(AllowlistError, match="adjusts an import, but U.25C6 is literata"):
        repo.generate()


def test_an_override_of_an_unselected_code_point_is_refused(tmp_path: Path) -> None:
    repo = build_repo(tmp_path, rules=RULES.replace('"U+25E6" = ', '"U+2E19" = '))
    with pytest.raises(AllowlistError, match="no rule selects that code point"):
        repo.generate()


def test_an_override_referring_to_a_glyph_the_family_lacks_is_refused(
    tmp_path: Path,
) -> None:
    repo = build_repo(tmp_path, rules=RULES.replace('"U+2022" }', '"U+2E19" }'))
    with pytest.raises(AllowlistError, match="neither the allowlist nor Literata"):
        repo.generate()


# --------------------------------------------------------------------------- #
# Emoji presentation
# --------------------------------------------------------------------------- #


def test_the_emoji_flag_marks_the_code_points_that_need_fe0e(tmp_path: Path) -> None:
    """⚪ defaults to emoji; ❤ and ◇ do not (❤ needs U+FE0F to become one)."""
    repo = build_repo(
        tmp_path,
        rules=RULES.replace(
            '"Geometric Shapes" = ["U+25A0", "U+25A3"]',
            '"Geometric Shapes" = ["U+25A0", "U+25A3"]\n'
            '"Miscellaneous Symbols" = ["U+2600", "U+26FF"]\n'
            '"Dingbats" = ["U+2700", "U+27BF"]',
        ),
        dejavu=DEJAVU | {0x26AA, 0x2764},
    )
    assert repo.generate() == 0
    rows = repo.by_codepoint()

    assert rows[0x26AA].emoji_presentation == "yes"
    assert rows[0x2764].emoji_presentation == "no"
    assert rows[0x25C7].emoji_presentation == "no"
    assert allowlist.is_emoji_presentation("⚪") is True
    assert allowlist.is_emoji_presentation("❤") is False


# --------------------------------------------------------------------------- #
# The file itself
# --------------------------------------------------------------------------- #


def test_the_header_records_the_pins_and_the_generator_version(repo: Repo) -> None:
    assert repo.generate() == 0
    lines = repo.tsv.read_text(encoding="utf-8").splitlines()
    joined = "\n".join(line for line in lines if line.startswith("#"))

    assert f"generator {allowlist.GENERATOR_VERSION}" in joined
    assert "Literata 3.103" in joined
    assert "DejaVu Sans 2.37" in joined
    assert "00" * 32 in joined and "22" * 32 in joined
    assert f"{EXPECTED_TOTAL} rows" in joined


def test_render_and_parse_are_inverses(repo: Repo) -> None:
    assert repo.generate() == 0
    rows = repo.rows()
    assert allowlist.parse_rows(allowlist.render(rows)) == rows


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# only comments\n", "no column header"),
        ("codepoint\tchar\n", "expected the column header"),
        ("\t".join(allowlist.COLUMNS) + "\nU+2055\t⁕\n", "tab-separated fields"),
    ],
)
def test_a_damaged_manifest_is_rejected(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "allowlist.tsv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(AllowlistError, match=message):
        allowlist.read_tsv(path)


def test_reading_a_manifest_that_was_never_generated_says_so(tmp_path: Path) -> None:
    with pytest.raises(AllowlistError, match="mise run allowlist"):
        allowlist.read_tsv(tmp_path / "allowlist.tsv")


# --------------------------------------------------------------------------- #
# Drift
# --------------------------------------------------------------------------- #


def test_check_passes_on_a_freshly_generated_manifest(repo: Repo) -> None:
    assert repo.generate() == 0
    assert repo.generate(check=True) == 0


def test_check_never_repairs_the_file_it_is_checking(repo: Repo) -> None:
    assert repo.generate() == 0
    tampered = repo.tsv.read_bytes().replace(b"markers", b"MARKERS")
    repo.tsv.write_bytes(tampered)

    assert repo.generate(check=True) == 1
    assert repo.tsv.read_bytes() == tampered


def test_check_reports_a_stale_manifest_as_a_diff(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    assert repo.generate() == 0
    lines = repo.tsv.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = "".join(line for line in lines if "U+2731" not in line)
    repo.tsv.write_text(kept, encoding="utf-8")
    capsys.readouterr()

    assert repo.generate(check=True) == 1
    captured = capsys.readouterr()
    assert "+U+2731" in captured.out
    assert "run `mise run allowlist`" in captured.err


def test_check_fails_when_the_rules_changed_but_the_manifest_did_not(repo: Repo) -> None:
    assert repo.generate() == 0
    # Narrowing the range drops ■, which nothing else asks for.
    narrowed = repo.rules.read_text(encoding="utf-8").replace('"U+25A0", ', '"U+25A1", ')
    repo.rules.write_text(narrowed, encoding="utf-8")
    assert repo.generate(check=True) == 1


def test_check_without_a_manifest_fails(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert repo.generate(check=True) == 1
    assert "does not exist" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Bitstream Vera overlap
# --------------------------------------------------------------------------- #


def test_no_import_may_come_from_a_code_point_bitstream_vera_covers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """✱ is DejaVu-sourced; pretending Vera has it must stop the build."""
    repo = build_repo(tmp_path, vera=(0x00D7, 0x2022, 0x2731))
    assert repo.generate(quiet=False) == 1
    captured = capsys.readouterr()
    assert "U+2731" in captured.out
    assert "Bitstream Vera" in captured.err
    assert repo.generate(check=True) == 1


def test_a_preserved_glyph_may_overlap_bitstream_vera(repo: Repo) -> None:
    """× and • are in the fixture, but they come from Literata, not DejaVu."""
    assert repo.generate() == 0
    assert repo.by_codepoint()[0x00D7].source == "literata"


def test_the_overlap_report_counts_the_fixture(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    assert repo.generate(quiet=False) == 0
    assert "overlap: 0 of 3 fixture code point(s)" in capsys.readouterr().out


def test_a_missing_vera_fixture_is_an_error(repo: Repo) -> None:
    allowlist.vera_path_for(repo.root).unlink()
    with pytest.raises(AllowlistError, match="missing Bitstream Vera fixture"):
        repo.generate()


def test_the_checked_in_vera_fixture_is_bitstream_vera_1_10(repo_root: Path) -> None:
    path = allowlist.vera_path_for(repo_root)
    header = path.read_text(encoding="utf-8")

    assert len(allowlist.load_vera_codepoints(path)) == 256
    assert "c4c45690b345435b2cba52ecabe275f05e49b389b39fe68ad03afbb551288d3d" in header
    assert "65932" in header
    assert 'ID 5 "Release 1.10"' in header


# --------------------------------------------------------------------------- #
# Reporting and wiring
# --------------------------------------------------------------------------- #


def test_the_run_reports_counts_by_source(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    assert repo.generate(quiet=False) == 0
    out = capsys.readouterr().out

    assert f"{EXPECTED_TOTAL} rows" in out
    assert "custom       4" in out
    assert "literata    14" in out
    assert "dejavu      29" in out
    assert "required inventory: 43/43" in out


def test_the_allowlist_command_is_wired_to_this_module() -> None:
    assert COMMANDS["allowlist"] is allowlist.run
    assert not getattr(COMMANDS["allowlist"], "is_stub", False)


def test_the_command_takes_a_check_flag() -> None:
    assert build_parser().parse_args(["allowlist"]).check is False
    assert build_parser().parse_args(["allowlist", "--check"]).check is True


def test_a_failure_is_reported_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(allowlist.upstream, "default_root", lambda: tmp_path)
    args = build_parser().parse_args(["allowlist"])

    assert args.handler(args) == 1
    assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The checked-in manifest
# --------------------------------------------------------------------------- #


def test_the_checked_in_manifest_covers_the_required_inventory(repo_root: Path) -> None:
    """Shape only — no font is opened, so this holds without `mise run fetch`."""
    rows = {row.codepoint: row for row in allowlist.read_tsv(allowlist.tsv_path_for(repo_root))}
    rules = allowlist.load_rules(allowlist.rules_path_for(repo_root))
    vera = allowlist.load_vera_codepoints(allowlist.vera_path_for(repo_root))

    assert set(INVENTORY) <= set(rows), "a promised character has no row"
    inventory = {source: 0 for source in allowlist.SOURCES}
    for codepoint in INVENTORY:
        inventory[rows[codepoint].source] += 1
    # 13 = the 12 UI characters Literata draws (⁂ is replaced by a custom star)
    # plus the preserved bullet.
    assert inventory == {"custom": 4, "literata": 13, "dejavu": 26}
    assert {rows[cp].source for cp in rules.custom} == {"custom"}

    for row in rows.values():
        assert row.source in allowlist.SOURCES
        assert row.emoji_presentation in ("yes", "no")
        if row.source != "literata":
            assert row.glyph_name == allowlist.production_name(row.codepoint)
        assert row.codepoint not in vera or row.source != "dejavu"

    assert list(rows) == sorted(rows)
    assert not allowlist.vera_overlap(rows.values(), vera)


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "build" / "upstream" / ".verified").is_file(),
    reason="needs `mise run fetch`; the unit suite does not download anything",
)
def test_the_checked_in_manifest_matches_the_pinned_fonts(repo_root: Path) -> None:
    """The one test that reads the real donors, when they happen to be there."""
    assert allowlist.generate(repo_root, check=True, quiet=True) == 0
