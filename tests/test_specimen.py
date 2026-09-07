"""Unit tests for the specimen page.

The specimen is HTML, so what can usefully be pinned down is its *structure*:
that every allowlist row reaches the page in all four RIBBI styles, that the
``@font-face`` links are relative to ``fonts/`` (which is what makes the page
work out of the release zip), that an emoji-presentation row is rendered with
U+FE0E, and that text from the manifest is escaped rather than interpolated.
How it looks is a review question and belongs in front of a person, not in an
assertion about pixels.

Everything runs against a small fake family and a five-row manifest; the real
fonts are never opened, so the suite stays hermetic.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from asterwell_build import allowlist, assemble, specimen, stars

FAMILY = assemble.Family(
    family="Asterwell Text",
    ps_family="AsterwellText",
    version="1.000",
    vendor_id="ASTW",
    repo_url="https://example.invalid/asterwell",
    license_url="https://example.invalid/ofl",
    copyright_holder="The Asterwell Text Project Authors",
    copyright_year="2026",
    reserved_font_name="Asterwell",
)

PARAGRAPHS = [
    "The reading room kept its own weather — “every letter is an argument” — and "
    "the light went amber…",
    "A second paragraph, so the page has two.",
]


def _row(
    codepoint: int,
    source: str,
    *,
    emoji: str = "no",
    name: str = "TEST CHARACTER",
    block: str = "Dingbats",
) -> allowlist.Row:
    return allowlist.Row(
        codepoint=codepoint,
        char=chr(codepoint),
        unicode_name=name,
        block=block,
        source=source,
        glyph_name=f"uni{codepoint:04X}",
        group="test",
        emoji_presentation=emoji,
        note="",
    )


ROWS = [
    _row(0x273D, allowlist.SOURCE_CUSTOM),
    _row(0x2731, allowlist.SOURCE_DEJAVU),
    _row(0x2022, allowlist.SOURCE_LITERATA, block="General Punctuation"),
    # Emoji by default: the page has to defeat the platform's emoji font.
    _row(0x26A1, allowlist.SOURCE_DEJAVU, emoji="yes", block="Miscellaneous Symbols"),
    # A name with an ampersand in it: the page must escape, not interpolate.
    _row(0x2051, allowlist.SOURCE_CUSTOM, name="TWO ASTERISKS ALIGNED <&> VERTICALLY"),
]

NAMES = {
    "roman": "AsterwellText[opsz,wght].woff2",
    "italic": "AsterwellText-Italic[opsz,wght].woff2",
}

#: The italic treatment the page is rendered with — the checked-in design's
#: numbers, kept here rather than read from ``sources/stars.toml`` so the suite
#: stays hermetic.
ITALIC_TREATMENT = stars.ItalicTreatment(rotation=30.0, stack_slant=2.5)


@pytest.fixture(scope="module")
def page() -> str:
    return specimen.render(
        family=FAMILY,
        rows=ROWS,
        paragraphs=PARAGRAPHS,
        names=NAMES,
        italic=ITALIC_TREATMENT,
        inventory="✽⁎•",
    )


# --------------------------------------------------------------------------- #
# The page as a whole
# --------------------------------------------------------------------------- #


def test_the_page_is_html_with_a_title_naming_the_family_and_version(page: str) -> None:
    assert page.startswith("<!DOCTYPE html>")
    assert "<title>Asterwell Text 1.000 — specimen</title>" in page
    assert page.rstrip().endswith("</html>")


def test_every_section_the_spec_asks_for_is_present(page: str) -> None:
    assert re.findall(r'<section id="([^"]+)"', page) == [
        "prose",
        "inventory",
        "stars",
        "ramp",
        "allowlist",
    ]


def test_the_page_is_self_contained_apart_from_the_two_web_fonts(page: str) -> None:
    """No stylesheet, no script, no image: the only thing the page fetches is
    the two font files beside it in the release zip."""
    assert "<link" not in page
    assert "<script" not in page
    assert "<img" not in page
    external = re.findall(r'url\("([^"]+)"\)', page)
    assert all(reference.startswith("../webfonts/") for reference in external)


def test_the_font_face_links_are_relative_and_percent_encoded(page: str) -> None:
    """The filenames carry brackets and a comma; encoded, they resolve the same
    way from a file:// URL as from a server."""
    sources = re.findall(r'src: url\("([^"]+)"\)', page)
    assert sources == [
        "../webfonts/AsterwellText%5Bopsz%2Cwght%5D.woff2",
        "../webfonts/AsterwellText-Italic%5Bopsz%2Cwght%5D.woff2",
    ]
    assert "font-weight: 200 900;" in page
    assert page.count("@font-face") == 2


