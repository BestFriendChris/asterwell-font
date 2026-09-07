"""Unit tests for the release payload.

Everything here runs against a *synthetic* build tree: a dozen files whose
contents are a few bytes of text, laid out exactly the way ``mise run build``
lays out ``fonts/``. The real family is 4.7 MB of TTFs and takes seven minutes
to produce, and none of what packaging has to get right — which files ship,
under which names, hashed how, stamped with which timestamp — depends on the
bytes being fonts.

What the suite pins down:

* the zip carries exactly the expected member set, at the expected paths;
* two runs produce byte-identical zips under a fixed ``SOURCE_DATE_EPOCH``,
  which is what CI's reproducibility step compares;
* ``SHA256SUMS.txt`` covers every shipped file *and* the zip, and verifies —
  both by reimplementing ``sha256sum -c`` and, where the binary exists, by
  running the real one from ``fonts/``;
* the release notes carry the version, the pins and the coverage counts;
* the two Markdown fontbakery reports ship and their ``.html``/``.json`` twins
  do not, even though the fixture writes all six into ``fonts/qa``;
* a tree that disagrees with ``BUILD-INFO.json`` is refused rather than shipped.
"""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from asterwell_build import allowlist, assemble, cli, package, qa, specimen

# A fixed instant with no special properties beyond being after 1980 and
# recognisable in a failure message: 2023-11-14T22:13:20Z.
EPOCH = 1700000000
EPOCH_STAMP = (2023, 11, 14, 22, 13, 20)

VERSION = "2.500"

#: What `mise run package` is told to build, the way the release workflow tells
#: it: `family.toml` carries no version, so every packaging test sets one.
FAMILY_TOML = """\
family      = "Asterwell Text"
ps_family   = "AsterwellText"
vendor_id   = "ASTW"
repo_url    = "https://example.invalid/asterwell"
license_url = "https://example.invalid/ofl"
copyright_holder = "The Asterwell Text Project Authors"
copyright_year   = "2026"
reserved_font_name = "Asterwell"
"""

UPSTREAM_TOML = """\
[literata]
name    = "Literata"
version = "3.103"
repo    = "https://example.invalid/literata"
commit  = "0c2761b727a1b3a7cffd313c37f0f5163dfc7a63"
url     = "https://example.invalid/literata/3.103.zip"
sha256  = "f7fb973cafb26cf785cbebaeaf51c18f87c15a3bcf4d82a7d4857564db5b056d"
size    = 17546949
license = "OFL-1.1"
[literata.members]
"fonts/variable/Literata[opsz,wght].ttf" = \
"b41138c9373112f32abb589cc22e8674b06ed4048b0c513be922bdd26f274440"

[dejavu]
name    = "DejaVu Sans"
version = "2.37"
repo    = "https://example.invalid/dejavu"
tag     = "version_2_37"
url     = "https://example.invalid/dejavu/dejavu-fonts-ttf-2.37.tar.bz2"
sha256  = "fa9ca4d13871dd122f61258a80d01751d603b4d3ee14095d65453b4e846e17d7"
size    = 5429777
license = "Bitstream-Vera"
[dejavu.members]
"dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf" = \
"7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954"
"""

#: The synthetic build's outputs, ``fonts/``-relative — two variable fonts, two
#: statics and two web fonts. Fewer than the real sixteen statics, because the
#: count is the build's business and packaging only has to carry what it finds.
FONT_OUTPUTS = (
    "ttf/AsterwellText-Bold.ttf",
    "ttf/AsterwellText-Regular.ttf",
    "variable/AsterwellText-Italic[opsz,wght].ttf",
    "variable/AsterwellText[opsz,wght].ttf",
    "webfonts/AsterwellText-Italic[opsz,wght].woff2",
    "webfonts/AsterwellText[opsz,wght].woff2",
)

#: The fontbakery reports the fixture's ``qa`` run left behind. Only the two
#: Markdown ones ship; the ``.html`` and ``.json`` twins are written beside them
#: so that "they stay out of the zip" is a tested property and not an assumption.
QA_REPORTS = ("qa/fontbakery-variable.md", "qa/fontbakery-static.md")
QA_UNSHIPPED = (
    "qa/fontbakery-variable.html",
    "qa/fontbakery-variable.json",
    "qa/fontbakery-static.html",
    "qa/fontbakery-static.json",
)

#: What the zip must contain for that build: the fonts at their ``fonts/``
#: paths, the specimen, the two fontbakery reports, the build manifest, and the
#: repository documents at the archive root — ``sources/allowlist.tsv``
#: flattened to ``allowlist.tsv``.
EXPECTED_MEMBERS = frozenset(
    {
        *FONT_OUTPUTS,
        *QA_REPORTS,
        "specimen/index.html",
        "BUILD-INFO.json",
        "OFL.txt",
        "DEJAVU-LICENSE.txt",
        "FONTLOG.txt",
        "AUTHORS.txt",
        "README.md",
        "allowlist.tsv",
    }
)

