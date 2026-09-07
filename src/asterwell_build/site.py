"""Render ``fonts/site/`` — the showcase page the family is published behind.

This is the page a person lands on: what the family is, what is unusual about
it, what it looks like in prose and in an interface, and a playground where
they can type their own words and drag the weight until it looks like theirs.
The QA specimen — every one of the allowlisted code points in every shipped
style — is a different artifact for a different reader, and is carried along
as a sub-page rather than merged into this one.

Three things shape the module:

*It is generated, not maintained.* The words live in ``sources/site.toml``;
everything the page asserts as fact — the version, the code-point counts by
source, the upstream pins, the italic treatment's angles, the file names in
the ``@font-face`` snippet — is read from the same inputs the fonts were built
from. A claim on this page therefore cannot drift away from what shipped, and
editing the copy never means editing HTML.

*It fetches nothing.* No CDN, no analytics, no third-party font, no icon set:
one HTML file with inline CSS and inline vanilla JavaScript, beside the two
variable WOFF2s the build produced. Every URL the page resolves is relative,
because a GitHub Pages project site lives under a path prefix; the handful of
absolute links it *displays* are checked against :func:`allowed_urls`.

*It degrades.* Without JavaScript the playground still shows its default text
at its default size and weight — the preview is rendered into the HTML with
those values — and every control is a plain form input that simply does not do
anything. Nothing on the page depends on the script having run.

The output tree is what ``upload-pages-artifact`` is handed verbatim::

    fonts/site/index.html            this page
    fonts/site/.nojekyll             so Pages leaves the [opsz,wght] names alone
    fonts/site/webfonts/*.woff2      the two variable fonts, copied
    fonts/site/specimen/index.html   the QA specimen, copied

It is deliberately *not* part of the release zip: the zip is the font family,
the site is how the family is shown.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from asterwell_build import allowlist, assemble, specimen, stars, upstream
from asterwell_build.assemble import Family
from asterwell_build.specimen import Face, escape, font_face_rules

Log = Callable[[str], None]

__all__ = [
    "Content",
    "Examples",
    "Playground",
    "Section",
    "SiteError",
    "allowed_urls",
    "build",
    "load_content",
    "load_content_text",
    "render",
    "run",
]


class SiteError(Exception):
    """The site cannot be rendered from the copy and the build on disk."""


# --------------------------------------------------------------------------- #
# Where everything goes
# --------------------------------------------------------------------------- #

#: Output tree, relative to ``fonts/``.
SITE_DIR = "site"
INDEX_NAME = "index.html"
WEBFONT_DIR = "webfonts"
SPECIMEN_DIR = "specimen"

#: Empty marker that tells GitHub Pages not to run the files through Jekyll.
#: Without it Jekyll refuses to serve ``AsterwellText[opsz,wght].woff2`` — a
#: name with brackets in it — and the page would be set in a fallback serif.
NOJEKYLL_NAME = ".nojekyll"

#: Where the page's ``@font-face`` rules point, relative to ``index.html``.
WEBFONT_PREFIX = f"{WEBFONT_DIR}/"

#: Where the specimen is linked from, relative to ``index.html``.
SPECIMEN_HREF = f"{SPECIMEN_DIR}/"

#: The family name the page uses internally — as in the specimen, deliberately
#: not the real one, so a visitor who already has Asterwell Text installed is
#: still looking at the files this build produced.
CSS_FAMILY = "AsterwellTextSite"


# --------------------------------------------------------------------------- #
# The absolute links the page is allowed to carry (§15.2.4)
# --------------------------------------------------------------------------- #

#: The upstream projects and licenses the page credits. Everything else it
#: references is relative. These are spelled out rather than read from
#: ``upstream.toml`` on purpose: a pin's ``repo`` is where the *source* lives,
#: which is not always where a reader should be sent (DejaVu's project page is
#: not its git host), and the set of hosts this page may name is a decision,
#: not a consequence of a pin bump.
LITERATA_URL = "https://github.com/googlefonts/literata"
TYPETOGETHER_URL = "https://www.type-together.com"
DEJAVU_URL = "https://dejavu-fonts.github.io/"
DEJAVU_LICENSE_URL = "https://dejavu-fonts.github.io/License.html"
OFL_URL = "https://openfontlicense.org"

EXTERNAL_URLS = (
    LITERATA_URL,
    TYPETOGETHER_URL,
    DEJAVU_URL,
    DEJAVU_LICENSE_URL,
    OFL_URL,
)

#: Matches an absolute URL anywhere in the rendered page, whether it is an
#: ``href``, a ``url()`` or plain prose. The tests use it; so should anything
#: else that needs to answer "what does this page point at".
URL_RE = re.compile(r"https?://[^\s\"'<>()]+")


# --------------------------------------------------------------------------- #
# What the built-in demonstration blocks show
# --------------------------------------------------------------------------- #

#: The four demonstration blocks ``site.py`` knows how to draw. A ``[[section]]``
#: names one in its ``demo`` key; an unknown name is refused rather than
#: silently rendered as an empty card.
DEMOS = ("opsz", "steady", "stars", "provenance")

#: A ``[[section]]``'s ``key`` becomes that card's ``id`` in the page, so it has
#: to be spellable as one. (It is *not* required to equal the section's ``demo``:
#: the shipped copy heads the ``opsz`` card "optical", which is what a reader
#: linking to it would type.)
SECTION_KEY_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

#: Hero specimen material. Type, not copy: it belongs beside the demo blocks in
#: code rather than in ``site.toml``, which is the page's *words*.
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz 0123456789"
HERO_SENTENCE = "Handgloves, a footnote ✽, and the long afternoon of a first draft."

#: The ``opsz`` card: one sentence, three settings.
OPSZ_SENTENCE = "The drawing changes with the size, not just the scale."
OPSZ_SMALL = 12
OPSZ_LARGE = 34

#: The ``steady`` card: the whole weight range with four symbols riding along.
RAMP_WEIGHTS = (200, 300, 400, 500, 600, 700, 800, 900)
RAMP_TEXT = "Handgloves & ornament"
RAMP_SYMBOLS = "✽ ⚑ ✎ ✓"

#: The ``stars`` card. The first row is the four marks beside the asterisk whose
#: advance ⁎ ⁑ ⁂ inherit; the second is the five full-size siblings the section's
#: prose names, so the claim and the drawing are on the page together.
STAR_ROW = ("✽", "⁎", "⁑", "⁂", "*")
STAR_SIZE = 72
SIBLING_ROW = ("✽", "✻", "✼", "✾", "❃")
SIBLING_SIZE = 48

#: Both star rows are shown twice: the italic is a second drawing of the same
#: template (D21), and a single row would show half the family. The same two
#: faces set the hero sample, and their CSS is the specimen's ``Face.css``.
STAR_FACES: tuple[Face, ...] = (specimen.REGULAR, specimen.ITALIC)

#: The playground's named-weight buttons — the eight instances ``fvar`` names.
WEIGHT_STOPS: tuple[tuple[str, int], ...] = (
    ("ExtraLight", 200),
    ("Light", 300),
    ("Regular", 400),
    ("Medium", 500),
    ("SemiBold", 600),
    ("Bold", 700),
    ("ExtraBold", 800),
    ("Black", 900),
)

#: Range of the playground's size control (§15.2.1, Q7). There is deliberately
#: no separate ``opsz`` control: the preview sets ``font-optical-sizing: auto``,
#: so dragging the size drags the optical size with it, which is the behaviour a
#: visitor should be copying into their own stylesheet.
SIZE_MIN = 12
SIZE_MAX = 96
WEIGHT_MIN = 200
WEIGHT_MAX = 900


# --------------------------------------------------------------------------- #
# Copy — sources/site.toml
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Section:
    """One "what makes it special" card: prose, and the demo drawn under it."""

    key: str
    """Anchor for the card in the page."""

    heading: str
    body: tuple[str, ...]
    demo: str
    """One of :data:`DEMOS`."""


@dataclass(frozen=True)
class Examples:
    """``[examples]`` — the family doing the two jobs it was made for."""

    manuscript: tuple[str, ...]
    margin_note: str
    interface: str


@dataclass(frozen=True)
class Playground:
    """``[playground]`` — where the visitor's own text starts."""

    default_text: str
    default_weight: int
    default_size: int


