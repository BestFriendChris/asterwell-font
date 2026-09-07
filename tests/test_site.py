"""Unit tests for the showcase site.

The page is the family's public face, so what these tests pin down is what a
visitor's browser would otherwise have to be trusted with:

*That it fetches nothing.* Every absolute URL on the page is checked against
the short allowlist in §15.2.4, and the two things that would quietly reach off
the machine — ``<script src>`` and ``<link rel=stylesheet>`` — are asserted
absent rather than merely not written today.

*That the playground is wired to elements that exist.* The script is inline and
never runs in this suite, so every ``getElementById`` in it is matched against
the ids in the HTML: a renamed control is a test failure here rather than a
dead slider on a published page.

*That the output is stable.* Two runs produce identical bytes, the two web
fonts are copies of the built ones, and the specimen is carried through
verbatim so its ``../webfonts/`` rules resolve inside ``fonts/site/``.

Everything is hermetic: a synthetic family, a six-row manifest, two dummy WOFF2
files and a dummy specimen in ``tmp_path``. The real fonts are never opened.
The one exception is a small group at the end that renders the *checked-in*
``sources/site.toml`` against the *checked-in* manifest — still no fonts, still
no network, but it is the copy that will actually be published.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from asterwell_build import allowlist, assemble, site, specimen, stars, upstream
from asterwell_build.cli import COMMANDS

# --------------------------------------------------------------------------- #
# The public-repo check (§15.2.4)
# --------------------------------------------------------------------------- #

#: Substrings that must never reach a published page. The list is structural:
#: it is committed empty, and the coordinator fills it in out of band when
#: something has to be kept off the site. An empty list still exercises the
#: check's plumbing, so filling it in later is a data change, not a new test.
PRIVATE_REFERENCES: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Fixtures — a whole tree the site can be built from, with no fonts in it
# --------------------------------------------------------------------------- #

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

NAMES = {
    "roman": "AsterwellText[opsz,wght].woff2",
    "italic": "AsterwellText-Italic[opsz,wght].woff2",
}

ITALIC_TREATMENT = stars.ItalicTreatment(rotation=30.0, stack_slant=2.5)

PINS = (
    upstream.Pin(
        key="literata",
        name="Literata",
        version="3.103",
        url="https://example.invalid/literata.zip",
        sha256="0" * 64,
        size=1,
        members={"x": "0" * 64},
    ),
    upstream.Pin(
        key="dejavu",
        name="DejaVu Sans",
        version="2.37",
        url="https://example.invalid/dejavu.tar.bz2",
        sha256="1" * 64,
        size=1,
        members={"y": "1" * 64},
    ),
)

REPO_URL = "https://github.com/BestFriendChris/asterwell-font"
RELEASE_URL = f"{REPO_URL}/releases/latest"
SITE_URL = "https://bestfriendchris.github.io/asterwell-font/"

#: The emoji-presentation character the fixture copy carries, so the U+FE0E
#: rule is exercised by a real string on the page rather than in the abstract.
EMOJI_CHAR = "⚡"


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
    _row(0x2051, allowlist.SOURCE_CUSTOM),
    _row(0x2022, allowlist.SOURCE_LITERATA, block="General Punctuation"),
    _row(0x2731, allowlist.SOURCE_DEJAVU),
    _row(0x2318, allowlist.SOURCE_DEJAVU, block="Miscellaneous Technical"),
    # Emoji by default: the page has to defeat the platform's colour artwork.
    _row(0x26A1, allowlist.SOURCE_DEJAVU, emoji="yes", block="Miscellaneous Symbols"),
]

#: A complete `site.toml`, with a `<` and an `&` in the copy (the page must
#: escape rather than interpolate) and the emoji character in two places.
SITE_TOML = f"""
title = "Asterwell Text"
tagline = "A serif for reading & for the <interface> around it"
repo_url = "{REPO_URL}"
release_url = "{RELEASE_URL}"
site_url = "{SITE_URL}"

[[section]]
key = "optical"
heading = "Optical sizes"
demo = "opsz"
body = ["Say `font-optical-sizing: auto` once.", "A second paragraph."]

[[section]]
key = "steady"
heading = "Steady symbols"
demo = "steady"
body = ["The ornament {EMOJI_CHAR} holds still while the prose thickens."]