ROWS = [
    allowlist.Row(
        codepoint=0x273D,
        char="✽",
        unicode_name="HEAVY TEARDROP-SPOKED ASTERISK",
        block="Dingbats",
        source=allowlist.SOURCE_CUSTOM,
        glyph_name="uni273D",
        group="stars",
        emoji_presentation="no",
        note="",
    ),
    allowlist.Row(
        codepoint=0x2022,
        char="•",
        unicode_name="BULLET",
        block="General Punctuation",
        source=allowlist.SOURCE_LITERATA,
        glyph_name="bullet",
        group="punctuation",
        emoji_presentation="no",
        note="",
    ),
    allowlist.Row(
        codepoint=0x2731,
        char="✱",
        unicode_name="HEAVY ASTERISK",
        block="Dingbats",
        source=allowlist.SOURCE_DEJAVU,
        glyph_name="uni2731",
        group="dingbats",
        emoji_presentation="no",
        note="",
    ),
    allowlist.Row(
        codepoint=0x26A1,
        char="⚡",
        unicode_name="HIGH VOLTAGE SIGN",
        block="Miscellaneous Symbols",
        source=allowlist.SOURCE_DEJAVU,
        glyph_name="uni26A1",
        group="ui-reserve",
        emoji_presentation="yes",
        note="",
    ),
]

#: The ChangeLog entry this synthetic release was asked for: its text is the
#: first paragraph of the notes.
CHANGELOG_TEXT = "the synthetic release, for the tests."

