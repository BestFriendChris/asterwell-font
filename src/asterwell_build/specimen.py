"""Render ``fonts/specimen/index.html`` — the whole family, on one page.

The specimen is the review artifact the design work is judged against and the
only place the promise "every code point, in every shipped style" is visible
rather than asserted. It shows five things:

*Prose*, from ``qa/specimen-text.txt``, at three reading sizes in Regular,
Italic and Bold, on a light panel and a dark one — the sample carries curly
quotes, an apostrophe, an em dash, an ellipsis and a star used as a footnote
marker, so the punctuation this family is for is on the page and not only in
the character grid.

*The required inventory* — the 43 characters the family must cover — at four
sizes across all four RIBBI styles, which is where a symbol that is right at
24 px and illegible at 12 px gives itself away.

*The stars*, ✽ ✻ ✼ ✾ ❃ ⁎ ⁑ ⁂, beside the asterisk and the diamond they have to
live with, at three sizes and in both styles — a Regular row and an Italic row
at each, because the italic turns the template and leans the stacks (D21) and
the only way to judge that is to see the two rows together.

*A weight ramp* from 200 to 900 with a symbol inline: the symbols carry no
``gvar`` deltas (D5), so the ramp is where "the ornament holds still while the
prose thickens" is either true or obviously not.

*The full allowlist*, grouped by Unicode block, each row rendered four times —
Regular, Bold, Italic, Bold Italic — beside its code point and the badge saying
where its outline came from. Rows flagged ``emoji_presentation`` are rendered
with U+FE0E appended, which is exactly what an application has to do to get the
text presentation these glyphs are drawn for.

The page is self-contained apart from two relative ``@font-face`` links into
``../webfonts/``. That is deliberate: those are the paths inside the release
zip, so the specimen that ships works when the zip is unpacked, without a
server and without the repository.
"""

from __future__ import annotations

import argparse
import html
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from asterwell_build import allowlist, assemble, stars, upstream, web
from asterwell_build.assemble import Family

Log = Callable[[str], None]

__all__ = ["SpecimenError", "build", "render", "run"]


class SpecimenError(Exception):
    """The specimen cannot be rendered from what the build left behind."""


# --------------------------------------------------------------------------- #
# What the page shows
# --------------------------------------------------------------------------- #

#: Output, relative to ``fonts/``. The directory is what the release zip carries.
SPECIMEN_DIR = "specimen"
SPECIMEN_NAME = "index.html"

#: The prose sample, relative to the repository root.
TEXT_FILE = Path("qa") / "specimen-text.txt"

#: Where the ``@font-face`` rules point, relative to ``fonts/specimen/``. The
#: web fonts are the two *variable* fonts, so one file covers every weight of a
#: style and the ramp below is a real interpolation rather than eight files.
WEBFONT_PREFIX = "../webfonts/"

#: The family name the page uses internally. Deliberately not "Asterwell Text":
#: a reviewer who already has the family installed must still be looking at the
#: file in ``fonts/webfonts/``, not at whatever their system resolved.
CSS_FAMILY = "AsterwellTextSpecimen"


@dataclass(frozen=True)
class Face:
    """One of the four styles a RIBBI-addressable host can reach."""

    key: str
    label: str
    weight: int
    italic: bool

    @property
    def css(self) -> str:
        style = "italic" if self.italic else "normal"
        return f"font-weight:{self.weight};font-style:{style}"


REGULAR = Face("regular", "Regular", 400, False)
BOLD = Face("bold", "Bold", 700, False)
ITALIC = Face("italic", "Italic", 400, True)
BOLD_ITALIC = Face("bolditalic", "Bold Italic", 700, True)

#: The four styles the allowlist grid and the inventory are shown in (§9.8).
RIBBI = (REGULAR, BOLD, ITALIC, BOLD_ITALIC)

#: Prose is shown in three of them, in reading order of interest.
PROSE_FACES = (REGULAR, ITALIC, BOLD)

PROSE_SIZES = (17, 19, 34)
INVENTORY_SIZES = (12, 14, 17, 24)
STAR_SIZES = (17, 34, 72)