@dataclass(frozen=True)
class Content:
    """All of ``sources/site.toml``."""

    title: str
    tagline: str
    repo_url: str
    release_url: str
    site_url: str
    sections: tuple[Section, ...]
    examples: Examples
    playground: Playground


def content_path_for(root: Path) -> Path:
    return root / "sources" / "site.toml"


def site_dir_for(root: Path) -> Path:
    return assemble.fonts_dir_for(root) / SITE_DIR


def allowed_urls(content: Content) -> tuple[str, ...]:
    """Every absolute URL the page may point at, as prefixes (§15.2.4).

    Prefixes rather than exact strings because the repository link is also the
    root of the in-repo license links (``…/blob/main/OFL.txt``): one entry
    covers the project, not one entry per file.
    """
    return (content.repo_url, content.release_url, content.site_url, *EXTERNAL_URLS)


def _text(where: str, table: Mapping[str, object], field: str) -> str:
    value = table.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SiteError(f"{where} `{field}` must be a non-empty string")
    return value


def _paragraphs(where: str, table: Mapping[str, object], field: str) -> tuple[str, ...]:
    value = table.get(field)
    if not isinstance(value, list) or not value:
        raise SiteError(f"{where} `{field}` must be a non-empty array of strings")
    for index, paragraph in enumerate(value):
        if not isinstance(paragraph, str) or not paragraph.strip():
            raise SiteError(f"{where} `{field}[{index}]` must be a non-empty string")
    return tuple(value)


def _positive_int(
    where: str, table: Mapping[str, object], field: str, *, low: int, high: int
) -> int:
    value = table.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SiteError(f"{where} `{field}` must be an integer")
    if not low <= value <= high:
        raise SiteError(f"{where} `{field}` must be between {low} and {high}, not {value}")
    return value


def _table(where: str, raw: Mapping[str, object], name: str) -> Mapping[str, object]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise SiteError(f"{where} is missing the `[{name}]` table")
    return section


