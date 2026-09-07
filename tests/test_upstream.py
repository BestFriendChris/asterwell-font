"""Fetching, verifying and extracting the pinned upstream archives.

Nothing here touches the network: every "download" is a ``file://`` URL for an
archive the test built moments earlier, which still exercises the real
:mod:`urllib.request` streaming and hashing path. The archives are shaped like
the real pins — a zip with a bracketed variable-font name plus ``__MACOSX``
cruft, and a bzip2 tarball with a versioned top-level directory.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import ssl
import tarfile
import urllib.error
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from asterwell_build import upstream
from asterwell_build.cli import COMMANDS, build_parser

# Members of the zip fixture. The bracketed name is the point of the exercise:
# it is why extraction goes through Python's zipfile rather than `unzip`, whose
# globbing would swallow it.
ZIP_MEMBERS = {
    "fonts/variable/Fixture[opsz,wght].ttf": b"roman variable font\n",
    "fonts/variable/Fixture-Italic[opsz,wght].ttf": b"italic variable font\n",
    "OFL.txt": b"the open font license\n",
}
# Archive cruft that is present but unpinned, so must not be extracted.
ZIP_CRUFT = {"__MACOSX/._OFL.txt": b"resource fork\n", "fonts/static/Fixture.ttf": b"static\n"}

# Members of the tarball fixture, under a versioned directory as the real one is.
TAR_MEMBERS = {
    "fixture-fonts-ttf-2.37/ttf/FixtureSans.ttf": b"donor font\n",
    "fixture-fonts-ttf-2.37/LICENSE": b"bitstream and arev notices\n",
    "fixture-fonts-ttf-2.37/AUTHORS": b"the fixture fonts team\n",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def make_tar_bz2(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:bz2") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


@dataclass
class Section:
    """One ``[key]`` table of a synthetic ``upstream.toml``."""

    key: str
    name: str
    version: str
    url: str
    sha256: str
    size: int
    members: dict[str, str] = field(default_factory=dict)

    def to_toml(self) -> str:
        lines = [
            f"[{self.key}]",
            f'name = "{self.name}"',
            f'version = "{self.version}"',
            f'url = "{self.url}"',
            f'sha256 = "{self.sha256}"',
            f"size = {self.size}",
            f"[{self.key}.members]",
        ]
        lines += [f'"{member}" = "{digest}"' for member, digest in self.members.items()]
        return "\n".join(lines) + "\n"


@dataclass
class Pinned:
    """A repo-shaped tmp dir whose pins point at locally built archives."""

    root: Path
    sections: dict[str, Section]
    contents: dict[str, dict[str, bytes]]

    @property
    def pin_file(self) -> Path:
        return self.root / "sources" / "upstream.toml"

    @property
    def upstream_dir(self) -> Path:
        return self.root / "build" / "upstream"

    def archive(self, key: str) -> Path:
        section = self.sections[key]
        suffix = ".tar.bz2" if section.url.endswith(".tar.bz2") else ".zip"
        return self.upstream_dir / f"{key}-{section.version}{suffix}"

    def extracted(self, key: str, member: str) -> Path:
        return self.upstream_dir / key / member

    def write(self) -> None:
        self.pin_file.parent.mkdir(parents=True, exist_ok=True)
        self.pin_file.write_text(
            "".join(section.to_toml() for section in self.sections.values()), encoding="utf-8"
        )

    def fetch(self, **kwargs: object) -> dict[str, object]:
        return upstream.fetch(self.root, quiet=True, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def pinned(tmp_path: Path) -> Pinned:
    """Two archives on disk (zip + tar.bz2) and a pin file naming them."""
    served = tmp_path / "served"
    served.mkdir()
    zip_path = served / "3.103.zip"
    make_zip(zip_path, {**ZIP_MEMBERS, **ZIP_CRUFT})
    tar_path = served / "fixture-fonts-ttf-2.37.tar.bz2"
    make_tar_bz2(tar_path, TAR_MEMBERS)

    sections = {
        "zipped": Section(
            key="zipped",
            name="Fixture Serif",
            version="3.103",
            url=zip_path.as_uri(),
            sha256=sha256_bytes(zip_path.read_bytes()),
            size=zip_path.stat().st_size,
            members={name: sha256_bytes(data) for name, data in ZIP_MEMBERS.items()},
        ),
        "tarred": Section(
            key="tarred",
            name="Fixture Sans",
            version="2.37",
            url=tar_path.as_uri(),
            sha256=sha256_bytes(tar_path.read_bytes()),
            size=tar_path.stat().st_size,
            members={name: sha256_bytes(data) for name, data in TAR_MEMBERS.items()},
        ),
    }
    pins = Pinned(
        root=tmp_path / "repo",
        sections=sections,
        contents={"zipped": ZIP_MEMBERS, "tarred": TAR_MEMBERS},
    )
    pins.write()
    return pins


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any attempt to open a URL an outright test failure."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"network access attempted: {args!r}")

    monkeypatch.setattr(upstream.urllib.request, "urlopen", forbidden)


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_fetch_downloads_verifies_and_extracts_every_pin(pinned: Pinned) -> None:
    manifest = pinned.fetch()

    assert pinned.archive("zipped").name == "zipped-3.103.zip"
    assert pinned.archive("tarred").name == "tarred-2.37.tar.bz2"
    for key, members in pinned.contents.items():
        assert pinned.archive(key).is_file()
        for member, data in members.items():
            assert pinned.extracted(key, member).read_bytes() == data

    assert manifest["schema"] == upstream.MANIFEST_SCHEMA
    archives = manifest["archives"]
    assert isinstance(archives, dict)
    assert set(archives) == {"zipped", "tarred"}
    assert archives["zipped"]["members"] == pinned.sections["zipped"].members
    assert archives["tarred"]["archive"] == "tarred-2.37.tar.bz2"


def test_fetch_writes_the_verified_manifest(pinned: Pinned) -> None:
    manifest = pinned.fetch()
    verified = pinned.upstream_dir / upstream.VERIFIED_NAME

    assert json.loads(verified.read_text(encoding="utf-8")) == manifest
    assert upstream.read_verified(pinned.root) == manifest


def test_bracketed_member_names_survive_extraction(pinned: Pinned) -> None:
    """The reason extraction never shells out to `unzip`, which globs `[...]`."""
    pinned.fetch()
    bracketed = pinned.extracted("zipped", "fonts/variable/Fixture[opsz,wght].ttf")
    assert bracketed.read_bytes() == ZIP_MEMBERS["fonts/variable/Fixture[opsz,wght].ttf"]


def test_unpinned_archive_entries_are_not_extracted(pinned: Pinned) -> None:
    pinned.fetch()
    for cruft in ZIP_CRUFT:
        assert not (pinned.upstream_dir / "zipped" / cruft).exists()


def test_a_second_run_does_no_network_io(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = pinned.fetch()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted on a warm build/upstream")

    monkeypatch.setattr(upstream.urllib.request, "urlopen", forbidden)
    assert pinned.fetch() == first


# --------------------------------------------------------------------------- #
# Verification failures
# --------------------------------------------------------------------------- #


def test_a_wrong_archive_sha256_aborts_and_deletes_the_partial_file(pinned: Pinned) -> None:
    pinned.sections["zipped"].sha256 = "0" * 64
    pinned.write()

    with pytest.raises(upstream.UpstreamError) as exc:
        pinned.fetch()

    assert "sha256" in str(exc.value)
    assert not pinned.archive("zipped").exists()
    assert not (pinned.upstream_dir / upstream.VERIFIED_NAME).exists()


def test_a_wrong_archive_size_aborts_and_deletes_the_partial_file(pinned: Pinned) -> None:
    pinned.sections["zipped"].size += 1
    pinned.write()

    with pytest.raises(upstream.UpstreamError, match="size"):
        pinned.fetch()

    assert not pinned.archive("zipped").exists()


def test_a_wrong_member_sha256_aborts_and_removes_the_extracted_file(pinned: Pinned) -> None:
    pinned.sections["tarred"].members["fixture-fonts-ttf-2.37/LICENSE"] = "1" * 64
    pinned.write()

    with pytest.raises(upstream.UpstreamError, match="LICENSE"):
        pinned.fetch()

    assert not pinned.extracted("tarred", "fixture-fonts-ttf-2.37/LICENSE").exists()
    assert not (pinned.upstream_dir / upstream.VERIFIED_NAME).exists()


def test_a_member_missing_from_the_archive_is_named_in_the_error(pinned: Pinned) -> None:
    pinned.sections["zipped"].members["fonts/variable/Nonesuch.ttf"] = "2" * 64
    pinned.write()

    with pytest.raises(upstream.UpstreamError, match="Nonesuch"):
        pinned.fetch()


def test_a_corrupt_archive_on_disk_is_downloaded_again(pinned: Pinned) -> None:
    pinned.fetch()
    pinned.archive("zipped").write_bytes(b"truncated")

    pinned.fetch()

    assert upstream.sha256_file(pinned.archive("zipped")) == pinned.sections["zipped"].sha256


def test_a_corrupt_member_on_disk_is_extracted_again(pinned: Pinned) -> None:
    pinned.fetch()
    member = pinned.extracted("zipped", "OFL.txt")
    member.write_bytes(b"tampered\n")

    pinned.fetch()

    assert member.read_bytes() == ZIP_MEMBERS["OFL.txt"]


def test_a_member_name_that_escapes_the_directory_is_refused(tmp_path: Path) -> None:
    served = tmp_path / "served"
    served.mkdir()
    archive = served / "escape.zip"
    make_zip(archive, {"../escape.txt": b"nope\n"})
    root = tmp_path / "repo"
    (root / "sources").mkdir(parents=True)
    section = Section(
        key="zipped",
        name="Escape",
        version="1",
        url=archive.as_uri(),
        sha256=sha256_bytes(archive.read_bytes()),
        size=archive.stat().st_size,
        members={"../escape.txt": sha256_bytes(b"nope\n")},
    )
    (root / "sources" / "upstream.toml").write_text(section.to_toml(), encoding="utf-8")

    with pytest.raises(upstream.UpstreamError, match="escapes"):
        upstream.fetch(root, quiet=True)

    assert not (root / "build" / "upstream" / "escape.txt").exists()


# --------------------------------------------------------------------------- #
# --offline
# --------------------------------------------------------------------------- #


def test_offline_verifies_a_warm_tree_without_touching_the_network(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = pinned.fetch()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("--offline opened a socket")

    monkeypatch.setattr(upstream.urllib.request, "urlopen", forbidden)
    assert pinned.fetch(offline=True) == expected


def test_offline_re_extracts_a_missing_member_from_the_cached_archive(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned.fetch()
    member = pinned.extracted("tarred", "fixture-fonts-ttf-2.37/AUTHORS")
    member.unlink()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("--offline opened a socket")

    monkeypatch.setattr(upstream.urllib.request, "urlopen", forbidden)
    pinned.fetch(offline=True)

    assert member.read_bytes() == TAR_MEMBERS["fixture-fonts-ttf-2.37/AUTHORS"]


def test_offline_on_a_cold_tree_fails_instead_of_downloading(
    pinned: Pinned, no_network: None
) -> None:
    with pytest.raises(upstream.UpstreamError, match="offline"):
        pinned.fetch(offline=True)


def test_offline_fails_when_a_member_is_gone_and_so_is_its_archive(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned.fetch()
    pinned.archive("zipped").unlink()
    pinned.extracted("zipped", "OFL.txt").unlink()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("--offline opened a socket")

    monkeypatch.setattr(upstream.urllib.request, "urlopen", forbidden)
    with pytest.raises(upstream.UpstreamError, match="offline"):
        pinned.fetch(offline=True)


def test_offline_refuses_an_archive_that_no_longer_matches_its_pin(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned.fetch()
    pinned.archive("zipped").write_bytes(b"truncated")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("--offline opened a socket")

    monkeypatch.setattr(upstream.urllib.request, "urlopen", forbidden)
    with pytest.raises(upstream.UpstreamError, match="offline"):
        pinned.fetch(offline=True)


# --------------------------------------------------------------------------- #
# The pin file itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ('name = "x"\n', "missing `version`"),
        ('name = "x"\nversion = "1"\n', "missing `url`"),
        ('name = "x"\nversion = "1"\nurl = "u"\nsha256 = "short"\nsize = 1\n', "sha256"),
        ('name = "x"\nversion = "1"\nurl = "u"\nsha256 = "' + "a" * 64 + '"\n', "size"),
        (
            'name = "x"\nversion = "1"\nurl = "u"\nsha256 = "' + "a" * 64 + '"\nsize = 1\n',
            "members",
        ),
    ],
)
def test_a_malformed_pin_is_rejected(tmp_path: Path, mutation: str, message: str) -> None:
    pin_file = tmp_path / "upstream.toml"
    pin_file.write_text(f"[broken]\n{mutation}", encoding="utf-8")

    with pytest.raises(upstream.UpstreamError, match=message):
        upstream.load_pins(pin_file)


def test_an_empty_pin_file_is_rejected(tmp_path: Path) -> None:
    pin_file = tmp_path / "upstream.toml"
    pin_file.write_text("# nothing pinned\n", encoding="utf-8")

    with pytest.raises(upstream.UpstreamError, match="no upstream sections"):
        upstream.load_pins(pin_file)


def test_a_member_digest_that_is_not_a_sha256_is_rejected(pinned: Pinned) -> None:
    pinned.sections["zipped"].members["OFL.txt"] = "not-a-digest"
    pinned.write()

    with pytest.raises(upstream.UpstreamError, match="OFL.txt"):
        upstream.load_pins(pinned.pin_file)


def test_the_checked_in_pins_are_release_assets_with_the_expected_members(
    repo_root: Path,
) -> None:
    """The real ``sources/upstream.toml``: shape only, no bytes fetched."""
    pins = {pin.key: pin for pin in upstream.load_pins(repo_root / "sources" / "upstream.toml")}

    assert set(pins) == {"literata", "dejavu"}
    assert (pins["literata"].name, pins["literata"].version) == ("Literata", "3.103")
    assert (pins["dejavu"].name, pins["dejavu"].version) == ("DejaVu Sans", "2.37")
    assert sum(len(pin.members) for pin in pins.values()) == 6

    for pin in pins.values():
        # Release assets only: GitHub's auto-generated archive/refs tarballs are
        # not stable, and api.github.com is not part of the build's world.
        assert "/releases/download/" in pin.url, pin.url
        assert "api.github.com" not in pin.url

    assert pins["literata"].archive_path(Path("u")) == Path("u/literata-3.103.zip")
    assert pins["dejavu"].archive_path(Path("u")) == Path("u/dejavu-2.37.tar.bz2")
    assert "fonts/variable/Literata[opsz,wght].ttf" in pins["literata"].members
    assert "dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf" in pins["dejavu"].members


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


def test_downloads_use_pythons_default_tls_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(upstream.RELAX_CA_ENV, raising=False)
    assert upstream._ssl_context() is None


def test_relaxing_ca_extension_checks_keeps_chain_and_hostname_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-in drops one RFC 5280 audit, not verification itself."""
    monkeypatch.setenv(upstream.RELAX_CA_ENV, "1")
    context = upstream._ssl_context()

    assert context is not None
    assert not context.verify_flags & ssl.VERIFY_X509_STRICT
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED


def test_a_failed_download_deletes_the_partial_file_and_explains_itself(
    pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(url: str, *args: object, **kwargs: object) -> None:
        raise urllib.error.URLError(
            ssl.SSLCertVerificationError("CA cert does not include key usage extension")
        )

    monkeypatch.setattr(upstream.urllib.request, "urlopen", refuse)
    with pytest.raises(upstream.UpstreamError, match=upstream.RELAX_CA_ENV):
        pinned.fetch()

    assert not pinned.archive("zipped").exists()


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_the_fetch_command_is_wired_to_this_module() -> None:
    assert COMMANDS["fetch"] is upstream.run
    assert not getattr(COMMANDS["fetch"], "is_stub", False)


def test_fetch_takes_an_offline_flag() -> None:
    assert build_parser().parse_args(["fetch"]).offline is False
    assert build_parser().parse_args(["fetch", "--offline"]).offline is True


def test_a_verification_failure_is_reported_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(**kwargs: object) -> None:
        raise upstream.UpstreamError("pinned sha256 does not match")

    monkeypatch.setattr(upstream, "fetch", boom)
    assert upstream.run(argparse.Namespace(offline=False)) == 1
    assert "pinned sha256 does not match" in capsys.readouterr().err


def test_read_verified_explains_itself_when_the_fetch_has_not_run(tmp_path: Path) -> None:
    with pytest.raises(upstream.UpstreamError, match="mise run fetch"):
        upstream.read_verified(tmp_path)