def test_the_page_does_not_borrow_an_installed_copy_of_the_family(page: str) -> None:
    assert f'font-family: "{specimen.CSS_FAMILY}"' in page
    assert specimen.CSS_FAMILY != FAMILY.family


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def test_the_prose_appears_in_three_styles_at_three_sizes_on_two_panels(
    page: str,
) -> None:
    prose = page.split('<section id="prose"')[1].split("</section>")[0]
    assert prose.count('class="panel light"') == 1
    assert prose.count('class="panel dark"') == 1
    for face in specimen.PROSE_FACES:
        for size in specimen.PROSE_SIZES:
            assert f"{face.label} · {size}px" in prose
    # Two paragraphs × 3 faces × 3 sizes × 2 panels.
    assert prose.count("<p style=") == len(PARAGRAPHS) * 3 * 3 * 2


def test_the_prose_punctuation_survives_into_the_page(page: str) -> None:
    for character in ("“", "”", "—", "…"):
        assert character in page


def test_the_inventory_is_shown_at_four_sizes_in_four_styles(page: str) -> None:
    inventory = page.split('<section id="inventory"')[1].split("</section>")[0]
    for face in specimen.RIBBI:
        assert f"<th>{face.label}</th>" in inventory
    for size in specimen.INVENTORY_SIZES:
        assert f"<th>{size}px</th>" in inventory
        assert inventory.count(f"font-size:{size}px") == len(specimen.RIBBI)


def test_the_star_row_stands_beside_the_asterisk_and_the_diamond(page: str) -> None:
    section = page.split('<section id="stars"')[1].split("</section>")[0]
    assert specimen.STAR_ROW[:5] == ("✽", "✻", "✼", "✾", "❃")
    assert specimen.STAR_ROW[-2:] == ("*", "◆")
    for char in specimen.STAR_ROW:
        assert char in section
    for size in specimen.STAR_SIZES:
        assert section.count(f"font-size:{size}px") == len(specimen.STAR_ROW) * len(
            specimen.STAR_FACES
        )


def test_the_stars_are_shown_in_both_styles(page: str) -> None:
    """D21: the italic's stars are a different drawing, so one row would show
    half the family. Each size gets a labelled Regular row and Italic row."""
    section = page.split('<section id="stars"')[1].split("</section>")[0]
    assert specimen.STAR_FACES == (specimen.REGULAR, specimen.ITALIC)
    for size in specimen.STAR_SIZES:
        for face in specimen.STAR_FACES:
            assert f'<span class="label">{size}px {face.label}</span>' in section
            assert f'<span class="{face.key}" style="font-size:{size}px">' in section
    assert section.count('class="starrow"') == len(specimen.STAR_SIZES) * len(
        specimen.STAR_FACES
    )


def test_the_star_lede_states_the_italic_treatment_it_was_given(page: str) -> None:
    """The angles come from ``sources/stars.toml``, so retuning the treatment
    retunes the sentence rather than leaving the page saying the old number."""
    other = specimen.render(
        family=FAMILY,
        rows=ROWS,
        paragraphs=PARAGRAPHS,
        names=NAMES,
        italic=stars.ItalicTreatment(rotation=20.0, stack_slant=4.0),
        inventory="✽⁎•",
    )
    lede = page.split('<section id="stars"')[1].split("</section>")[0]
    assert f"turned {ITALIC_TREATMENT.rotation:g}°" in lede
    assert f"lean {ITALIC_TREATMENT.stack_slant:g}°" in lede
    changed = other.split('<section id="stars"')[1].split("</section>")[0]
    assert "turned 20°" in changed and "lean 4°" in changed


def test_the_weight_ramp_walks_every_weight_with_a_symbol_inline(page: str) -> None:
    ramp = page.split('<section id="ramp"')[1].split("</section>")[0]
    for weight in specimen.RAMP_WEIGHTS:
        assert f"font-weight:{weight}" in ramp
    assert ramp.count(specimen.RAMP_SYMBOL) >= len(specimen.RAMP_WEIGHTS)


# --------------------------------------------------------------------------- #
# The allowlist grid
# --------------------------------------------------------------------------- #


def test_every_row_reaches_the_grid_in_all_four_styles(page: str) -> None:
    grid = page.split('<section id="allowlist"')[1]
    assert grid.count("<li data-source=") == len(ROWS)
    for face in specimen.RIBBI:
        assert grid.count(f'class="g {face.key}"') + grid.count(
            f'class="g {face.key} fe0e"'
        ) == len(ROWS)