def load_content(path: Path) -> Content:
    """Parse and validate ``sources/site.toml``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SiteError(f"missing site copy: {path}") from exc
    return load_content_text(text, where=str(path))


def load_content_text(text: str, *, where: str) -> Content:
    """Parse and validate the copy, from a string rather than a file.

    Every string the page sets is required to be there and non-empty, and every
    ``[[section]]`` has to name a demo this module can draw. A page with a blank
    heading or a card with no demonstration under it is a page nobody would
    notice was broken until it was published.

    ``where`` is what messages call the copy; it is the file's path in normal
    use, and a label in the tests, which check the refusals against strings
    rather than by writing a file per case.
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SiteError(f"{where}: not valid TOML: {exc}") from exc

    raw_sections = raw.get("section")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise SiteError(f"{where} needs at least one `[[section]]`")

    sections = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_sections):
        place = f"{where}: [[section]] {index + 1}"
        if not isinstance(entry, dict):
            raise SiteError(f"{place} is not a table")
        key = _text(place, entry, "key")
        # The key becomes an `id`, so it has to be one: lowercase, no spaces.
        if not SECTION_KEY_RE.fullmatch(key):
            raise SiteError(
                f"{place} key {key!r} is not usable as an HTML id — "
                "lowercase letters, digits and hyphens only"
            )
        if key in seen:
            raise SiteError(f"{place} repeats the key {key!r}; keys are the page's anchors")
        seen.add(key)
        demo = _text(place, entry, "demo")
        if demo not in DEMOS:
            raise SiteError(
                f"{place} names an unknown demo {demo!r}; "
                f"site.py draws {', '.join(DEMOS)}"
            )
        sections.append(
            Section(
                key=key,
                heading=_text(place, entry, "heading"),
                body=_paragraphs(place, entry, "body"),
                demo=demo,
            )
        )

    examples_table = _table(where, raw, "examples")
    examples_where = f"{where}: [examples]"
    examples = Examples(
        manuscript=_paragraphs(examples_where, examples_table, "manuscript"),
        margin_note=_text(examples_where, examples_table, "margin_note"),
        interface=_text(examples_where, examples_table, "interface"),
    )

    playground_table = _table(where, raw, "playground")
    playground_where = f"{where}: [playground]"
    playground = Playground(
        default_text=_text(playground_where, playground_table, "default_text"),
        default_weight=_positive_int(
            playground_where, playground_table, "default_weight",
            low=WEIGHT_MIN, high=WEIGHT_MAX,
        ),
        default_size=_positive_int(
            playground_where, playground_table, "default_size",
            low=SIZE_MIN, high=SIZE_MAX,
        ),
    )

    return Content(
        title=_text(where, raw, "title"),
        tagline=_text(where, raw, "tagline"),
        repo_url=_text(where, raw, "repo_url"),
        release_url=_text(where, raw, "release_url"),
        site_url=_text(where, raw, "site_url"),
        sections=tuple(sections),
        examples=examples,
        playground=playground,
    )


# --------------------------------------------------------------------------- #
# Turning copy into page text
# --------------------------------------------------------------------------- #

INLINE_CODE_RE = re.compile(r"`([^`]+)`")


class Presentation:
    """How a string from the copy reaches the page.

    Some of the family's code points have a *default emoji presentation*: on an
    emoji-capable platform the system's colour artwork wins over the font,
    whatever the CSS says. The manifest records which ones, and the fix is to
    append U+FE0E — so this does it, for every string the page sets, rather than
    leaving it to whoever writes the copy to remember. The rule itself is
    :func:`specimen.display_text`'s; only the scope (a string, not a row) is new.
    """

    def __init__(self, rows: Sequence[allowlist.Row]) -> None:
        self._emoji = frozenset(
            row.codepoint for row in rows if specimen.display_text(row) != row.char
        )

    def text(self, value: str) -> str:
        """``value`` with U+FE0E after every emoji-presentation code point."""
        if not self._emoji:
            return value
        out: list[str] = []
        for index, character in enumerate(value):
            out.append(character)
            following = value[index + 1 : index + 2]
            if (
                ord(character) in self._emoji
                and following != specimen.TEXT_PRESENTATION_SELECTOR
            ):
                out.append(specimen.TEXT_PRESENTATION_SELECTOR)
        return "".join(out)

    def html(self, value: str) -> str:
        """``value`` ready to be dropped into the page."""
        return escape(self.text(value))

    def prose(self, value: str) -> str:
        """As :meth:`html`, with `backticks` turned into ``<code>``.

        The copy is about typography and keeps naming CSS declarations; writing
        them as code in the source file is how they read there, and this is what
        makes them read that way on the page too.
        """
        return INLINE_CODE_RE.sub(r"<code>\1</code>", self.html(value))


def _js_string(value: str) -> str:
    """``value`` as a JavaScript string literal, safe inside ``<script>``.

    JSON is a subset of JavaScript's expression syntax, so this is just
    :func:`json.dumps` — except for ``<``, which is escaped so that no copy can
    close the script element early with a literal ``</script>``.
    """
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #


def _stylesheet(names: Mapping[str, str]) -> str:
    faces = font_face_rules(names, prefix=WEBFONT_PREFIX, css_family=CSS_FAMILY)
    # The two styles the page sets things in by name, as the specimen does it:
    # one class per face, carrying that face's weight and slope.
    face_rules = "\n".join(f".{face.key} {{ {face.css} }}" for face in STAR_FACES)
    return f"""{faces}

{face_rules}

/* The specimen's palette, plus the dark half the design doc asks to verify.
   The page follows the reader's system setting; the playground's preview
   panel carries its own light/dark switch, independent of it. */
:root {{
  --ink: #16130f;
  --paper: #fbfaf7;
  --panel: #ffffff;
  --rule: #ddd8ce;
  --muted: #6d675d;
  --accent: #7a3b12;
  --badge-custom: #7a3b12;
  --badge-literata: #1f4d5c;
  --badge-dejavu: #3d3a6b;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --ink: #f2efe8;
    --paper: #14120f;
    --panel: #1c1915;
    --rule: #322d26;
    --muted: #a9a196;
    --accent: #e6a86f;
    --badge-custom: #a8551f;
    --badge-literata: #2c6d80;
    --badge-dejavu: #575397;
  }}
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
@media (prefers-reduced-motion: reduce) {{ html {{ scroll-behavior: auto; }} }}
body {{
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: "{CSS_FAMILY}", Georgia, "Times New Roman", serif;
  font-optical-sizing: auto;
  line-height: 1.55;
}}
.wrap {{ max-width: 60rem; margin: 0 auto; padding: 3rem 1.25rem 5rem; }}
a {{ color: inherit; text-decoration: none; border-bottom: 1px solid var(--accent); }}
a:hover {{ color: var(--accent); }}
.label {{ font: 600 .7rem/1.4 ui-monospace, "SFMono-Regular", Menlo, monospace;
          letter-spacing: .06em; text-transform: uppercase; color: var(--muted);
          display: block; margin: 0 0 .4rem; }}
code, pre {{ font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; }}
code {{ font-size: .86em; color: var(--accent); }}
pre {{ font-size: .78rem; line-height: 1.6; overflow-x: auto; margin: 0;
       padding: 1rem 1.1rem; background: var(--panel); color: var(--ink);
       border: 1px solid var(--rule); border-radius: 4px; }}

/* Hero */
header {{ border-bottom: 1px solid var(--rule); padding-bottom: 2.5rem; }}
h1 {{ font-size: clamp(3rem, 11vw, 6.5rem); font-weight: 700; line-height: 1;
      margin: 0 0 .5rem; letter-spacing: -.01em; }}
.tagline {{ font-size: clamp(1.15rem, 3vw, 1.6rem); font-style: italic;
            color: var(--muted); margin: 0 0 1.5rem; max-width: 34em; }}
.version {{ margin: 0 0 1.5rem; }}
nav {{ display: flex; flex-wrap: wrap; gap: 1.5rem; margin: 0 0 2.5rem; }}
nav a {{ font: 600 .8rem/1 ui-monospace, Menlo, monospace; letter-spacing: .06em;
         text-transform: uppercase; padding-bottom: .3rem; }}
.hero-sample {{ margin: 0; }}
.hero-sample p {{ margin: 0 0 .75rem; overflow-x: auto; white-space: nowrap; }}
.alphabet {{ font-size: clamp(1.1rem, 3.4vw, 2rem); }}
.hero-line {{ font-size: clamp(1rem, 2.6vw, 1.5rem); }}

/* Cards */
section {{ margin: 4rem 0 0; scroll-margin-top: 1.5rem; }}
h2 {{ font-size: 1.9rem; margin: 0 0 .5rem; }}
h3 {{ font-size: 1.4rem; margin: 0 0 .5rem; }}
.lede {{ color: var(--muted); max-width: 42em; margin: 0 0 1.5rem; }}
.card {{ border-top: 1px solid var(--rule); padding: 2rem 0 0; margin: 2.5rem 0 0; }}
.card p {{ max-width: 40em; }}
.demo {{ margin: 1.5rem 0 0; padding: 1.5rem; border: 1px solid var(--rule);
         border-radius: 4px; background: var(--panel); overflow-x: auto; }}
.caption {{ color: var(--muted); font-size: .9rem; margin: 1rem 0 0; max-width: 40em; }}

/* opsz demo */
.opsz-row {{ margin: 0 0 1.5rem; }}
.opsz-row:last-of-type {{ margin-bottom: 0; }}
.opsz-line {{ margin: 0; font-optical-sizing: auto; }}
.opsz-line.pinned {{ font-optical-sizing: none; font-variation-settings: "opsz" 12; }}

/* steady demo */
.ramp {{ list-style: none; padding: 0; margin: 0; }}
.ramp li {{ display: flex; gap: 1rem; align-items: baseline; padding: .3rem 0;
            border-bottom: 1px dotted var(--rule); }}
.ramp li:last-child {{ border-bottom: 0; }}
.ramp .w {{ min-width: 3.5rem; margin: 0; }}
.ramp .line {{ font-size: 1.5rem; white-space: nowrap; }}

/* stars demo */
.starrow {{ display: flex; gap: 1.5rem; align-items: baseline; flex-wrap: nowrap;
            margin: 0 0 1rem; }}
.starrow .label {{ min-width: 5rem; margin: 0; }}

/* provenance demo */
.badges {{ list-style: none; padding: 0; margin: 0 0 1.25rem; display: flex;
           flex-wrap: wrap; gap: 1.25rem; }}
.badges li {{ display: flex; align-items: baseline; gap: .5rem; }}
.badges b {{ font-size: 1.4rem; }}
.badge {{ font: 600 .6rem/1 ui-monospace, Menlo, monospace; letter-spacing: .05em;
          text-transform: uppercase; color: #fff; border-radius: 2px;
          padding: .22rem .34rem; }}
.badge.custom {{ background: var(--badge-custom); }}
.badge.literata {{ background: var(--badge-literata); }}
.badge.dejavu {{ background: var(--badge-dejavu); }}

/* Examples */
.panel {{ padding: 1.75rem; border: 1px solid var(--rule); border-radius: 4px;
          margin: 0 0 1.5rem; background: var(--panel); }}
.panel.dark {{ background: #14120f; color: #f2efe8; border-color: #322d26; }}
.panel.dark .label {{ color: #a9a196; }}
.manuscript p {{ font-size: 17px; line-height: 1.8; max-width: 38em; margin: 0 0 1.1rem; }}
.margin-note {{ font-style: italic; color: var(--muted); border-left: 2px solid var(--rule);
                padding-left: 1rem; }}
.ui-line {{ font-size: 14px; white-space: nowrap; overflow-x: auto; margin: 0; }}

/* Playground */
.pg-field {{ width: 100%; font: inherit; font-size: 1rem; padding: .75rem .9rem;
             background: var(--panel); color: var(--ink); border: 1px solid var(--rule);
             border-radius: 4px; resize: vertical; }}
.controls {{ border: 1px solid var(--rule); border-radius: 4px; margin: 1.25rem 0;
             padding: 1.25rem; display: grid; gap: 1.25rem;
             grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); }}
.controls legend {{ font: 600 .7rem/1 ui-monospace, Menlo, monospace;
                    letter-spacing: .06em; text-transform: uppercase;
                    color: var(--muted); padding: 0 .4rem; }}
.control {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
            font-size: .85rem; }}
.control input[type="range"] {{ width: 100%; accent-color: var(--accent); }}
.control output {{ font: 600 .8rem/1 ui-monospace, Menlo, monospace; color: var(--accent); }}
.stops {{ grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: .4rem; }}
.stops .label {{ flex-basis: 100%; }}
.stop {{ font: 600 .72rem/1 ui-monospace, Menlo, monospace; letter-spacing: .04em;
         padding: .45rem .6rem; border: 1px solid var(--rule); border-radius: 3px;
         background: var(--panel); color: var(--ink); cursor: pointer; }}
.stop:hover {{ border-color: var(--accent); }}
.stop[aria-pressed="true"] {{ background: var(--accent); border-color: var(--accent);
                              color: #fff; }}
.switch {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
.switch label {{ display: flex; align-items: center; gap: .35rem; cursor: pointer; }}
.preview {{ min-height: 8rem; padding: 1.75rem; border: 1px solid var(--rule);
            border-radius: 4px; background: #fbfaf7; color: #16130f;
            font-optical-sizing: auto; overflow-wrap: break-word;
            white-space: pre-wrap; margin: 0 0 1.25rem; }}
.preview.dark {{ background: #14120f; color: #f2efe8; border-color: #322d26; }}

/* Footer */
footer {{ margin: 5rem 0 0; padding: 2rem 0 0; border-top: 1px solid var(--rule);
          color: var(--muted); font-size: .85rem; }}
footer p {{ margin: 0 0 .5rem; max-width: 44em; }}
"""