DOCUMENT_TEXT = {
    "OFL.txt": "Copyright 2026 The Asterwell Text Project Authors\nSIL OFL 1.1\n",
    "DEJAVU-LICENSE.txt": "Bitstream Vera Fonts Copyright\n",
    "FONTLOG.txt": (
        "FONTLOG for Asterwell Text\n"
        "==========================\n"
        "\n"
        "ChangeLog\n"
        "---------\n"
        "\n"
        f"{VERSION} (2026-09-07): {CHANGELOG_TEXT}\n"
    ),
    "AUTHORS.txt": "The Asterwell Text Project Authors\n",
    "README.md": "# Asterwell Text\n\nA synthetic README.\n",
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_repo(root: Path) -> Path:
    """A repository whose ``fonts/`` tree looks like a finished build."""
    _write(assemble.family_path_for(root), FAMILY_TOML)
    _write(root / "sources" / "upstream.toml", UPSTREAM_TOML)
    _write(allowlist.tsv_path_for(root), allowlist.render(ROWS))
    for name, text in DOCUMENT_TEXT.items():
        _write(root / name, text)

    fonts = assemble.fonts_dir_for(root)
    for relative in FONT_OUTPUTS:
        # Distinct contents per file, so a swapped pair is a test failure.
        _write(fonts / relative, f"synthetic font: {relative}\n")
    _write(
        fonts / specimen.SPECIMEN_DIR / specimen.SPECIMEN_NAME,
        "<!doctype html><title>Specimen</title>\n",
    )
    for relative in (*QA_REPORTS, *QA_UNSHIPPED):
        _write(fonts / relative, f"synthetic fontbakery report: {relative}\n")
    _write_build_info(root)
    return root


def _write_build_info(root: Path, *, epoch: int | None = EPOCH) -> None:
    fonts = assemble.fonts_dir_for(root)
    _write(
        fonts / assemble.BUILD_INFO_NAME,
        json.dumps(
            {
                "version": VERSION,
                "source_date_epoch": epoch,
                "tools": {"python": "3.13.15", "fonttools": "4.64.0"},
                "inputs": {"literata/Literata.ttf": "0" * 64},
                "allowlist_sha256": "1" * 64,
                "outputs": {
                    relative: _sha256(fonts / relative) for relative in FONT_OUTPUTS
                },
            },
            indent=2,
        )
        + "\n",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic repository, with the build's epoch and version in the
    environment — the two things the release workflow puts there."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    monkeypatch.setenv(package.VERSION_ENV, VERSION)
    return _make_repo(tmp_path / "repo")


@pytest.fixture
def packaged(repo: Path) -> Path:
    """A repository that has been packaged once; the fixture is its root."""
    assert package.package(repo, quiet=True) == 0
    return repo


def _archive(root: Path) -> Path:
    return package.dist_dir_for(root) / f"AsterwellText-{VERSION}.zip"


def _checksums(root: Path) -> dict[str, str]:
    """``SHA256SUMS.txt`` parsed the way a checker reads it: path → digest."""
    text = (package.dist_dir_for(root) / package.CHECKSUMS_NAME).read_text(encoding="utf-8")
    entries: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        digest, _, path = line.partition("  ")
        assert path, f"not a sha256sum line: {line!r}"
        entries[path] = digest
    return entries


# --------------------------------------------------------------------------- #
# What the zip contains
# --------------------------------------------------------------------------- #


def test_package_writes_the_three_dist_files(packaged: Path) -> None:
    dist = package.dist_dir_for(packaged)
    assert sorted(path.name for path in dist.iterdir()) == [
        f"AsterwellText-{VERSION}.zip",
        package.RELEASE_NOTES_NAME,
        package.CHECKSUMS_NAME,
    ]


def test_the_zip_contains_exactly_the_expected_members(packaged: Path) -> None:
    with zipfile.ZipFile(_archive(packaged)) as archive:
        assert set(archive.namelist()) == EXPECTED_MEMBERS


def test_every_member_carries_the_bytes_of_its_source_file(packaged: Path) -> None:
    fonts = assemble.fonts_dir_for(packaged)
    sources = {
        **{relative: fonts / relative for relative in FONT_OUTPUTS},
        **{relative: fonts / relative for relative in QA_REPORTS},
        "specimen/index.html": fonts / "specimen" / "index.html",
        "BUILD-INFO.json": fonts / assemble.BUILD_INFO_NAME,
        "allowlist.tsv": allowlist.tsv_path_for(packaged),
        **{name: packaged / name for name in DOCUMENT_TEXT},
    }
    with zipfile.ZipFile(_archive(packaged)) as archive:
        for arcname, source in sources.items():
            assert archive.read(arcname) == source.read_bytes(), arcname


def test_members_are_stored_in_sorted_order(packaged: Path) -> None:
    """Order is part of the archive's bytes, so it cannot depend on a listing."""
    with zipfile.ZipFile(_archive(packaged)) as archive:
        assert archive.namelist() == sorted(EXPECTED_MEMBERS)


def test_the_shipped_reports_are_the_markdown_report_of_every_file_class() -> None:
    """A release that claims to be checked carries the check's own verdict: one
    Markdown report per file class ``qa`` runs fontbakery over. Adding a third
    class to :data:`qa.FONTBAKERY_GROUPS` without shipping its report fails
    here rather than shipping a release that quietly reports on two thirds of
    the family."""
    assert set(package.QA_REPORTS) == set(QA_REPORTS)
    assert {path.split("/")[0] for path in package.QA_REPORTS} == {qa.QA_DIR}
    groups = {
        path.split("/")[1].removeprefix("fontbakery-").removesuffix(".md")
        for path in package.QA_REPORTS
    }
    assert groups == set(qa.FONTBAKERY_GROUPS)


def test_the_html_and_json_reports_stay_out_of_the_zip(packaged: Path) -> None:
    """They are in ``fonts/qa`` — the fixture writes them — and are still not
    members: the ``.html`` twin is the same findings at twice the bytes, and
    the ``.json`` one is a machine format that would nearly double the archive.
    Also proof the manifest is derived rather than a glob of ``fonts/qa``."""
    fonts = assemble.fonts_dir_for(packaged)
    for relative in QA_UNSHIPPED:
        assert (fonts / relative).is_file(), relative
    with zipfile.ZipFile(_archive(packaged)) as archive:
        members = set(archive.namelist())
    assert members.isdisjoint(QA_UNSHIPPED)
    assert set(_checksums(packaged)).isdisjoint(QA_UNSHIPPED)


def test_the_specimen_reaches_its_web_fonts_from_inside_the_zip(packaged: Path) -> None:
    """``specimen/index.html`` links ``../webfonts/…``. That link is only good
    if the zip lays the two directories out as siblings, which is the whole
    reason the archive mirrors ``fonts/`` rather than flattening it."""
    web_font = "AsterwellText[opsz,wght].woff2"
    linked = posixpath.normpath(
        posixpath.join(specimen.SPECIMEN_DIR, specimen.WEBFONT_PREFIX + web_font)
    )
    assert linked == f"webfonts/{web_font}"
    with zipfile.ZipFile(_archive(packaged)) as archive:
        assert linked in archive.namelist()


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #


def test_two_runs_produce_byte_identical_zips(repo: Path) -> None:
    assert package.package(repo, quiet=True) == 0
    first = _archive(repo).read_bytes()
    shutil.rmtree(package.dist_dir_for(repo))
    assert package.package(repo, quiet=True) == 0
    assert _archive(repo).read_bytes() == first


def test_two_runs_produce_identical_checksum_files(repo: Path) -> None:
    """What §10.1's reproducibility step diffs, so it has to be stable."""
    assert package.package(repo, quiet=True) == 0
    first = (package.dist_dir_for(repo) / package.CHECKSUMS_NAME).read_text(encoding="utf-8")
    assert package.package(repo, quiet=True) == 0
    assert (package.dist_dir_for(repo) / package.CHECKSUMS_NAME).read_text(
        encoding="utf-8"
    ) == first


def test_every_entry_is_stamped_with_the_source_date_epoch(packaged: Path) -> None:
    with zipfile.ZipFile(_archive(packaged)) as archive:
        stamps = {info.date_time for info in archive.infolist()}
    assert stamps == {EPOCH_STAMP}


def test_entries_carry_a_fixed_mode_and_creator(packaged: Path) -> None:
    """Not the umask's and not the host's, or the archive stops being portable."""
    with zipfile.ZipFile(_archive(packaged)) as archive:
        for info in archive.infolist():
            assert info.external_attr == package.ZIP_FILE_MODE, info.filename
            assert info.create_system == package.ZIP_CREATE_SYSTEM, info.filename
            assert info.compress_type == zipfile.ZIP_DEFLATED, info.filename


def test_the_environment_wins_over_the_recorded_epoch(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1000000000")
    assert package.package(repo, quiet=True) == 0
    with zipfile.ZipFile(_archive(repo)) as archive:
        assert {info.date_time for info in archive.infolist()} == {(2001, 9, 9, 1, 46, 40)}


def test_without_an_environment_epoch_the_build_info_is_used(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mise run package`` exports no epoch; the zip still matches its fonts."""
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    assert package.package(repo, quiet=True) == 0
    with zipfile.ZipFile(_archive(repo)) as archive:
        assert {info.date_time for info in archive.infolist()} == {EPOCH_STAMP}


def test_an_epoch_before_1980_is_clamped_rather_than_fatal(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SOURCE_DATE_EPOCH=0`` is mise.toml's fallback when git is unavailable."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    assert package.package(repo, quiet=True) == 0
    with zipfile.ZipFile(_archive(repo)) as archive:
        assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_with_no_epoch_at_all_the_timestamp_is_still_fixed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    _write_build_info(repo, epoch=None)
    assert package.package(repo, quiet=True) == 0
    with zipfile.ZipFile(_archive(repo)) as archive:
        assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}


# --------------------------------------------------------------------------- #
# SHA256SUMS.txt
# --------------------------------------------------------------------------- #


def test_checksums_cover_every_shipped_file_and_the_zip(packaged: Path) -> None:
    expected = {
        *FONT_OUTPUTS,
        *QA_REPORTS,
        "specimen/index.html",
        "BUILD-INFO.json",
        f"dist/AsterwellText-{VERSION}.zip",
        "../OFL.txt",
        "../DEJAVU-LICENSE.txt",
        "../FONTLOG.txt",
        "../AUTHORS.txt",
        "../README.md",
        "../sources/allowlist.tsv",
    }
    assert set(_checksums(packaged)) == expected


def test_every_recorded_digest_is_the_file_s_own(packaged: Path) -> None:
    """``sha256sum -c`` semantics, reimplemented: resolve each path from
    ``fonts/`` and hash what is there."""
    fonts = assemble.fonts_dir_for(packaged)
    for path, digest in _checksums(packaged).items():
        target = (fonts / path).resolve()
        assert target.is_file(), path
        assert _sha256(target) == digest, path


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="no sha256sum binary")
def test_the_real_sha256sum_verifies_the_file_from_fonts(packaged: Path) -> None:
    """The done-when check itself: `cd fonts && sha256sum -c dist/SHA256SUMS.txt`."""
    completed = subprocess.run(
        ["sha256sum", "-c", f"{package.DIST_DIR}/{package.CHECKSUMS_NAME}"],
        cwd=assemble.fonts_dir_for(packaged),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.count(": OK") == len(_checksums(packaged))


def test_a_tampered_file_fails_the_checksums(packaged: Path) -> None:
    (assemble.fonts_dir_for(packaged) / "specimen" / "index.html").write_text(
        "<!doctype html><title>Tampered</title>\n", encoding="utf-8"
    )
    entries = _checksums(packaged)
    fonts = assemble.fonts_dir_for(packaged)
    bad = [path for path, digest in entries.items() if _sha256((fonts / path).resolve()) != digest]
    assert bad == ["specimen/index.html"]


def test_checksums_carry_a_header_a_checker_ignores() -> None:
    text = package.checksums_text({"variable/Font.ttf": "a" * 64})
    assert text.startswith("#")
    assert text.endswith("\n")
    assert f"{'a' * 64}  variable/Font.ttf" in text.splitlines()


# --------------------------------------------------------------------------- #
# RELEASE-NOTES.md
# --------------------------------------------------------------------------- #


@pytest.fixture
def notes(packaged: Path) -> str:
    return (package.dist_dir_for(packaged) / package.RELEASE_NOTES_NAME).read_text(
        encoding="utf-8"
    )


def test_the_notes_name_the_family_and_its_version(notes: str) -> None:
    assert notes.startswith(f"# Asterwell Text v{VERSION}\n")
    assert f"AsterwellText-{VERSION}.zip" in notes


def test_the_notes_open_with_the_changelog_entry(notes: str) -> None:
    """The one human-written sentence about what changed leads the release."""
    assert notes.splitlines()[:3] == [f"# Asterwell Text v{VERSION}", "", CHANGELOG_TEXT]


def test_the_notes_do_without_a_changelog_entry_they_cannot_read(repo: Path) -> None:
    """Grammar is QA's gate: packaging does not refuse to write the notes."""
    (repo / "FONTLOG.txt").write_text("FONTLOG for Asterwell Text\n", encoding="utf-8")
    assert package.package(repo, quiet=True) == 0
    notes = (package.dist_dir_for(repo) / package.RELEASE_NOTES_NAME).read_text(
        encoding="utf-8"
    )
    assert notes.startswith(f"# Asterwell Text v{VERSION}\n\nAsterwell Text is ")


def test_the_notes_carry_both_upstream_pins(notes: str) -> None:
    assert "Literata" in notes and "3.103" in notes
    assert "DejaVu Sans" in notes and "2.37" in notes
    # A commit pin reads as a commit and a tag pin as a tag.
    assert "commit `0c2761b727a1`" in notes
    assert "tag `version_2_37`" in notes


def test_the_notes_count_the_allowlist_by_source(notes: str) -> None:
    assert "| `custom` | 1 |" in notes
    assert "| `literata` | 1 |" in notes
    assert "| `dejavu` | 2 |" in notes
    assert f"| **total** | **{len(ROWS)}** |" in notes


def test_the_notes_quote_the_checksums_that_matter(packaged: Path, notes: str) -> None:
    entries = _checksums(packaged)
    assert entries[f"dist/AsterwellText-{VERSION}.zip"] in notes
    assert entries["variable/AsterwellText[opsz,wght].ttf"] in notes
    assert entries["webfonts/AsterwellText[opsz,wght].woff2"] in notes
    assert package.CHECKSUMS_NAME in notes


def test_the_notes_record_the_epoch_the_build_was_pinned_to(notes: str) -> None:
    assert f"SOURCE_DATE_EPOCH={EPOCH}" in notes
    assert "2023-11-14" in notes


# --------------------------------------------------------------------------- #
# What packaging refuses to ship
# --------------------------------------------------------------------------- #


def test_a_missing_font_is_named_with_the_task_that_makes_it(repo: Path) -> None:
    (assemble.fonts_dir_for(repo) / "variable" / "AsterwellText[opsz,wght].ttf").unlink()
    with pytest.raises(package.PackageError) as error:
        package.package(repo, quiet=True)
    assert "variable/AsterwellText[opsz,wght].ttf" in str(error.value)
    assert "mise run build" in str(error.value)


def test_a_missing_specimen_is_named_with_its_own_task(repo: Path) -> None:
    (assemble.fonts_dir_for(repo) / "specimen" / "index.html").unlink()
    with pytest.raises(package.PackageError) as error:
        package.package(repo, quiet=True)
    assert "mise run specimen" in str(error.value)


def test_a_missing_fontbakery_report_is_named_with_its_own_task(repo: Path) -> None:
    """Packaging a tree ``qa`` has not run over is a release that claims a check
    nobody made, so it is refused — with the command that makes the report."""
    (assemble.fonts_dir_for(repo) / "qa" / "fontbakery-static.md").unlink()
    with pytest.raises(package.PackageError) as error:
        package.package(repo, quiet=True)
    assert "qa/fontbakery-static.md" in str(error.value)
    assert "mise run qa" in str(error.value)


def test_a_missing_document_is_reported_as_a_checked_in_file(repo: Path) -> None:
    (repo / "FONTLOG.txt").unlink()
    with pytest.raises(package.PackageError) as error:
        package.package(repo, quiet=True)
    assert "FONTLOG.txt" in str(error.value)
    assert "checked in" in str(error.value)


def test_a_stale_font_is_refused_rather_than_shipped(repo: Path) -> None:
    """A file whose bytes are not the ones the build hashed never reaches a zip."""
    (assemble.fonts_dir_for(repo) / "ttf" / "AsterwellText-Regular.ttf").write_text(
        "hand-edited\n", encoding="utf-8"
    )
    with pytest.raises(package.PackageError) as error:
        package.package(repo, quiet=True)
    assert "ttf/AsterwellText-Regular.ttf" in str(error.value)
    assert "BUILD-INFO.json" in str(error.value)
    assert not package.dist_dir_for(repo).exists()


def test_packaging_without_a_build_says_so(repo: Path) -> None:
    (assemble.fonts_dir_for(repo) / assemble.BUILD_INFO_NAME).unlink()
    with pytest.raises(package.PackageError) as error:
        package.package(repo, quiet=True)
    assert "mise run build" in str(error.value)


def test_an_unreadable_build_info_is_a_package_error(repo: Path) -> None:
    (assemble.fonts_dir_for(repo) / assemble.BUILD_INFO_NAME).write_text("{", encoding="utf-8")
    with pytest.raises(package.PackageError, match="not valid JSON"):
        package.package(repo, quiet=True)


def test_the_command_turns_a_package_error_into_exit_1(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(package.upstream, "default_root", lambda: repo)
    (assemble.fonts_dir_for(repo) / assemble.BUILD_INFO_NAME).unlink()
    assert cli.main(["package"]) == 1
    assert "error:" in capsys.readouterr().err


def test_the_command_packages_the_default_root(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(package.upstream, "default_root", lambda: repo)
    assert cli.main(["package"]) == 0
    assert _archive(repo).is_file()
    assert f"AsterwellText-{VERSION}.zip" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Versions: the grammar
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("0.001", (0, 1)),
        ("v0.001", (0, 1)),
        ("  v0.010  ", (0, 10)),
        ("0.999", (0, 999)),
        ("1.000", (1, 0)),
        ("12.345", (12, 345)),
    ],
)
def test_parse_version_reads_the_one_spelling(text: str, number: tuple[int, int]) -> None:
    assert package.parse_version(text) == number


@pytest.mark.parametrize(
    ("text", "message"),
    [
        # The whole reason for three digits: 0.1 sorts above 0.09 as a number.
        ("0.1", "write 0.001"),
        ("1.22", "write 1.022"),
        ("1.00", "three digits"),
        ("1.0000", "three digits"),
        ("01.000", "write 1.000"),
        ("1", "M.mmm"),
        ("v1", "M.mmm"),
        ("release-candidate", "M.mmm"),
        ("", "M.mmm"),
    ],
)
def test_parse_version_refuses_every_other_spelling(text: str, message: str) -> None:
    with pytest.raises(package.PackageError, match=message):
        package.parse_version(text)


def test_format_version_is_parse_version_backwards() -> None:
    assert package.format_version(0, 1) == "0.001"
    assert package.format_version(1, 0) == "1.000"
    assert package.format_version(*package.parse_version("v0.010")) == "0.010"


# --------------------------------------------------------------------------- #
# Versions: FONTLOG.txt's ChangeLog
# --------------------------------------------------------------------------- #


def _changelog(*body: str) -> str:
    """A FONTLOG-shaped file whose ChangeLog section is ``body``."""
    return "\n".join(
        [
            "FONTLOG for Asterwell Text",
            "==========================",
            "",
            "ChangeLog",
            "---------",
            "",
            *body,
            "",
            "Acknowledgements",
            "----------------",
            "",
            "1998 was a good year for fonts.",
            "",
        ]
    )


def test_parse_changelog_reads_the_entries_newest_first() -> None:
    entries = package.parse_changelog(
        _changelog(
            "0.010 (2026-09-10): the tenth release.",
            "",
            "0.009 (2026-09-09): the ninth.",
        )
    )
    assert [(entry.version, entry.date, entry.text) for entry in entries] == [
        ("0.010", "2026-09-10", "the tenth release."),
        ("0.009", "2026-09-09", "the ninth."),
    ]
    assert entries[0].tag == "v0.010"
    assert entries[0].number == (0, 10)


def test_parse_changelog_joins_continuation_lines() -> None:
    entries = package.parse_changelog(
        _changelog(
            "0.002 (2026-09-10): the stars turn in the italic,",
            "    and the asterisks lean.",
        )
    )
    assert entries[0].text == "the stars turn in the italic, and the asterisks lean."


def test_parse_changelog_leaves_the_note_under_the_heading_alone() -> None:
    """The checked-in ChangeLog opens with prose about how to add an entry."""
    entries = package.parse_changelog(
        _changelog(
            "Newest first: an entry merged to main releases that version.",
            "",
            "0.001 (2026-09-08): initial release.",
        )
    )
    assert [entry.version for entry in entries] == ["0.001"]


def test_parse_changelog_reads_an_empty_changelog_as_no_entries() -> None:
    assert package.parse_changelog(_changelog("Nothing has been released yet.")) == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (("0.1 (2026-09-10): too few digits.",), "write 0.001"),
        (("01.000 (2026-09-10): a leading zero.",), "write 1.000"),
        (("1.0000 (2026-09-10): too many digits.",), "three digits"),
        (("0.001 (10 September 2026): a prose date.",), "not YYYY-MM-DD"),
        (("0.001: no date at all.",), "M.mmm \\(YYYY-MM-DD\\)"),
        (("  0.001 (2026-09-10): indented.",), "column 0"),
        (
            ("0.002 (2026-09-10): out of order.", "0.003 (2026-09-11): newer, below."),
            "newest first",
        ),
        (
            ("0.002 (2026-09-10): twice.", "0.002 (2026-09-09): again."),
            "twice",
        ),
    ],
)
def test_parse_changelog_refuses_and_says_what_to_write(
    body: tuple[str, ...], message: str
) -> None:
    with pytest.raises(package.PackageError, match=message):
        package.parse_changelog(_changelog(*body))


def test_parse_changelog_refuses_an_entry_above_the_heading() -> None:
    text = "0.001 (2026-09-08): stray.\n\n" + _changelog("0.002 (2026-09-10): here.")
    with pytest.raises(package.PackageError, match="above the `ChangeLog` heading"):
        package.parse_changelog(text)


def test_parse_changelog_needs_the_heading() -> None:
    with pytest.raises(package.PackageError, match="no `ChangeLog` heading"):
        package.parse_changelog("FONTLOG for Asterwell Text\n\n0.001: nothing here.\n")


def test_parse_changelog_stops_at_the_next_section() -> None:
    """The acknowledgements below are prose, not a rejected entry."""
    text = _changelog("0.001 (2026-09-08): initial release.").replace(
        "1998 was a good year for fonts.", "0.5 (1998): a version-shaped sentence."
    )
    assert [entry.version for entry in package.parse_changelog(text)] == ["0.001"]


def test_the_checked_in_changelog_parses(repo_root: Path) -> None:
    """Whatever FONTLOG.txt says today, every build has to be able to read it."""
    package.parse_changelog(
        package.fontlog_path_for(repo_root).read_text(encoding="utf-8")
    )


# --------------------------------------------------------------------------- #
# Versions: what a merge would release
# --------------------------------------------------------------------------- #


def _entries(*versions: str) -> list[package.Entry]:
    return [
        package.Entry(version=version, date="2026-09-10", text="what changed.")
        for version in versions
    ]


def test_nothing_is_pending_without_a_changelog_entry() -> None:
    assert package.pending_release([], ["v0.001"]) is None


def test_an_untagged_top_entry_is_the_pending_release() -> None:
    assert package.pending_release(_entries("0.002", "0.001"), ["v0.001"]) == "0.002"


def test_the_first_release_needs_no_tags_at_all() -> None:
    assert package.pending_release(_entries("0.001"), []) == "0.001"


def test_a_tagged_top_entry_releases_nothing() -> None:
    assert package.pending_release(_entries("0.002", "0.001"), ["v0.001", "v0.002"]) is None


def test_a_tag_at_head_resumes_its_own_release() -> None:
    """A run that tagged and then failed before publishing re-runs from here."""
    entries = _entries("0.002", "0.001")
    tags = ["v0.001", "v0.002"]
    assert package.pending_release(entries, tags, ["v0.002"]) == "0.002"
    assert package.pending_release(entries, tags, ["v0.001"]) is None


def test_a_top_entry_below_the_newest_tag_is_refused() -> None:
    with pytest.raises(package.PackageError, match="must be greater"):
        package.pending_release(_entries("0.001"), ["v0.002"])


def test_two_untagged_entries_are_refused() -> None:
    with pytest.raises(package.PackageError, match="only the top entry may be untagged"):
        package.pending_release(_entries("0.003", "0.002", "0.001"), ["v0.001"])


def test_versions_are_compared_as_numbers_not_as_text() -> None:
    """`0.010 > 0.009` and `1.000 > 0.999` — the whole point of three digits."""
    assert package.pending_release(_entries("0.010", "0.009"), ["v0.009"]) == "0.010"
    assert package.pending_release(_entries("1.000", "0.999"), ["v0.999"]) == "1.000"
    with pytest.raises(package.PackageError, match="must be greater"):
        package.pending_release(_entries("0.009"), ["v0.010"])


def test_tags_that_are_not_release_tags_are_ignored() -> None:
    assert package.pending_release(_entries("0.001"), ["v1", "v0.1", "verified"]) == "0.001"


# --------------------------------------------------------------------------- #
# Versions: what this build carries
# --------------------------------------------------------------------------- #


def test_a_build_with_nothing_to_go_on_is_a_dev_build(tmp_path: Path) -> None:
    resolved = package.resolve_version(tmp_path, {})
    assert (resolved.version, resolved.source) == (package.DEV_VERSION, package.SOURCE_DEV)
    assert not resolved.is_release
    assert str(resolved) == "0.000 (dev build)"


@pytest.mark.parametrize("named", ["0.002", "v0.002", " 0.002 "])
def test_the_environment_names_the_version(tmp_path: Path, named: str) -> None:
    resolved = package.resolve_version(tmp_path, {package.VERSION_ENV: named})
    assert (resolved.version, resolved.source) == ("0.002", package.SOURCE_ENV)
    assert resolved.is_release
    assert str(resolved) == "0.002 (from ASTERWELL_VERSION)"


def test_a_malformed_environment_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(package.PackageError, match="write 0.002"):
        package.resolve_version(tmp_path, {package.VERSION_ENV: "0.2"})


def test_an_empty_environment_version_is_no_version_at_all(tmp_path: Path) -> None:
    resolved = package.resolve_version(tmp_path, {package.VERSION_ENV: "  "})
    assert resolved.version == package.DEV_VERSION


@pytest.fixture
def tagged_checkout(tmp_path: Path) -> Path:
    """A one-commit git repository whose HEAD carries two release tags."""
    if shutil.which("git") is None:  # pragma: no cover - git is a dev dependency
        pytest.skip("git is not installed")
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "README.md").write_text("# synthetic\n", encoding="utf-8")
    run = lambda *argv: subprocess.run(  # noqa: E731 - one line, one meaning
        ["git", "-C", str(root), *argv], check=True, capture_output=True
    )
    run("init", "-q")
    run("config", "user.email", "tests@example.invalid")
    run("config", "user.name", "Asterwell tests")
    run("add", "README.md")
    run("commit", "-qm", "synthetic")
    run("tag", "v0.009")
    run("tag", "v0.010")
    run("tag", "not-a-release")
    return root


def test_a_tag_at_head_names_the_version(tagged_checkout: Path) -> None:
    """The newest release tag on this commit — 0.010 outranks 0.009."""
    resolved = package.resolve_version(tagged_checkout, {})
    assert (resolved.version, resolved.source) == ("0.010", package.SOURCE_TAG)
    assert resolved.is_release
    assert str(resolved) == "0.010 (from the v0.010 tag at HEAD)"


def test_the_environment_outranks_a_tag_at_head(tagged_checkout: Path) -> None:
    resolved = package.resolve_version(tagged_checkout, {package.VERSION_ENV: "0.011"})
    assert (resolved.version, resolved.source) == ("0.011", package.SOURCE_ENV)


def test_a_directory_that_is_not_a_repository_is_a_dev_build(tmp_path: Path) -> None:
    assert package.tags_at_head(tmp_path / "nowhere") == []


# --------------------------------------------------------------------------- #
# `asterwell-build version`
# --------------------------------------------------------------------------- #


def test_the_command_reports_this_build_s_version(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(package.upstream, "default_root", lambda: repo)
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"{VERSION} (from ASTERWELL_VERSION)"


def test_the_command_reports_a_dev_build(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(package.VERSION_ENV)
    monkeypatch.setattr(package.upstream, "default_root", lambda: repo)
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.000 (dev build)"


def _pending(monkeypatch: pytest.MonkeyPatch, root: Path, tags: str) -> None:
    """Point the command at ``root`` and put ``tags`` on its stdin."""
    monkeypatch.setattr(package.upstream, "default_root", lambda: root)
    monkeypatch.setattr(package.sys, "stdin", io.StringIO(tags))


def test_the_command_prints_the_pending_version(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "FONTLOG.txt").write_text(
        _changelog("0.002 (2026-09-10): the second.", "0.001 (2026-09-08): the first."),
        encoding="utf-8",
    )
    _pending(monkeypatch, repo, "v0.001\n")
    assert cli.main(["version", "--pending"]) == 0
    assert capsys.readouterr().out == "0.002\n"


def test_the_command_prints_nothing_when_nothing_is_pending(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "FONTLOG.txt").write_text(
        _changelog("0.002 (2026-09-10): the second.", "0.001 (2026-09-08): the first."),
        encoding="utf-8",
    )
    _pending(monkeypatch, repo, "v0.001\nv0.002\n")
    assert cli.main(["version", "--pending"]) == 0
    assert capsys.readouterr().out == ""


def test_the_command_resumes_a_release_tagged_at_head(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "FONTLOG.txt").write_text(
        _changelog("0.002 (2026-09-10): the second."), encoding="utf-8"
    )
    _pending(monkeypatch, repo, "v0.002\n")
    assert cli.main(["version", "--pending", "--at-head", "v0.002\n"]) == 0
    assert capsys.readouterr().out == "0.002\n"


def test_the_command_fails_on_a_changelog_that_cannot_release(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "FONTLOG.txt").write_text(
        _changelog("0.001 (2026-09-08): older than the newest tag."), encoding="utf-8"
    )
    _pending(monkeypatch, repo, "v0.002\n")
    assert cli.main(["version", "--pending"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must be greater" in captured.err


def test_at_head_without_pending_is_an_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(package.upstream, "default_root", lambda: repo)
    assert cli.main(["version", "--at-head", "v0.002"]) == 1
    assert "--pending" in capsys.readouterr().err