[[section]]
key = "stars"
heading = "An original star family"
demo = "stars"
body = ["Six petals, one template."]

[[section]]
key = "provenance"
heading = "Made to be relied on"
demo = "provenance"
body = ["Every code point has a row."]

[examples]
manuscript = ["The archive kept its own weather.", "“Every entry is an argument.”"]
margin_note = "The footnote star is ✽."
interface = "⌘S Save   ✓ Done   {EMOJI_CHAR} Power"

[playground]
default_text = "The quick brown fox — “quoted”, dashed… and starred ✽"
default_weight = 400
default_size = 24
"""

SPECIMEN_HTML = (
    "<!DOCTYPE html><html><head><style>@font-face { "
    '  src: url("../webfonts/AsterwellText%5Bopsz%2Cwght%5D.woff2"); }'
    "</style></head><body>a dummy specimen</body></html>\n"
)

#: Bytes standing in for the two variable web fonts. Different lengths so a
#: copy that swapped them would be caught by the size assertions.
WOFF2_BYTES = {
    NAMES["roman"]: b"wOF2-roman-" + b"r" * 64,
    NAMES["italic"]: b"wOF2-italic-" + b"i" * 32,
}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A repository root with everything ``site.build`` reads, and no fonts."""
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "site.toml").write_text(SITE_TOML, encoding="utf-8")
    (sources / "family.toml").write_text(
        "\n".join(
            f'{key} = "{value}"'
            for key, value in (
                ("family", FAMILY.family),
                ("ps_family", FAMILY.ps_family),
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
    (sources / "allowlist.tsv").write_text(
        "\t".join(allowlist.COLUMNS)
        + "\n"
        + "\n".join("\t".join(row.fields()) for row in ROWS)
        + "\n",
        encoding="utf-8",
    )
    # The star card states the italic treatment's own angles, and the footer
    # names the pins: both are read from the root `build` is handed.
    for name in ("stars.toml", "upstream.toml"):
        (sources / name).write_bytes((_REPO / "sources" / name).read_bytes())

    fonts = tmp_path / "fonts"
    (fonts / "webfonts").mkdir(parents=True)
    for name, payload in WOFF2_BYTES.items():
        (fonts / "webfonts" / name).write_bytes(payload)
    (fonts / "specimen").mkdir()
    (fonts / "specimen" / "index.html").write_text(SPECIMEN_HTML, encoding="utf-8")
    return tmp_path


_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def content() -> site.Content:
    return site.load_content_text(SITE_TOML, where="test site.toml")


@pytest.fixture
def page(content: site.Content) -> str:
    return site.render(
        family=FAMILY,
        content=content,
        rows=ROWS,
        names=NAMES,
        pins=PINS,
        italic=ITALIC_TREATMENT,
    )


@pytest.fixture
def built(tree: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``fonts/site`` after one run, as a directory."""
    # family.toml carries no version since §15.4: the build is told one.
    monkeypatch.setenv("ASTERWELL_VERSION", FAMILY.version)
    site.build(tree, quiet=True)
    return site.site_dir_for(tree)


# --------------------------------------------------------------------------- #
# The copy file
# --------------------------------------------------------------------------- #


def test_the_copy_is_parsed_into_the_sections_it_declares(content: site.Content) -> None:
    assert [section.key for section in content.sections] == [
        "optical",
        "steady",
        "stars",
        "provenance",
    ]
    assert [section.demo for section in content.sections] == list(site.DEMOS)
    assert content.playground.default_weight == 400
    assert content.examples.margin_note.startswith("The footnote star")


def test_a_section_naming_an_unknown_demo_is_refused() -> None:
    broken = SITE_TOML.replace('demo = "stars"', 'demo = "pinwheels"')
    with pytest.raises(site.SiteError, match="unknown demo 'pinwheels'"):
        site.load_content_text(broken, where="broken.toml")


def test_every_demo_key_names_a_block_site_py_can_draw() -> None:
    """The four keys are code, not copy: nothing else may be written there."""
    assert site.DEMOS == ("opsz", "steady", "stars", "provenance")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("title", 'title = ""'),
        ("tagline", 'tagline = ""'),
        ("heading", 'heading = ""'),
        ("body", "body = []"),
        ("margin_note", 'margin_note = ""'),
        ("default_text", 'default_text = ""'),
    ],
)
def test_an_empty_string_anywhere_in_the_copy_is_refused(
    field: str, replacement: str
) -> None:
    line = next(
        text
        for text in SITE_TOML.splitlines()
        if text.startswith(f"{field} =")
    )
    with pytest.raises(site.SiteError, match=field):
        site.load_content_text(SITE_TOML.replace(line, replacement, 1), where="x.toml")


def test_a_playground_default_outside_the_control_range_is_refused() -> None:
    for line, replacement in (
        ("default_weight = 400", "default_weight = 1000"),
        ("default_size = 24", "default_size = 4"),
    ):
        with pytest.raises(site.SiteError, match="between"):
            site.load_content_text(SITE_TOML.replace(line, replacement, 1), where="x.toml")


def test_a_section_key_that_is_not_an_html_id_is_refused() -> None:
    """The key is the card's anchor; a space or a capital in it is a link that
    silently does not work."""
    for broken in ('key = "Optical Sizes"', 'key = "-optical"'):
        with pytest.raises(site.SiteError, match="not usable as an HTML id"):
            site.load_content_text(
                SITE_TOML.replace('key = "optical"', broken, 1), where="x.toml"
            )


def test_a_repeated_section_key_is_refused() -> None:
    broken = SITE_TOML.replace('key = "stars"', 'key = "steady"')
    with pytest.raises(site.SiteError, match="repeats the key"):
        site.load_content_text(broken, where="x.toml")


def test_a_missing_copy_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(site.SiteError, match="missing site copy"):
        site.load_content(tmp_path / "nothing.toml")


# --------------------------------------------------------------------------- #
# The page: structure
# --------------------------------------------------------------------------- #


def test_the_page_is_html_with_a_title_naming_the_family(page: str) -> None:
    assert page.startswith("<!DOCTYPE html>")
    assert "<title>Asterwell Text — A serif for reading &amp; for the" in page
    assert page.rstrip().endswith("</html>")


def test_the_page_carries_every_part_the_design_asks_for(page: str) -> None:
    assert "<header>" in page and "</footer>" in page
    assert re.findall(r'<section id="([^"]+)"', page) == [
        "special",
        "examples",
        "playground",
        "get",
    ]


def test_the_four_cards_render_in_the_order_the_copy_lists_them(
    page: str, content: site.Content
) -> None:
    show = site.Presentation(ROWS)
    assert re.findall(r'<article class="card" id="([^"]+)"', page) == [
        section.key for section in content.sections
    ]
    for section in content.sections:
        assert f"<h3>{section.heading}</h3>" in page
        for paragraph in section.body:
            assert f"<p>{show.prose(paragraph)}</p>" in page


def test_each_cards_own_demo_block_is_drawn_under_it(page: str) -> None:
    special = page.split('<section id="special"')[1].split('<section id="examples"')[0]
    cards = special.split('<article class="card"')[1:]
    assert len(cards) == len(site.DEMOS)
    opsz, steady, stars_card, provenance = cards
    # opsz: the same sentence three times — twice sized, once pinned at opsz 12.
    assert opsz.count(site.OPSZ_SENTENCE) == 3
    assert f'style="font-size:{site.OPSZ_SMALL}px"' in opsz
    assert 'class="opsz-line pinned"' in opsz
    assert '"opsz" 12' in opsz
    # steady: the whole weight range with the symbols inline.
    for weight in site.RAMP_WEIGHTS:
        assert f"font-weight:{weight}" in steady
    assert steady.count(site.RAMP_SYMBOLS) >= len(site.RAMP_WEIGHTS)
    # stars: both rows, in both styles.
    for character in site.STAR_ROW:
        assert character in stars_card
    for character in site.SIBLING_ROW:
        assert character in stars_card
    assert stars_card.count('class="starrow"') == 2 * len(site.STAR_FACES)
    # provenance: one badge per source, carrying that source's count.
    summary = allowlist.summarize(ROWS)
    for source in allowlist.SOURCES:
        assert f'<span class="badge {source}">{source}</span>' in provenance
        assert f"<b>{summary.by_source[source]}</b>" in provenance
    assert f"{len(ROWS)} code points" in provenance
    assert 'href="specimen/"' in provenance


def test_the_stars_card_states_the_treatment_the_fonts_were_drawn_with(
    content: site.Content,
) -> None:
    """The angles come from ``sources/stars.toml``, so retuning the treatment
    retunes the page rather than leaving it claiming the old number."""
    other = site.render(
        family=FAMILY,
        content=content,
        rows=ROWS,
        names=NAMES,
        pins=PINS,
        italic=stars.ItalicTreatment(rotation=20.0, stack_slant=4.0),
    )
    assert "turned 20°" in other and "lean 4°" in other


def test_the_examples_set_prose_and_an_interface_line_light_and_dark(
    page: str, content: site.Content
) -> None:
    examples = page.split('<section id="examples"')[1].split("</section>")[0]
    assert examples.count('class="panel light"') == 1
    assert examples.count('class="panel dark"') == 1
    assert 'class="panel manuscript"' in examples
    assert 'class="margin-note"' in examples
    assert examples.count('class="ui-line"') == 2
    assert "The archive kept its own weather." in examples


def test_the_manuscript_is_set_at_the_reading_size_the_design_asks_for(
    page: str,
) -> None:
    """17 px, 1.8 line height, 38 em measure (§15.2.3)."""
    assert ".manuscript p { font-size: 17px; line-height: 1.8; max-width: 38em;" in page
    assert ".ui-line { font-size: 14px;" in page


def test_the_get_it_section_shows_the_release_asset_names(page: str) -> None:
    get = page.split('<section id="get"')[1].split("</section>")[0]
    assert get.count("@font-face {") == 2
    for name in NAMES.values():
        assert name in get
    assert "Reserved Font Name" in get
    assert "sold on its own" in get


def test_the_footer_names_the_version_and_both_upstream_pins(page: str) -> None:
    footer = page.split("<footer>")[1]
    assert FAMILY.version in footer
    assert "Literata 3.103" in footer
    assert "DejaVu Sans 2.37" in footer


# --------------------------------------------------------------------------- #
# The page: the playground
# --------------------------------------------------------------------------- #


def test_the_playground_has_all_four_controls_and_the_dark_toggle(page: str) -> None:
    playground = page.split('<section id="playground"')[1].split("</section>")[0]
    assert '<textarea id="pg-text"' in playground
    assert (
        f'<input type="range" id="pg-size" min="{site.SIZE_MIN}" '
        f'max="{site.SIZE_MAX}"' in playground
    )
    assert (
        f'<input type="range" id="pg-weight" min="{site.WEIGHT_MIN}" '
        f'max="{site.WEIGHT_MAX}"' in playground
    )
    assert 'id="pg-roman"' in playground and 'id="pg-italic"' in playground
    assert 'type="checkbox" id="pg-dark"' in playground
    for label, weight in site.WEIGHT_STOPS:
        assert f'data-weight="{weight}" aria-pressed=' in playground
        assert f">{label}</button>" in playground


def test_the_playground_works_before_the_script_runs(
    page: str, content: site.Content
) -> None:
    """No JS: the preview is already rendered at the defaults, and the CSS
    snippet already says what it is showing."""
    playground = page.split('<section id="playground"')[1].split("</section>")[0]
    default = content.playground
    assert (
        f'<div id="pg-preview" class="preview" style="font-size:{default.default_size}px;'
        f"font-weight:{default.default_weight};font-style:normal\">" in playground
    )
    assert default.default_text in playground  # in the textarea and in the preview
    assert (
        f"font-family: &quot;{FAMILY.family}&quot;;\n"
        f"font-weight: {default.default_weight};\n"
        "font-style: normal;\n"
        f"font-size: {default.default_size}px;\n"
        "font-optical-sizing: auto;" in playground
    )


def test_the_size_control_drives_the_optical_size(page: str) -> None:
    """Q7: no separate opsz control — the preview says
    ``font-optical-sizing: auto``, so dragging the size drags opsz with it."""
    assert ".preview {" in page
    preview_rule = page.split(".preview {")[1].split("}")[0]
    assert "font-optical-sizing: auto" in preview_rule
    assert 'id="pg-opsz"' not in page


def test_every_element_the_script_reaches_for_exists_in_the_page(page: str) -> None:
    wanted = re.findall(r'getElementById\("([^"]+)"\)', page)
    assert len(wanted) >= 11, "the script should be reaching for every control"
    present = set(re.findall(r'\bid="([^"]+)"', page))
    assert not [name for name in wanted if name not in present]


def test_the_script_rebuilds_the_same_five_declarations(page: str) -> None:
    script = page.split("<script>")[1].split("</script>")[0]
    for declaration in (
        "font-family",
        "font-weight: ",
        "font-style: ",
        "font-size: ",
        "font-optical-sizing: auto;",
    ):
        assert declaration in script
    assert 'preview.classList.add("dark")' in script


def test_the_copy_cannot_close_the_script_element(content: site.Content) -> None:
    """Copy reaches the script as a string literal; a stray ``</script>`` in it
    would end the element early and dump the rest of the page as markup."""
    hostile = site.Content(
        title=content.title,
        tagline=content.tagline,
        repo_url=content.repo_url,
        release_url=content.release_url,
        site_url=content.site_url,
        sections=content.sections,
        examples=content.examples,
        playground=site.Playground(
            default_text="</script><script>alert(1)</script>",
            default_weight=400,
            default_size=24,
        ),
    )
    page = site.render(
        family=FAMILY,
        content=hostile,
        rows=ROWS,
        names=NAMES,
        pins=PINS,
        italic=ITALIC_TREATMENT,
    )
    assert page.count("<script>") == 1
    assert page.count("</script>") == 1
    assert "&lt;/script&gt;" in page  # escaped into the textarea and the preview


# --------------------------------------------------------------------------- #
# The page: §15.2.4's constraints
# --------------------------------------------------------------------------- #


def test_the_page_fetches_nothing_from_anywhere(page: str) -> None:
    assert "<script src" not in page
    assert "<link" not in page
    assert "<img" not in page
    assert "<iframe" not in page
    assert "@import" not in page
    assert "integrity=" not in page


def test_every_font_the_page_loads_is_one_beside_it(page: str) -> None:
    references = re.findall(r'url\("([^"]+)"\)', page)
    assert references and all(
        reference.startswith(site.WEBFONT_PREFIX) for reference in references
    )
    # Two in the stylesheet, two more in the copy-and-paste snippet.
    assert page.count("@font-face {") == 2 + 2
    assert page.count("font-weight: 200 900;") == 2 + 2
    # The names carry brackets and a comma; encoded, they resolve the same way
    # from a file:// URL as from Pages.
    assert references == [
        "webfonts/AsterwellText%5Bopsz%2Cwght%5D.woff2",
        "webfonts/AsterwellText-Italic%5Bopsz%2Cwght%5D.woff2",
    ]


def test_every_absolute_url_on_the_page_is_one_the_spec_allows(
    page: str, content: site.Content
) -> None:
    allowed = site.allowed_urls(content)
    found = sorted(set(site.URL_RE.findall(page)))
    assert found, "the page should at least credit its upstreams"
    unexpected = [
        url for url in found if not any(url.startswith(prefix) for prefix in allowed)
    ]
    assert not unexpected, f"unexpected external references: {unexpected}"


def test_the_page_credits_both_upstream_projects_and_both_licences(page: str) -> None:
    for url in (
        site.LITERATA_URL,
        site.TYPETOGETHER_URL,
        site.DEJAVU_URL,
        site.DEJAVU_LICENSE_URL,
        site.OFL_URL,
    ):
        assert url in page


def test_the_specimen_is_linked_by_a_relative_path(page: str) -> None:
    """Pages project sites live under a path prefix, so a leading-slash or
    absolute link to the sub-page would 404 in production and work locally."""
    assert page.count(f'href="{site.SPECIMEN_HREF}"') >= 2
    assert 'href="/' not in page
    assert not [url for url in site.URL_RE.findall(page) if "specimen" in url]


def test_every_emoji_presentation_code_point_shown_carries_the_selector(
    page: str,
) -> None:
    selector = specimen.TEXT_PRESENTATION_SELECTOR
    emoji = [row for row in ROWS if row.emoji_presentation == "yes"]
    assert emoji, "the fixture must contain one, or this proves nothing"
    for row in emoji:
        assert row.char in page, "the fixture copy should put it on the page"
        for index in (m.start() for m in re.finditer(re.escape(row.char), page)):
            assert page[index + 1 : index + 2] == selector, (
                f"{allowlist.format_codepoint(row.codepoint)} is set without U+FE0E"
            )
    # A row that is not emoji by default is left exactly as written.
    assert "✽" in page and "✽" + selector not in page


def test_copy_is_escaped_rather_than_interpolated(page: str) -> None:
    assert "reading &amp; for the &lt;interface&gt;" in page
    assert "<interface>" not in page


def test_backticks_in_the_copy_become_code_not_backticks(page: str) -> None:
    assert "<code>font-optical-sizing: auto</code>" in page
    assert "`" not in page


def test_the_page_names_no_private_reference(page: str) -> None:
    """Structural: the list is committed empty and filled in out of band."""
    assert not [text for text in PRIVATE_REFERENCES if text in page]


def test_the_page_stays_under_the_size_budget(page: str) -> None:
    """§15.2.3: under 150 KB of HTML, fonts excluded."""
    assert len(page.encode("utf-8")) < 150_000


def test_the_page_honours_the_readers_colour_scheme(page: str) -> None:
    assert "@media (prefers-color-scheme: dark)" in page
    assert ".preview.dark {" in page


# --------------------------------------------------------------------------- #
# Writing the tree
# --------------------------------------------------------------------------- #


def test_build_writes_the_four_things_pages_is_handed(built: Path, tree: Path) -> None:
    assert (built / "index.html").is_file()
    assert (built / ".nojekyll").read_bytes() == b""
    for name, payload in WOFF2_BYTES.items():
        copied = built / "webfonts" / name
        assert copied.read_bytes() == payload
        source = assemble.fonts_dir_for(tree) / "webfonts" / name
        assert copied.stat().st_size == source.stat().st_size
    assert (built / "specimen" / "index.html").read_text(encoding="utf-8") == SPECIMEN_HTML


def test_the_specimens_own_font_links_resolve_inside_the_site(built: Path) -> None:
    """The copied specimen points at ``../webfonts/`` — which, one directory
    down from ``fonts/site/``, is the pair of files beside the showcase page."""
    page = (built / "specimen" / "index.html").read_text(encoding="utf-8")
    for reference in re.findall(r'url\("\.\./([^"]+)"\)', page):
        assert (built / reference.replace("%5B", "[").replace("%5D", "]").replace(
            "%2C", ","
        )).is_file()


def test_two_runs_produce_identical_bytes(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTERWELL_VERSION", FAMILY.version)
    site.build(tree, quiet=True)
    first = {
        path.relative_to(site.site_dir_for(tree)): path.read_bytes()
        for path in sorted(site.site_dir_for(tree).rglob("*"))
        if path.is_file()
    }
    site.build(tree, quiet=True)
    second = {
        path.relative_to(site.site_dir_for(tree)): path.read_bytes()
        for path in sorted(site.site_dir_for(tree).rglob("*"))
        if path.is_file()
    }
    assert first == second
    assert set(first) == {
        Path("index.html"),
        Path(".nojekyll"),
        Path("webfonts") / NAMES["roman"],
        Path("webfonts") / NAMES["italic"],
        Path("specimen") / "index.html",
    }


def test_a_stale_web_font_from_an_earlier_run_is_not_published(
    built: Path, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTERWELL_VERSION", FAMILY.version)
    stale = built / "webfonts" / "AsterwellText-0.001[opsz,wght].woff2"
    stale.write_bytes(b"an older release's file name")
    site.build(tree, quiet=True)
    assert not stale.exists()


def test_a_missing_web_font_names_the_task_that_builds_one(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTERWELL_VERSION", FAMILY.version)
    (assemble.fonts_dir_for(tree) / "webfonts" / NAMES["italic"]).unlink()
    with pytest.raises(site.SiteError, match=r"mise run build"):
        site.build(tree, quiet=True)


def test_a_missing_specimen_names_the_task_that_renders_one(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTERWELL_VERSION", FAMILY.version)
    (assemble.fonts_dir_for(tree) / "specimen" / "index.html").unlink()
    with pytest.raises(site.SiteError, match=r"mise run specimen"):
        site.build(tree, quiet=True)


def test_the_site_is_not_a_member_of_the_release_zip(tmp_path: Path) -> None:
    """The zip is the font family; the site is how the family is shown.

    ``package.manifest`` is an explicit list rather than a glob, so the check is
    that nobody added the site to it: asked for a tree that has none of the
    files, it names every member it wanted, and the specimen is in that list
    while ``site/`` is not.
    """
    from asterwell_build import package

    with pytest.raises(package.PackageError) as exc:
        package.manifest(tmp_path, {"outputs": {"variable/AsterwellText.ttf": "0" * 64}})
    wanted = str(exc.value)
    assert f"{specimen.SPECIMEN_DIR}/{specimen.SPECIMEN_NAME}" in wanted
    assert f"{site.SITE_DIR}/" not in wanted


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_cli_registers_site() -> None:
    assert COMMANDS["site"] is site.run
    assert not getattr(COMMANDS["site"], "is_stub", False)


def test_run_turns_a_broken_tree_into_a_message_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(upstream, "default_root", lambda: tmp_path)
    assert site.run(argparse.Namespace(quiet=True)) == 1
    assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The copy that will actually be published
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def shipped() -> tuple[site.Content, str]:
    """The checked-in copy, rendered against the checked-in manifest.

    Still hermetic — every input is a file in ``sources/`` — but this is the
    page a visitor gets, so the constraints are worth asserting against it and
    not only against a fixture.
    """
    root = _REPO
    content = site.load_content(site.content_path_for(root))
    rows = allowlist.read_tsv(allowlist.tsv_path_for(root))
    parameters = stars.load_parameters(stars.parameters_path_for(root))
    pins = upstream.load_pins(upstream.pin_file_for(root))
    page = site.render(
        family=FAMILY,
        content=content,
        rows=rows,
        names=NAMES,
        pins=pins,
        italic=parameters.italic,
    )
    return content, page


def test_the_shipped_copy_is_four_sections_one_per_demo(
    shipped: tuple[site.Content, str],
) -> None:
    content, _ = shipped
    assert len(content.sections) == len(site.DEMOS)
    assert sorted(section.demo for section in content.sections) == sorted(site.DEMOS)


def test_the_shipped_copy_is_prose_not_placeholder(
    shipped: tuple[site.Content, str],
) -> None:
    """Real copy about this family — §15.2.2 asks the implementer to write it."""
    content, _ = shipped
    for section in content.sections:
        words = sum(len(paragraph.split()) for paragraph in section.body)
        assert words >= 40, f"{section.key} is too thin to be the real copy"
    lowered = " ".join(
        paragraph.lower() for section in content.sections for paragraph in section.body
    )
    for filler in ("lorem ipsum", "placeholder", "tbd", "todo", "xxx"):
        assert filler not in lowered
    # It has to name what the family is made of.
    for subject in ("literata", "dejavu", "template"):
        assert subject in lowered


def test_the_shipped_page_fetches_nothing_and_points_only_where_it_may(
    shipped: tuple[site.Content, str],
) -> None:
    content, page = shipped
    assert "<script src" not in page
    assert "<link" not in page
    allowed = site.allowed_urls(content)
    unexpected = [
        url
        for url in sorted(set(site.URL_RE.findall(page)))
        if not any(url.startswith(prefix) for prefix in allowed)
    ]
    assert not unexpected, f"unexpected external references: {unexpected}"


def test_the_shipped_page_gives_every_emoji_row_its_selector(
    shipped: tuple[site.Content, str],
) -> None:
    _, page = shipped
    selector = specimen.TEXT_PRESENTATION_SELECTOR
    rows = allowlist.read_tsv(allowlist.tsv_path_for(_REPO))
    for row in rows:
        if row.emoji_presentation != "yes":
            continue
        for index in (m.start() for m in re.finditer(re.escape(row.char), page)):
            assert page[index + 1 : index + 2] == selector, (
                f"{allowlist.format_codepoint(row.codepoint)} is set without U+FE0E"
            )


def test_the_shipped_page_names_no_private_reference(
    shipped: tuple[site.Content, str],
) -> None:
    _, page = shipped
    assert not [text for text in PRIVATE_REFERENCES if text in page]


def test_the_shipped_copy_points_at_this_repository(
    shipped: tuple[site.Content, str],
) -> None:
    content, _ = shipped
    assert content.repo_url == REPO_URL
    assert content.release_url.startswith(REPO_URL)
    assert content.site_url == SITE_URL