# --------------------------------------------------------------------------- #
# Page sections
# --------------------------------------------------------------------------- #


def _hero(content: Content, family: Family, show: Presentation) -> str:
    faces = "".join(
        f'<p class="hero-line {face.key}">'
        f"{show.html(HERO_SENTENCE)}</p>"
        for face in STAR_FACES
    )
    return f"""<header>
<h1>{show.html(content.title)}</h1>
<p class="tagline">{show.html(content.tagline)}</p>
<p class="version label">Version {escape(family.version)} · SIL Open Font License 1.1</p>
<nav>
<a href="{escape(content.release_url)}">Download</a>
<a href="#special">What makes it special</a>
<a href="#examples">Examples</a>
<a href="#playground">Playground</a>
<a href="{SPECIMEN_HREF}">Specimen</a>
<a href="{escape(content.repo_url)}">Source</a>
</nav>
<div class="hero-sample">
<p class="alphabet regular">{show.html(ALPHABET)}</p>
{faces}
</div>
</header>"""


def _opsz_demo(show: Presentation) -> str:
    sentence = show.html(OPSZ_SENTENCE)
    rows = "".join(
        f'<div class="opsz-row"><span class="label">{size}px · '
        f"font-optical-sizing: auto</span>"
        f'<p class="opsz-line" style="font-size:{size}px">{sentence}</p></div>'
        for size in (OPSZ_SMALL, OPSZ_LARGE)
    )
    return f"""<div class="demo">
{rows}
<div class="opsz-row"><span class="label">{OPSZ_LARGE}px · font-variation-settings: "opsz" {OPSZ_SMALL}</span>
<p class="opsz-line pinned" style="font-size:{OPSZ_LARGE}px">{sentence}</p></div>
<p class="caption">The first two lines are the same file at two sizes, each drawn
for the size it is set at. The third is the {OPSZ_LARGE}px line held at the
{OPSZ_SMALL}px drawing: heavier joins, tighter spacing, a design meant to survive
being small and blown up instead. That difference is the axis doing its work.</p>
</div>"""


def _steady_demo(show: Presentation) -> str:
    line = show.html(f"{RAMP_TEXT} {RAMP_SYMBOLS}")
    items = "".join(
        f'<li><span class="label w">{weight}</span>'
        f'<span class="line" style="font-weight:{weight}">{line}</span></li>'
        for weight in RAMP_WEIGHTS
    )
    return f"""<div class="demo">
<ul class="ramp">{items}</ul>
<p class="caption">Eight weights of the one variable font. The words gain weight
from 200 to 900; ✽ ⚑ ✎ ✓ are the same drawing on every line.</p>
</div>"""


def _stars_demo(show: Presentation, italic: stars.ItalicTreatment) -> str:
    def row(characters: Sequence[str], size: int) -> str:
        return "".join(
            f'<div class="starrow"><span class="label">{size}px {face.label}</span>'
            + "".join(
                f'<span class="{face.key}" style="font-size:{size}px">'
                f"{show.html(character)}</span>"
                for character in characters
            )
            + "</div>"
            for face in STAR_FACES
        )

    return f"""<div class="demo">
{row(STAR_ROW, STAR_SIZE)}
{row(SIBLING_ROW, SIBLING_SIZE)}
<p class="caption">Above: ✽ ⁎ ⁑ ⁂ beside Literata's own asterisk, whose advance the
three small marks inherit. Below: the five full-size siblings drawn from the one
template. In the italic the template is turned {italic.rotation:g}°, so the petals
point up-left and up-right rather than one straight up, and the stacked stars of
⁑ and ⁂ lean {italic.stack_slant:g}° — the lean Literata gives its own colon and
asterism. Nothing is sheared.</p>
</div>"""