def test_each_row_carries_its_code_point_and_a_source_badge(page: str) -> None:
    for row in ROWS:
        assert f"<code>{allowlist.format_codepoint(row.codepoint)}</code>" in page
    assert page.count('class="badge custom"') == 2
    assert page.count('class="badge dejavu"') == 2
    assert page.count('class="badge literata"') == 1


def test_an_emoji_presentation_row_is_rendered_with_the_text_selector() -> None:
    emoji, plain = ROWS[3], ROWS[0]
    assert specimen.display_text(emoji) == "⚡︎"
    assert specimen.display_text(plain) == "✽"


def test_the_emoji_row_is_marked_in_the_page(page: str) -> None:
    assert page.count('class="g regular fe0e"') == 1
    assert "︎" in page


def test_the_rows_are_grouped_by_unicode_block(page: str) -> None:
    headings = re.findall(r"<h3>([^<]+)</h3>", page)
    assert headings == [
        "Dingbats · 3 code points",
        "General Punctuation · 1 code points",
        "Miscellaneous Symbols · 1 code points",
    ]


def test_manifest_text_is_escaped_rather_than_interpolated(page: str) -> None:
    assert "TWO ASTERISKS ALIGNED &lt;&amp;&gt; VERTICALLY" in page
    assert "<&>" not in page


# --------------------------------------------------------------------------- #
# The prose file
# --------------------------------------------------------------------------- #


def test_paragraphs_undo_the_files_line_wrapping(tmp_path: Path) -> None:
    path = tmp_path / "specimen-text.txt"
    path.write_text("one line\nwrapped here\n\nsecond paragraph\n", encoding="utf-8")
    assert specimen.load_paragraphs(path) == ["one line wrapped here", "second paragraph"]


def test_a_missing_prose_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(specimen.SpecimenError, match="missing prose sample"):
        specimen.load_paragraphs(tmp_path / "nothing.txt")


def test_an_empty_prose_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("\n\n  \n", encoding="utf-8")
    with pytest.raises(specimen.SpecimenError, match="no prose"):
        specimen.load_paragraphs(path)


def test_the_shipped_sample_has_what_the_specimen_needs(repo_root: Path) -> None:
    """§13 asks for ≈120 words with curly quotes, an ellipsis, an em dash and a
    footnote marker — the punctuation the specimen exists to show."""
    text = specimen.text_path_for(repo_root).read_text(encoding="utf-8")
    assert 100 <= len(text.split()) <= 150
    for character in ("“", "”", "’", "—", "…", "✽"):
        assert character in text


# --------------------------------------------------------------------------- #
# Writing the file
# --------------------------------------------------------------------------- #


def test_the_webfont_names_follow_the_variable_fonts() -> None:
    assert specimen.webfont_names(FAMILY) == NAMES


def test_build_refuses_when_the_web_fonts_are_not_there(tmp_path: Path) -> None:
    (tmp_path / "sources").mkdir()
    with pytest.raises(specimen.SpecimenError, match="web font"):
        specimen._require_webfonts(tmp_path, NAMES.values())


def test_build_writes_the_page_where_the_release_zip_expects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    fonts = tmp_path / "fonts"
    (fonts / "webfonts").mkdir(parents=True)
    for name in NAMES.values():
        (fonts / "webfonts" / name).write_bytes(b"")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "family.toml").write_text(
        "\n".join(
            f'{key} = "{value}"'
            for key, value in (
                ("family", FAMILY.family),
                ("ps_family", FAMILY.ps_family),
                ("version", FAMILY.version),
                ("vendor_id", FAMILY.vendor_id),
                ("repo_url", FAMILY.repo_url),
                ("license_url", FAMILY.license_url),
                ("copyright_holder", FAMILY.copyright_holder),
                ("copyright_year", FAMILY.copyright_year),
                ("reserved_font_name", FAMILY.reserved_font_name),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "sources" / "allowlist.tsv").write_text(
        "\t".join(allowlist.COLUMNS)
        + "\n"
        + "\n".join("\t".join(row.fields()) for row in ROWS)
        + "\n",
        encoding="utf-8",
    )
    # The star section states the italic treatment's own angles, so `build`
    # has to read them from the root it was handed.
    shutil.copyfile(
        repo_root / "sources" / "stars.toml", tmp_path / "sources" / "stars.toml"
    )
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa" / "specimen-text.txt").write_text(
        "\n\n".join(PARAGRAPHS) + "\n", encoding="utf-8"
    )

    output = specimen.build(tmp_path, quiet=True)
    assert output == fonts / "specimen" / "index.html"
    page = output.read_text(encoding="utf-8")
    assert page.count("<li data-source=") == len(ROWS)
    checked_in = stars.load_parameters(stars.parameters_path_for(tmp_path)).italic
    assert f"turned {checked_in.rotation:g}°" in page
