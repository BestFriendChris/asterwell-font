# Asterwell Text

Asterwell Text is a serif family for reading and for interfaces: the
Literata prose face, extended with a curated set of symbols, arrows,
geometric shapes and ornaments that Literata does not cover, plus an
original six-petal star family. The symbols are scaled to the prose
face's cap height and stay upright and steady at every weight and
optical size, so a marker or a keyboard glyph set in running text reads
as part of the same voice rather than as a pasted-in icon. This
repository is the build: pinned upstream inputs, a checked-in manifest
of every code point the family promises, and the pipeline that turns
them into fonts.

The family is licensed under the SIL Open Font License, Version 1.1
(`OFL.txt`), with `"Asterwell"` as a Reserved Font Name.

## What's in it

- **The prose face** — Literata 3.103, unchanged in outline, metrics and
  layout: roman and italic variable fonts with an optical-size axis
  (`opsz` 7–72) and a weight axis (`wght` 200–900).
- **Symbols from DejaVu Sans** — dingbats, miscellaneous symbols,
  geometric shapes, arrows and the keyboard glyphs (⌘ ⌫ ⏎ ⇧ ⇥ …),
  imported by an explicit allowlist and scaled to Literata's cap
  height. Nothing is imported that Literata already covers.
- **An original star family** — ✽ (U+273D) at full size, and ⁎ (U+204E),
  ⁑ (U+2051) and ⁂ (U+2042) built from one shared small-star outline, all
  generated from a single parametric template so the four read as one
  set. The asterism replaces Literata's own.

Each build produces two variable fonts, sixteen static instances derived
from them at `opsz=12`, and WOFF2 versions of the two variable fonts.

## Upstream fonts and licenses

| Upstream | Version | Project | License |
| --- | --- | --- | --- |
| Literata | 3.103 | <https://github.com/googlefonts/literata> | SIL Open Font License 1.1 — bundled here as [`OFL.txt`](OFL.txt) |
| DejaVu Sans | 2.37 | <https://dejavu-fonts.github.io/> ([repository](https://github.com/dejavu-fonts/dejavu-fonts)) | DejaVu Fonts License (Bitstream Vera + Arev notices) — <https://dejavu-fonts.github.io/License.html>, bundled verbatim as [`DEJAVU-LICENSE.txt`](DEJAVU-LICENSE.txt) |

The SIL Open Font License itself, with its FAQ, is at
<https://openfontlicense.org>.

Both inputs are pinned by release URL, size and SHA-256 in
[`sources/upstream.toml`](sources/upstream.toml), and every extracted
member is verified against its own checksum before the build touches it.
`FONTLOG.txt` records the full provenance and licensing statement.

## Downloads