def _provenance_demo(
    content: Content, rows: Sequence[allowlist.Row], show: Presentation
) -> str:
    summary = allowlist.summarize(rows)
    described = {
        allowlist.SOURCE_CUSTOM: "drawn here",
        allowlist.SOURCE_LITERATA: "preserved from Literata",
        allowlist.SOURCE_DEJAVU: "imported from DejaVu Sans",
    }
    badges = "".join(
        f'<li><span class="badge {escape(source)}">{escape(source)}</span>'
        f"<b>{summary.by_source.get(source, 0)}</b>"
        f"<span>{escape(described[source])}</span></li>"
        for source in allowlist.SOURCES
    )
    return f"""<div class="demo">
<ul class="badges">{badges}</ul>
<p class="caption">{summary.total} code points in
<a href="{escape(content.repo_url)}/blob/main/sources/allowlist.tsv">sources/allowlist.tsv</a>,
{summary.emoji} of them flagged as needing U+FE0E to defeat a platform's colour
emoji. The family is under the
<a href="{OFL_URL}">SIL Open Font License 1.1</a> — shipped as
<a href="{escape(content.repo_url)}/blob/main/OFL.txt">OFL.txt</a> — and the
<a href="{DEJAVU_LICENSE_URL}">DejaVu Fonts License</a> notices travel with it as
<a href="{escape(content.repo_url)}/blob/main/DEJAVU-LICENSE.txt">DEJAVU-LICENSE.txt</a>.</p>
<p class="caption"><a href="{SPECIMEN_HREF}">See every glyph in the full specimen →</a></p>
</div>"""


def _demo(
    section: Section,
    *,
    content: Content,
    rows: Sequence[allowlist.Row],
    show: Presentation,
    italic: stars.ItalicTreatment,
) -> str:
    if section.demo == "opsz":
        return _opsz_demo(show)
    if section.demo == "steady":
        return _steady_demo(show)
    if section.demo == "stars":
        return _stars_demo(show, italic)
    if section.demo == "provenance":
        return _provenance_demo(content, rows, show)
    # Unreachable: load_content refuses anything else before it gets here.
    raise SiteError(f"no demo named {section.demo!r}")


def _special_section(
    content: Content,
    rows: Sequence[allowlist.Row],
    show: Presentation,
    italic: stars.ItalicTreatment,
) -> str:
    cards = "".join(
        f'<article class="card" id="{escape(section.key)}">'
        f"<h3>{show.html(section.heading)}</h3>"
        + "".join(f"<p>{show.prose(paragraph)}</p>" for paragraph in section.body)
        + _demo(section, content=content, rows=rows, show=show, italic=italic)
        + "</article>"
        for section in content.sections
    )
    return f"""<section id="special">
<h2>What makes it special</h2>
<p class="lede">Four things this family does that a serif with a symbol font
bolted onto it does not.</p>
{cards}
</section>"""


def _examples_section(content: Content, show: Presentation) -> str:
    paragraphs = "".join(
        f"<p>{show.prose(paragraph)}</p>" for paragraph in content.examples.manuscript
    )
    interface = show.html(content.examples.interface)
    return f"""<section id="examples">
<h2>Examples</h2>
<p class="lede">The two jobs the family was drawn for: a page of prose, and a
line of interface furniture that has to sit in the same voice.</p>
<div class="panel manuscript">
<span class="label">Manuscript · 17px · Regular</span>
{paragraphs}
<p class="margin-note">{show.prose(content.examples.margin_note)}</p>
</div>
<div class="panel light">
<span class="label">Interface · 14px · light</span>
<p class="ui-line">{interface}</p>
</div>
<div class="panel dark">
<span class="label">Interface · 14px · dark</span>
<p class="ui-line">{interface}</p>
</div>
</section>"""


def _playground_section(content: Content, family: Family, show: Presentation) -> str:
    playground = content.playground
    default_text = show.html(playground.default_text)
    # The state the page opens in, applied without the script: the preview
    # already looks like the defaults, and the snippet already says so.
    preview_style = (
        f"font-size:{playground.default_size}px;"
        f"font-weight:{playground.default_weight};font-style:normal"
    )
    snippet = _css_snippet(
        family, playground.default_size, playground.default_weight, "normal"
    )
    stops = "".join(
        f'<button type="button" class="stop" data-weight="{weight}" '
        f'aria-pressed="{"true" if weight == playground.default_weight else "false"}">'
        f"{escape(label)}</button>"
        for label, weight in WEIGHT_STOPS
    )
    return f"""<section id="playground">
<h2>Playground</h2>
<p class="lede">Type your own words, drag the weight and the size, and switch to
the italic. The CSS underneath is the CSS that produces what you are looking at
— copy it straight out.</p>
<label class="label" for="pg-text">Your text</label>
<textarea id="pg-text" class="pg-field" rows="3" spellcheck="false">{default_text}</textarea>
<fieldset class="controls">
<legend>Controls</legend>
<div class="control">
<label class="label" for="pg-size">Size — <output id="pg-size-out" for="pg-size">{playground.default_size}px</output></label>
<input type="range" id="pg-size" min="{SIZE_MIN}" max="{SIZE_MAX}" step="1" value="{playground.default_size}">
</div>
<div class="control">
<label class="label" for="pg-weight">Weight — <output id="pg-weight-out" for="pg-weight">{playground.default_weight}</output></label>
<input type="range" id="pg-weight" min="{WEIGHT_MIN}" max="{WEIGHT_MAX}" step="1" value="{playground.default_weight}">
</div>
<div class="control switch">
<span class="label">Style</span>
<label><input type="radio" name="pg-style" id="pg-roman" value="normal" checked> Roman</label>
<label><input type="radio" name="pg-style" id="pg-italic" value="italic"> Italic</label>
</div>
<div class="control switch">
<span class="label">Preview panel</span>
<label><input type="checkbox" id="pg-dark"> Dark</label>
</div>
<div class="control stops" id="pg-stops">
<span class="label">Named weights</span>
{stops}
</div>
</fieldset>
<div id="pg-preview" class="preview" style="{preview_style}">{default_text}</div>
<span class="label">The CSS for what you are seeing</span>
<pre id="pg-css">{snippet}</pre>
</section>"""