#: The eight ornaments — the five full-size drawings of the one template, then
#: the three built from the small star — and the two characters they have to sit
#: beside: Literata's own asterisk (which ⁎ ⁑ ⁂ borrow their advance from) and
#: its diamond (the heaviest symbol the family already had).
STAR_ROW = ("✽", "✻", "✼", "✾", "❃", "⁎", "⁑", "⁂", "*", "◆")

#: The star row is shown in both styles: the italic's stars are turned and its
#: stacks lean (D21), so a single row would hide half the family.
STAR_FACES = (REGULAR, ITALIC)

#: Weights the ramp walks, and the symbol carried along to prove it holds still.
RAMP_WEIGHTS = (200, 300, 400, 500, 600, 700, 800, 900)
RAMP_SYMBOL = "✽"
RAMP_TEXT = "Handgloves & asterisks"

#: Variation selector that asks for the text presentation of a character
#: platforms would otherwise render as colour emoji.
TEXT_PRESENTATION_SELECTOR = "︎"


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


def text_path_for(root: Path) -> Path:
    return root / TEXT_FILE


def output_path_for(root: Path) -> Path:
    return assemble.fonts_dir_for(root) / SPECIMEN_DIR / SPECIMEN_NAME


def load_paragraphs(path: Path) -> list[str]:
    """The prose sample, as paragraphs with their line wrapping undone.

    The file is wrapped for review in a text editor; a browser does its own
    wrapping, so the newlines inside a paragraph are collapsed and a blank line
    is what separates one paragraph from the next.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SpecimenError(f"missing prose sample: {path}") from exc
    paragraphs = [" ".join(block.split()) for block in raw.split("\n\n")]
    kept = [paragraph for paragraph in paragraphs if paragraph]
    if not kept:
        raise SpecimenError(f"{path} has no prose in it")
    return kept


def webfont_names(family: Family) -> dict[str, str]:
    """Style key → the WOFF2 filename the page links to."""
    return {
        style.key: web.output_name(Path(assemble.output_name(family, style)))
        for style in assemble.STYLES
    }


def display_text(row: allowlist.Row) -> str:
    """The character as the page should render it.

    A code point with ``Emoji_Presentation=Yes`` is followed by U+FE0E, because
    without it a platform's emoji font wins and the reviewer would be looking at
    somebody else's artwork instead of this family's glyph.
    """
    if row.emoji_presentation == "yes":
        return row.char + TEXT_PRESENTATION_SELECTOR
    return row.char


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _face_span(face: Face, text: str, extra: str = "") -> str:
    classes = f"g {face.key}" + (f" {extra}" if extra else "")
    return f'<span class="{classes}">{_escape(text)}</span>'


def _stylesheet(names: Mapping[str, str]) -> str:
    faces = "\n".join(
        f"""@font-face {{
  font-family: "{CSS_FAMILY}";
  src: url("{WEBFONT_PREFIX}{quote(names[style.key])}") format("woff2");
  font-weight: 200 900;
  font-style: {"italic" if style.key == "italic" else "normal"};
  font-display: block;
}}"""
        for style in assemble.STYLES
    )
    ribbi_rules = "\n".join(
        f".{face.key} {{ {face.css} }}" for face in RIBBI
    )
    return f"""{faces}

:root {{
  --ink: #16130f;
  --paper: #fbfaf7;
  --rule: #ddd8ce;
  --muted: #6d675d;
  --badge-custom: #7a3b12;
  --badge-literata: #1f4d5c;
  --badge-dejavu: #3d3a6b;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: "{CSS_FAMILY}", Georgia, "Times New Roman", serif;
  font-optical-sizing: auto;
  line-height: 1.5;
}}
.wrap {{ max-width: 62rem; margin: 0 auto; padding: 2rem 1.25rem 6rem; }}
header h1 {{ font-size: 2.6rem; margin: 0 0 .25rem; font-weight: 700; }}
header p {{ margin: .25rem 0; color: var(--muted); }}
nav {{ margin: 1.5rem 0 0; border-top: 1px solid var(--rule);
       border-bottom: 1px solid var(--rule); padding: .6rem 0; }}
nav a {{ color: inherit; margin-right: 1.25rem; text-decoration: none;
         border-bottom: 1px solid var(--rule); }}
