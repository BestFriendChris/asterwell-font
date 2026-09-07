"""Compress the variable fonts for the web (§7.6).

Two files, one per style, and nothing else: the WOFF2 container holds the same
variable font a native host gets, so a page can name one ``@font-face`` with
``font-weight: 200 900`` and ``font-optical-sizing: auto`` and have the whole
design space.

Deliberately **not** subsetted and **not** feature-pruned. The subsetting
decision was already made upstream of here — ``sources/allowlist.tsv`` is the
subset, and it is reviewed as a diff rather than re-derived per output — and a
`--layout-features` prune would quietly cost the family its small caps, its
fractions and its two stylistic sets. WOFF2's own glyf transform plus brotli is
where the size goes (955 KB → 397 KB in the prototype, §14.4), and it is
lossless.

Static WOFF2s are not built: nothing consumes them (§6). Adding them later is
one more loop over :func:`compress_font`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fontTools.ttLib import TTFont, woff2

from asterwell_build import assemble
from asterwell_build.assemble import Log, StyleReport

__all__ = [
    "WebError",
    "WebfontReport",
    "build_webfonts",
    "compress_font",
    "output_name",
]


class WebError(assemble.AssembleError):
    """A built font cannot be turned into the web font the family ships."""


#: The one web format the family ships. WOFF 1.0 is not built: every browser
#: that can use a variable font can read WOFF2.
WEBFONT_SUFFIX = ".woff2"


@dataclass(frozen=True)
class WebfontReport:
    """One compression, with the numbers worth printing."""

    source: Path
    output: Path
    source_size: int
    output_size: int

    @property
    def ratio(self) -> float:
        """Compressed size as a fraction of the original."""
        return self.output_size / self.source_size if self.source_size else 0.0


def output_name(source: Path) -> str:
    """``AsterwellText[opsz,wght].ttf`` → ``AsterwellText[opsz,wght].woff2``.

    The stem is kept exactly, brackets and all: the web font and the TTF are the
    same font, and a reader comparing a release's files should not have to
    work out which is which.
    """
    return source.stem + WEBFONT_SUFFIX


def compress_font(source: Path, output: Path) -> WebfontReport:
    """Compress one font into WOFF2, verifying the result reopens.

    The round-trip is not ceremony: WOFF2 rewrites ``glyf`` and ``loca`` through
    a lossy-looking transform that must reconstruct exactly, and a container
    that cannot be reopened is a file no browser will use.
    """
    if not source.is_file():
        raise WebError(f"{source} is missing — run `mise run build`")
    output.parent.mkdir(parents=True, exist_ok=True)
    woff2.compress(str(source), str(output))

    reopened = _reopen(output)
    try:
        if "fvar" not in reopened:
            raise WebError(f"{output.name}: the compressed font lost its fvar table")
    finally:
        reopened.close()
    return WebfontReport(
        source=source,
        output=output,
        source_size=source.stat().st_size,
        output_size=output.stat().st_size,
    )


def _reopen(path: Path) -> TTFont:
    """Open a written WOFF2, turning a container fontTools cannot read back
    into this module's own error rather than a traceback from deep inside it."""
    try:
        return TTFont(path, recalcTimestamp=False)
    except Exception as exc:  # noqa: BLE001 - any failure here means the same thing
        raise WebError(f"{path.name}: the compressed font does not reopen: {exc}") from exc


def build_webfonts(
    variable: Sequence[StyleReport], *, root: Path, log: Log = print
) -> list[WebfontReport]:
    """The web stage of ``asterwell-build build``: one WOFF2 per variable font."""
    output_dir = assemble.fonts_dir_for(root) / "webfonts"
    reports = [
        compress_font(report.output, output_dir / output_name(report.output))
        for report in variable
    ]
    log(f"webfonts: {len(reports)}")
    for report in reports:
        log(
            f"  {report.output.name:<34} "
            f"{report.source_size:,} → {report.output_size:,} B "
            f"({report.ratio:.0%})"
        )
    return reports