def _css_snippet(family: Family, size: int, weight: int, style: str) -> str:
    """The declarations the playground reports — and what the script rebuilds."""
    return escape(
        f'font-family: "{family.family}";\n'
        f"font-weight: {weight};\n"
        f"font-style: {style};\n"
        f"font-size: {size}px;\n"
        "font-optical-sizing: auto;"
    )


def _get_section(content: Content, family: Family, names: Mapping[str, str]) -> str:
    rules = "\n\n".join(
        f"@font-face {{\n"
        f'  font-family: "{family.family}";\n'
        f'  src: url("{names[style.key]}") format("woff2");\n'
        f"  font-weight: 200 900;\n"
        f"  font-style: {'italic' if style.key == 'italic' else 'normal'};\n"
        f"  font-display: swap;\n"
        f"}}"
        for style in assemble.STYLES
    )
    body = (
        f"body {{\n"
        f'  font-family: "{family.family}", Georgia, serif;\n'
        f"  font-optical-sizing: auto;\n"
        f"}}"
    )
    return f"""<section id="get">
<h2>Get it</h2>
<p class="lede">Every release carries the two variable fonts, sixteen static
instances, the two web fonts this page is set in, the specimen, the fontbakery
reports and the licences — with checksums for all of it.</p>
<p><a href="{escape(content.release_url)}">Download the latest release</a> ·
<a href="{escape(content.repo_url)}">Build it yourself from source</a> ·
<a href="{SPECIMEN_HREF}">Read the specimen</a></p>
<pre>{escape(rules)}

{escape(body)}</pre>
<p>Two rules, not sixteen: each file is a variable font covering the whole
200–900 range and the 7–72 optical-size axis, so one <code>@font-face</code> per
style is the whole family.</p>
<p><strong>Licensing.</strong> Asterwell Text is licensed under the
<a href="{OFL_URL}">SIL Open Font License, Version 1.1</a>, with
&ldquo;Asterwell&rdquo; as a Reserved Font Name — a modified version has to be
renamed. The glyphs imported from DejaVu Sans carry the Bitstream Vera and Arev
notices, shipped verbatim beside the fonts and repeated in their name table. The
fonts may be bundled with and sold as part of a larger package; no copy of the
font software may be sold on its own.</p>
</section>"""


def _footer(
    content: Content, family: Family, pins: Sequence[upstream.Pin], show: Presentation
) -> str:
    by_key = {pin.key: pin for pin in pins}
    literata = by_key.get("literata")
    dejavu = by_key.get("dejavu")
    literata_text = (
        f'<a href="{LITERATA_URL}">{escape(literata.name)} {escape(literata.version)}</a>'
        f' by <a href="{TYPETOGETHER_URL}">TypeTogether</a>'
        if literata is not None
        else ""
    )
    dejavu_text = (
        f'<a href="{DEJAVU_URL}">{escape(dejavu.name)} {escape(dejavu.version)}</a>'
        if dejavu is not None
        else ""
    )
    return f"""<footer>
<p>{show.html(content.title)} {escape(family.version)} — built from
{literata_text} and {dejavu_text}, both pinned by checksum.</p>
<p><a href="{escape(content.repo_url)}">Source and build</a> ·
<a href="{escape(content.release_url)}">Releases</a> ·
<a href="{SPECIMEN_HREF}">Specimen</a> ·
<a href="{escape(content.site_url)}">{escape(content.site_url)}</a></p>
<p>This page loads nothing from anywhere: the two web fonts beside it are the
fonts the build produced, and the styles and the script are in the file.</p>
</footer>"""


# --------------------------------------------------------------------------- #
# The playground's script
# --------------------------------------------------------------------------- #


def _script(content: Content, family: Family) -> str:
    """The playground, in vanilla JavaScript with no dependencies.

    Everything it touches is already in the HTML with the defaults applied, so
    the page is complete before this runs and stays usable if it never does.
    """
    return f"""(function () {{
  "use strict";
  var text = document.getElementById("pg-text");
  var preview = document.getElementById("pg-preview");
  var size = document.getElementById("pg-size");
  var sizeOut = document.getElementById("pg-size-out");
  var weight = document.getElementById("pg-weight");
  var weightOut = document.getElementById("pg-weight-out");
  var stops = document.getElementById("pg-stops");
  var italic = document.getElementById("pg-italic");
  var roman = document.getElementById("pg-roman");
  var dark = document.getElementById("pg-dark");
  var css = document.getElementById("pg-css");
  if (!text || !preview || !size || !sizeOut || !weight || !weightOut ||
      !stops || !italic || !roman || !dark || !css) {{
    return;
  }}

  var FAMILY = {_js_string(family.family)};
  var PLACEHOLDER = {_js_string(content.playground.default_text)};
  var buttons = stops.querySelectorAll("button[data-weight]");

  function paint() {{
    var px = size.value;
    var wght = weight.value;
    var style = italic.checked ? "italic" : "normal";

    preview.textContent = text.value.length ? text.value : PLACEHOLDER;
    preview.style.fontSize = px + "px";
    preview.style.fontWeight = wght;
    preview.style.fontStyle = style;
    if (dark.checked) {{
      preview.classList.add("dark");
    }} else {{
      preview.classList.remove("dark");
    }}

    sizeOut.textContent = px + "px";
    weightOut.textContent = wght;
    for (var i = 0; i < buttons.length; i++) {{
      buttons[i].setAttribute(
        "aria-pressed",
        buttons[i].getAttribute("data-weight") === wght ? "true" : "false"
      );
    }}

    css.textContent =
      'font-family: "' + FAMILY + '";\\n' +
      "font-weight: " + wght + ";\\n" +
      "font-style: " + style + ";\\n" +
      "font-size: " + px + "px;\\n" +
      "font-optical-sizing: auto;";
  }}

  text.addEventListener("input", paint);
  size.addEventListener("input", paint);
  weight.addEventListener("input", paint);
  roman.addEventListener("change", paint);
  italic.addEventListener("change", paint);
  dark.addEventListener("change", paint);
  for (var j = 0; j < buttons.length; j++) {{
    buttons[j].addEventListener("click", function (event) {{
      weight.value = event.currentTarget.getAttribute("data-weight");
      paint();
    }});
  }}

  paint();
}})();"""