section {{ margin: 3rem 0 0; }}
h2 {{ font-size: 1.5rem; margin: 0 0 .25rem; }}
h2 + p.lede {{ margin: 0 0 1.5rem; color: var(--muted); max-width: 46rem; }}
h3 {{ font-size: 1rem; font-weight: 600; margin: 2rem 0 .5rem;
      color: var(--muted); }}
.label {{ font: 600 .72rem/1.4 ui-monospace, "SFMono-Regular", Menlo, monospace;
          letter-spacing: .04em; text-transform: uppercase; color: var(--muted); }}
.panel {{ padding: 1.5rem; border: 1px solid var(--rule); border-radius: 4px;
          margin: 1rem 0; overflow-x: auto; }}
.panel.dark {{ background: #16130f; color: #f4f1ea; border-color: #2f2a24; }}
.panel.dark .label {{ color: #a9a196; }}
.sample p {{ margin: 0 0 1rem; max-width: 40em; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid var(--rule); padding: .5rem .6rem;
          vertical-align: middle; text-align: left; }}
th {{ font: 600 .72rem/1.4 ui-monospace, Menlo, monospace; letter-spacing: .04em;
      text-transform: uppercase; color: var(--muted); background: #f3f0ea; }}
td.run {{ word-break: break-word; }}
.starrow {{ display: flex; gap: 1.5rem; align-items: baseline;
            flex-wrap: wrap; margin: .5rem 0 1.25rem; }}
.ramp li {{ list-style: none; display: flex; gap: 1rem; align-items: baseline;
            border-bottom: 1px dotted var(--rule); padding: .35rem 0; }}
.ramp {{ padding: 0; margin: 0; }}
.ramp .w {{ min-width: 3.5rem; }}
.ramp .line {{ font-size: 1.6rem; }}
.grid {{ display: grid; gap: .5rem; padding: 0; margin: 0 0 1.5rem;
         grid-template-columns: repeat(auto-fill, minmax(11.5rem, 1fr)); }}
.grid li {{ list-style: none; border: 1px solid var(--rule); border-radius: 3px;
            padding: .5rem .55rem; background: #fff; }}
.glyphs {{ display: flex; gap: .35rem; align-items: baseline;
           font-size: 1.5rem; line-height: 1.3; }}
.glyphs .g {{ flex: 1 1 0; text-align: center; }}
.meta {{ display: flex; justify-content: space-between; align-items: center;
         gap: .4rem; margin-top: .35rem; }}
code {{ font: .72rem/1.4 ui-monospace, Menlo, monospace; color: var(--muted); }}
.uname {{ font-size: .62rem; line-height: 1.25; color: var(--muted);
          margin-top: .2rem; word-break: break-word; }}
.badge {{ font: 600 .6rem/1 ui-monospace, Menlo, monospace; letter-spacing: .05em;
          text-transform: uppercase; color: #fff; border-radius: 2px;
          padding: .18rem .3rem; }}
.badge.custom {{ background: var(--badge-custom); }}
.badge.literata {{ background: var(--badge-literata); }}
.badge.dejavu {{ background: var(--badge-dejavu); }}
.fe0e {{ outline: 1px dotted var(--rule); outline-offset: 1px; }}
{ribbi_rules}
"""


def _header(family: Family, rows: Sequence[allowlist.Row], names: Mapping[str, str]) -> str:
    summary = allowlist.summarize(rows)
    counts = ", ".join(
        f"{summary.by_source.get(source, 0)} {source}" for source in allowlist.SOURCES
    )
    links = " · ".join(
        f'<a href="{WEBFONT_PREFIX}{quote(name)}">{_escape(name)}</a>'
        for name in names.values()
    )
    return f"""<header>
<h1>{_escape(family.family)}</h1>
<p class="label">Specimen · version {_escape(family.version)} · {summary.total} code points ({_escape(counts)})</p>
<p>{summary.emoji} of those rows are emoji-presentation by default and are shown here
with U+FE0E appended, which is what an application has to do to get the text
presentation these glyphs are drawn for.</p>
<p class="label">Rendered from {links}</p>
<nav>
<a href="#prose">Prose</a>
<a href="#inventory">Inventory</a>
<a href="#stars">Stars</a>
<a href="#ramp">Weight ramp</a>
<a href="#allowlist">Allowlist</a>
</nav>
</header>"""


def _prose_section(paragraphs: Sequence[str]) -> str:
    panels = []
    for panel, panel_label in (("light", "Light"), ("dark", "Dark")):
        blocks = []
        for face in PROSE_FACES:
            for size in PROSE_SIZES:
                body = "".join(
                    f'<p style="font-size:{size}px">{_escape(paragraph)}</p>'
                    for paragraph in paragraphs
                )
                blocks.append(
                    f'<div class="sample {face.key}">'
                    f'<p class="label">{_escape(face.label)} · {size}px</p>'
                    f"{body}</div>"
                )
        panels.append(
            f'<div class="panel {panel}"><p class="label">{panel_label} panel</p>'
            + "".join(blocks)
            + "</div>"
        )
    return f"""<section id="prose">
<h2>Prose</h2>
<p class="lede">The sample carries curly quotes, an apostrophe, an em dash, an
ellipsis and a star used as a footnote marker, so the punctuation the family is
for is set rather than described. Source: <code>qa/specimen-text.txt</code>.</p>
{"".join(panels)}
</section>"""


def _inventory_section(inventory: str) -> str:
    header = "".join(f"<th>{_escape(face.label)}</th>" for face in RIBBI)
    body = "".join(
        "<tr><th>" + f"{size}px</th>"
        + "".join(
            f'<td class="run {face.key}" style="font-size:{size}px">'
            f"{_escape(inventory)}</td>"
            for face in RIBBI
        )
        + "</tr>"
        for size in INVENTORY_SIZES
    )
    return f"""<section id="inventory">
<h2>Required inventory</h2>
<p class="lede">The {len(inventory)} characters the family must cover in every
shipped style, at the sizes a user interface actually uses them at.</p>
<table><thead><tr><th>Size</th>{header}</tr></thead><tbody>{body}</tbody></table>
</section>"""


def _stars_section(italic: stars.ItalicTreatment) -> str:
    """The family at three sizes, in both styles.

    Two rows per size, not one: the italic is a different drawing of the same
    template (D21), and the point of this section is to be able to see that —
    the turn in ✽ ✻ ✼ ✾ ❃, the lean in ⁑ and ⁂ — beside the roman it has to set
    with. The angles are read from ``sources/stars.toml`` rather than typed out,
    so retuning the treatment retunes this paragraph with it.
    """
    rows = "".join(
        f'<div class="starrow"><span class="label">{size}px {face.label}</span>'
        + "".join(
            f'<span class="{face.key}" style="font-size:{size}px">{_escape(char)}</span>'
            for char in STAR_ROW
        )
        + "</div>"
        for size in STAR_SIZES
        for face in STAR_FACES
    )
    return f"""<section id="stars">
<h2>The star family</h2>
<p class="lede">✽ ✻ ✼ ✾ ❃ — five full-size drawings of one parametric template —
and ⁎ ⁑ ⁂, built from one small outline used up to three times, beside
Literata's own asterisk, whose advance ⁎ ⁑ ⁂ inherit, and its diamond.
In the italic the template is turned {italic.rotation:g}°, so the petals point
up-left and up-right rather than one straight up, and the stacked stars of ⁑ and
⁂ lean {italic.stack_slant:g}° with the italic the way its colon and its own
asterism do; nothing is sheared.</p>
{rows}
</section>"""


def _ramp_section() -> str:
    items = "".join(
        f'<li><span class="label w">{weight}</span>'
        f'<span class="line" style="font-weight:{weight}">'
        f"{_escape(RAMP_TEXT)} {_escape(RAMP_SYMBOL)}</span></li>"
        for weight in RAMP_WEIGHTS
    )
    return f"""<section id="ramp">
<h2>Weight ramp</h2>
<p class="lede">200 to 900 on the roman variable font, with {_escape(RAMP_SYMBOL)}
inline. The prose thickens; the ornament does not — the imported and original
symbols carry no <code>gvar</code> deltas by design.</p>
<ul class="ramp">{items}</ul>
</section>"""


def _row_cell(row: allowlist.Row) -> str:
    text = display_text(row)
    extra = "fe0e" if row.emoji_presentation == "yes" else ""
    glyphs = "".join(_face_span(face, text, extra) for face in RIBBI)
    return (
        f'<li data-source="{_escape(row.source)}">'
        f'<div class="glyphs">{glyphs}</div>'
        f'<div class="meta"><code>{allowlist.format_codepoint(row.codepoint)}</code>'
        f'<span class="badge {_escape(row.source)}">{_escape(row.source)}</span></div>'
        f'<div class="uname">{_escape(row.unicode_name)}</div>'
        "</li>"
    )


def _allowlist_section(rows: Sequence[allowlist.Row]) -> str:
    blocks: dict[str, list[allowlist.Row]] = {}
    for row in rows:
        blocks.setdefault(row.block, []).append(row)
    parts = []
    for block in sorted(blocks):
        entries = blocks[block]
        parts.append(
            f"<h3>{_escape(block)} · {len(entries)} code points</h3>"
            f'<ul class="grid">' + "".join(_row_cell(row) for row in entries) + "</ul>"
        )
    labels = " / ".join(face.label for face in RIBBI)
    return f"""<section id="allowlist">
<h2>The whole allowlist</h2>
<p class="lede">Every one of the {len(rows)} code points
<code>sources/allowlist.tsv</code> promises, grouped by Unicode block and drawn
four times in each cell: {_escape(labels)}. The badge says where the outline
came from. A dotted outline marks a row rendered with U+FE0E.</p>
{"".join(parts)}
</section>"""


def render(
    *,
    family: Family,
    rows: Sequence[allowlist.Row],
    paragraphs: Sequence[str],
    names: Mapping[str, str],
    italic: stars.ItalicTreatment,
    inventory: str = allowlist.REQUIRED_INVENTORY,
) -> str:
    """The complete page, as one string."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(family.family)} {_escape(family.version)} — specimen</title>
<style>
{_stylesheet(names)}
</style>
</head>
<body>
<div class="wrap">
{_header(family, rows, names)}
{_prose_section(paragraphs)}
{_inventory_section(inventory)}
{_stars_section(italic)}
{_ramp_section()}
{_allowlist_section(rows)}
</div>
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


def _require_webfonts(root: Path, names: Iterable[str]) -> None:
    """The page is only useful if the files it links to are there."""
    webfonts = assemble.fonts_dir_for(root) / "webfonts"
    missing = [name for name in names if not (webfonts / name).is_file()]
    if missing:
        raise SpecimenError(
            f"{len(missing)} web font(s) the specimen links to are missing from "
            f"{webfonts} — run `mise run build`: {', '.join(missing)}"
        )


def build(root: Path | None = None, *, quiet: bool = False) -> Path:
    """Write ``fonts/specimen/index.html`` and return its path."""
    # Deferred: `package` imports this module for the specimen's path in the
    # release zip, so the version it resolves is reached from inside here.
    from asterwell_build import package

    root = root if root is not None else upstream.default_root()
    log = _logger(quiet)
    family = assemble.load_family(
        assemble.family_path_for(root), package.resolve_version(root).version
    )
    rows = allowlist.read_tsv(allowlist.tsv_path_for(root))
    paragraphs = load_paragraphs(text_path_for(root))
    names = webfont_names(family)
    _require_webfonts(root, names.values())
    # The star section states the italic treatment's own numbers, so it is read
    # from the same file the fonts were drawn from rather than restated here.
    parameters = stars.load_parameters(stars.parameters_path_for(root))

    output = output_path_for(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render(
            family=family,
            rows=rows,
            paragraphs=paragraphs,
            names=names,
            italic=parameters.italic,
        ),
        encoding="utf-8",
    )
    log(
        f"specimen: {output} ({len(rows)} code points × {len(RIBBI)} styles, "
        f"{output.stat().st_size:,} B)"
    )
    return output


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``specimen``'s own options (called from :mod:`asterwell_build.cli`)."""
    parser.add_argument("--quiet", action="store_true", help="do not print the output path")


def run(args: argparse.Namespace) -> int:
    """``asterwell-build specimen`` — see :func:`build`."""
    # Deferred: see `build`. A malformed ASTERWELL_VERSION is a message here too.
    from asterwell_build.package import PackageError

    try:
        build(quiet=getattr(args, "quiet", False))
    except (
        SpecimenError,
        PackageError,
        assemble.AssembleError,
        allowlist.AllowlistError,
        upstream.UpstreamError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
