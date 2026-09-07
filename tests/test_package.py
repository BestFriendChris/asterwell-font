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

FAMILY_TOML = f"""\
family      = "Asterwell Text"
ps_family   = "AsterwellText"
version     = "{VERSION}"
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

DOCUMENT_TEXT = {
    "OFL.txt": "Copyright 2026 The Asterwell Text Project Authors\nSIL OFL 1.1\n",
    "DEJAVU-LICENSE.txt": "Bitstream Vera Fonts Copyright\n",
    "FONTLOG.txt": "FONTLOG for Asterwell Text\n1. Basic font information\n",
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
    """A synthetic repository, with the build's epoch in the environment."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
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
    assert notes.startswith(f"# Asterwell Text {VERSION}\n")
    assert f"AsterwellText-{VERSION}.zip" in notes


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
# `asterwell-build version`
# --------------------------------------------------------------------------- #


def test_version_reads_the_family_file(repo: Path) -> None:
    assert package.family_version(repo) == VERSION


def test_the_repository_s_own_version_is_what_the_command_prints(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = assemble.load_family(assemble.family_path_for(repo_root)).version
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == expected


@pytest.mark.parametrize("tag", [f"v{VERSION}", VERSION, f"  v{VERSION}  ", f"V{VERSION}"])
def test_assert_tag_accepts_the_matching_tag(repo: Path, tag: str) -> None:
    assert package.assert_tag(tag, repo) == VERSION


def test_assert_tag_rejects_a_different_version(repo: Path) -> None:
    with pytest.raises(package.PackageError) as error:
        package.assert_tag("v9.000", repo)
    message = str(error.value)
    assert "v9.000" in message
    assert VERSION in message
    assert "sources/family.toml" in message


def test_assert_tag_says_how_to_spell_a_numerically_equal_tag(repo: Path) -> None:
    """``v2.5`` is the same number as ``2.500`` and still the wrong tag."""
    with pytest.raises(package.PackageError, match=f"spelled exactly `v{VERSION}`"):
        package.assert_tag("v2.5", repo)


def test_assert_tag_rejects_something_that_is_not_a_version(repo: Path) -> None:
    with pytest.raises(package.PackageError, match="does not match"):
        package.assert_tag("release-candidate", repo)


def test_the_version_command_asserts_a_tag(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    version = assemble.load_family(assemble.family_path_for(repo_root)).version
    assert cli.main(["version", "--assert-tag", f"v{version}"]) == 0
    assert "matches" in capsys.readouterr().out


def test_the_version_command_fails_on_a_mismatched_tag(
    repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["version", "--assert-tag", "v99.999"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not match" in captured.err