# --------------------------------------------------------------------------- #
# The whole page
# --------------------------------------------------------------------------- #


def render(
    *,
    family: Family,
    content: Content,
    rows: Sequence[allowlist.Row],
    names: Mapping[str, str],
    pins: Sequence[upstream.Pin] = (),
    italic: stars.ItalicTreatment,
) -> str:
    """The complete ``fonts/site/index.html``, as one string."""
    show = Presentation(rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{show.html(content.title)} — {show.html(content.tagline)}</title>
<meta name="description" content="{show.html(content.tagline)}">
<style>
{_stylesheet(names)}
</style>
</head>
<body>
<div class="wrap">
{_hero(content, family, show)}
<main>
{_special_section(content, rows, show, italic)}
{_examples_section(content, show)}
{_playground_section(content, family, show)}
{_get_section(content, family, names)}
</main>
{_footer(content, family, pins, show)}
</div>
<script>
{_script(content, family)}
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def _logger(quiet: bool) -> Log:
    def log(message: str) -> None:
        if not quiet:
            print(message)

    return log


def _copy_webfonts(root: Path, names: Mapping[str, str], site: Path) -> list[Path]:
    """The two variable WOFF2s, beside the page that names them."""
    source_dir = assemble.fonts_dir_for(root) / WEBFONT_DIR
    wanted = sorted(names.values())
    missing = [name for name in wanted if not (source_dir / name).is_file()]
    if missing:
        raise SiteError(
            f"{len(missing)} web font(s) the site embeds are missing from "
            f"{source_dir} — run `mise run build`: {', '.join(missing)}"
        )
    target_dir = site / WEBFONT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    # An earlier build's file names would otherwise be published alongside the
    # current ones: the deploy uploads this directory whole.
    for stale in target_dir.iterdir():
        if stale.is_file() and stale.name not in wanted:
            stale.unlink()
    copied = []
    for name in wanted:
        target = target_dir / name
        shutil.copyfile(source_dir / name, target)
        copied.append(target)
    return copied


def _copy_specimen(root: Path, site: Path) -> Path:
    """The QA specimen, as the site's ``specimen/`` sub-page.

    Copied rather than re-rendered, and copied *here* rather than linked out:
    its ``../webfonts/`` rules then resolve to the same two files this page
    embeds, so the sub-page works with nothing else deployed.
    """
    source = specimen.output_path_for(root)
    if not source.is_file():
        raise SiteError(f"{source} is missing — run `mise run specimen`")
    target_dir = site / SPECIMEN_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / specimen.SPECIMEN_NAME
    shutil.copyfile(source, target)
    return target


def build(root: Path | None = None, *, quiet: bool = False) -> Path:
    """Write ``fonts/site/`` and return the path of its ``index.html``."""
    # Deferred, as in `specimen`: `package` reaches back into this package, and
    # the version this build carries is resolved from inside here.
    from asterwell_build import package

    root = root if root is not None else upstream.default_root()
    log = _logger(quiet)

    family = assemble.load_family(
        assemble.family_path_for(root), package.resolve_version(root).version
    )
    content = load_content(content_path_for(root))
    rows = allowlist.read_tsv(allowlist.tsv_path_for(root))
    names = specimen.webfont_names(family)
    pins = upstream.load_pins(upstream.pin_file_for(root))
    # As in the specimen: the star card states the treatment's own angles, so
    # they are read from the file the fonts were drawn from.
    parameters = stars.load_parameters(stars.parameters_path_for(root))

    site = site_dir_for(root)
    site.mkdir(parents=True, exist_ok=True)
    webfonts = _copy_webfonts(root, names, site)
    page = _copy_specimen(root, site)
    (site / NOJEKYLL_NAME).write_bytes(b"")

    index = site / INDEX_NAME
    index.write_text(
        render(
            family=family,
            content=content,
            rows=rows,
            names=names,
            pins=pins,
            italic=parameters.italic,
        ),
        encoding="utf-8",
    )

    log(f"site: {site}")
    log(f"  {INDEX_NAME:<34} {index.stat().st_size:,} B")
    for font in webfonts:
        log(f"  {WEBFONT_DIR}/{font.name:<26} {font.stat().st_size:,} B")
    log(f"  {SPECIMEN_DIR}/{page.name:<26} {page.stat().st_size:,} B")
    log(f"  {NOJEKYLL_NAME:<34} 0 B")
    return index


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``site``'s own options (called from :mod:`asterwell_build.cli`)."""
    parser.add_argument("--quiet", action="store_true", help="do not list what was written")


def run(args: argparse.Namespace) -> int:
    """``asterwell-build site`` — see :func:`build`."""
    # Deferred: see `build`. A malformed ASTERWELL_VERSION is a message here too.
    from asterwell_build.package import PackageError

    try:
        build(quiet=getattr(args, "quiet", False))
    except (
        SiteError,
        PackageError,
        assemble.AssembleError,
        allowlist.AllowlistError,
        stars.StarsError,
        upstream.UpstreamError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