Built fonts are not committed to this repository — they are published as
assets on the
[Releases page](https://github.com/BestFriendChris/asterwell-font/releases).
The first release is `v1.000`; until it is tagged, build the fonts
locally with the steps below. `mise run package` produces exactly the
assets a release carries, byte for byte, so a local build is not a
second-best copy of one.

Each release carries:

| Asset | What it is |
| --- | --- |
| `AsterwellText-<version>.zip` | everything: the two variable fonts, the sixteen statics, the two WOFF2s, the specimen, the two fontbakery reports, the license and provenance files, `allowlist.tsv` and `BUILD-INFO.json` |
| `AsterwellText[opsz,wght].ttf`, `AsterwellText-Italic[opsz,wght].ttf` | the two variable fonts, for installing directly |
| `AsterwellText[opsz,wght].woff2`, `AsterwellText-Italic[opsz,wght].woff2` | the same two fonts for the web |
| `SHA256SUMS.txt` | checksums for every shipped file, the zip included |
| `OFL.txt`, `DEJAVU-LICENSE.txt` | the licenses, also inside the zip |

The release notes quote the checksums of the zip and of the four font
assets, so verifying a single download is `sha256sum
AsterwellText-<version>.zip` compared against the notes.

`SHA256SUMS.txt` covers every file, with paths relative to a build's
`fonts/` directory — which is also the layout inside the zip, except that
the licenses and the other documents sit at the zip's root and appear
here as `../` entries:

```sh
cd fonts && sha256sum -c dist/SHA256SUMS.txt   # a local build: every entry
sha256sum -c --ignore-missing SHA256SUMS.txt   # an unpacked zip: the fonts, the
                                               # specimen, the reports and
                                               # BUILD-INFO.json
```

`qa/fontbakery-variable.md` and `qa/fontbakery-static.md` inside the zip
are [fontbakery](https://github.com/fonttools/fontbakery)'s own report on
the exact files shipped beside them — the release's checks, not a claim
about them.

## Using the fonts

On the web, load the two variable WOFF2 files and let the browser pick
the weight and optical size:

```css
@font-face {
  font-family: "Asterwell Text";
  src: url("AsterwellText[opsz,wght].woff2") format("woff2");
  font-weight: 200 900;
  font-style: normal;
  font-display: swap;
}

@font-face {
  font-family: "Asterwell Text";
  src: url("AsterwellText-Italic[opsz,wght].woff2") format("woff2");
  font-weight: 200 900;
  font-style: italic;
  font-display: swap;
}

body {
  font-family: "Asterwell Text", Georgia, serif;
  font-optical-sizing: auto;
}
```

Native applications can install the variable fonts directly; the static
TTFs are there for export paths and tools that still want one file per
weight.

**One thing to know about emoji.** Some of the symbols in this family
have a *default emoji presentation* on emoji-capable platforms — the
system will show its own colour emoji instead of the typographic glyph,
however you style the text. Append `U+FE0E` (VARIATION SELECTOR-15)
after such a character to ask for the text form:

```html
<span class="warn">&#x26A0;&#xFE0E;</span>
```

The rows that need this are flagged `yes` in the `emoji_presentation`
column of `sources/allowlist.tsv` (21 of them today).

## Building

The toolchain is pinned with [mise](https://mise.jdx.dev) (Python and
[uv](https://docs.astral.sh/uv/)); the Python libraries are pinned in
`uv.lock`. Nothing else needs installing.

```sh
mise install         # the pinned Python and uv
mise run fetch       # download + verify the pinned upstream archives
mise run package     # build, check, render the specimen, and package
```

`mise run package` runs the whole chain and leaves:

| Path | Contents |
| --- | --- |
| `fonts/variable/` | the two variable fonts |
| `fonts/ttf/` | the sixteen static instances |
| `fonts/webfonts/` | the two variable fonts as WOFF2 |
| `fonts/specimen/index.html` | a specimen showing every allowlisted code point in every shipped style |
| `fonts/dist/` | the release zip, `SHA256SUMS.txt` and release notes |
| `fonts/BUILD-INFO.json` | tool versions and the checksums of every input and output |

Individual stages are their own tasks — `mise tasks` lists them; `mise
run build`, `mise run qa`, `mise run specimen` and `mise run test` are
the useful ones. Builds are byte-reproducible: given the same pinned
inputs and `SOURCE_DATE_EPOCH`, the outputs hash identically.

`build/` and `fonts/` are ignored by git; the fonts are release
artifacts, not source.

## Coverage and provenance

The family's coverage is a checked-in manifest, not an accident of
whatever the source fonts happened to contain.

- [`sources/allowlist.toml`](sources/allowlist.toml) holds the *rules*:
  the blocks in scope, the individually selected extras, the custom
  glyphs, and per-code-point import overrides.
- [`sources/allowlist.tsv`](sources/allowlist.tsv) is *generated* from
  those rules and the pinned fonts, and committed. One row per code
  point, recording its Unicode name, its block, the glyph name in the
  output font, whether it needs `U+FE0E`, and its source: `custom`,
  `literata` (preserved) or `dejavu` (imported).

Regenerate it with `mise run allowlist` and commit the diff.
`asterwell-build allowlist --check` fails if the checked-in file no
longer matches the rules and the pinned fonts, so bumping an upstream
pin shows up as a reviewable coverage diff rather than a silent change
in what the family covers. The same command re-runs the provenance check
that keeps Bitstream Vera outlines out of the import set.

## Licensing

- **Asterwell Text** — SIL Open Font License, Version 1.1, in full, in
  [`OFL.txt`](OFL.txt). The family is a Modified Version of Literata, so
  the whole family is under the OFL; `"Asterwell"` is a Reserved Font
  Name, which means a modified version of these fonts must be renamed.
- **DejaVu material** — the glyphs imported from DejaVu Sans come from
  work the DejaVu project placed in the public domain. The Bitstream
  Vera and Arev copyright, trademark and permission notices travel with
  the fonts: verbatim in [`DEJAVU-LICENSE.txt`](DEJAVU-LICENSE.txt) and
  in the fonts' own name table.
- **Not for sale alone.** The fonts may be bundled with, and sold as
  part of, a larger package, but no copy of the font software may be
  sold by itself. The OFL and the Bitstream Vera notice agree on this.

[`FONTLOG.txt`](FONTLOG.txt) states the licensing position in full,
including the check that no Bitstream Vera code point is imported.
Redistribute `OFL.txt`, `DEJAVU-LICENSE.txt` and `FONTLOG.txt` with the
fonts.

## Releasing

1. Bump `version` in [`sources/family.toml`](sources/family.toml) and add
   the matching ChangeLog line in `FONTLOG.txt`.
2. Commit.
3. Tag it: `git tag -a v1.000 -m "Asterwell Text 1.000"`.
4. Push the tag: `git push origin v1.000`.

The tag must match `sources/family.toml`; the release build checks that
before it publishes anything.
