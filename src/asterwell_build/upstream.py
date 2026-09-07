"""Fetch, verify and extract the pinned upstream archives.

``sources/upstream.toml`` is the single source of truth for what the build
consumes: one section per upstream project pinning the release asset's URL,
byte size and sha256, plus the exact archive members the build reads and a
sha256 for each. This module turns that file into ``build/upstream/``::

    build/upstream/literata-3.103.zip                              archive
    build/upstream/literata/OFL.txt                                members
    build/upstream/literata/fonts/variable/Literata[opsz,wght].ttf
    build/upstream/dejavu-2.37.tar.bz2
    build/upstream/dejavu/dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf
    build/upstream/.verified                     JSON manifest of every hash

The archives are data, not tools, so they are fetched by this task rather than
by a package manager: a plain release-asset URL streamed to disk with
:mod:`urllib.request` while :mod:`hashlib` digests it on the fly. No GitHub API
call is involved, and nothing but the pinned bytes is trusted — a size or
digest that does not match the pin deletes the file and aborts.

Members are read with :mod:`zipfile`/:mod:`tarfile` and addressed by exact
name; shelling out to ``unzip`` would glob the ``[`` in Literata's file names.

Re-running is cheap: an archive or member already on disk that hashes correctly
is left alone, so a second ``asterwell-build fetch`` does no network I/O at all.
``asterwell-build fetch --offline`` goes further and never opens a socket — it
verifies what is on disk and fails if anything is missing, which is the CI
cache-hit path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import tarfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

#: Where progress lines go; :func:`_logger` builds one, tests ignore them.
Log = Callable[[str], None]

__all__ = [
    "Pin",
    "UpstreamError",
    "fetch",
    "load_pins",
    "read_verified",
    "sha256_file",
]

#: Version of the ``.verified`` manifest layout; bumped if its shape changes.
MANIFEST_SCHEMA = 1

#: Name of the manifest written once every pinned hash has been verified.
VERIFIED_NAME = ".verified"

#: Read size for streaming downloads and extractions.
CHUNK_SIZE = 1 << 20

#: Socket timeout, in seconds, for a single read from the network.
NETWORK_TIMEOUT = 120

#: Multi-part archive extensions, longest first, so ``.tar.bz2`` wins over
#: ``.bz2`` when naming the local copy of a release asset.
ARCHIVE_SUFFIXES = (".tar.bz2", ".tar.gz", ".tar.xz", ".tar", ".zip")

#: Opt-in relaxation of one certificate audit; see :func:`_ssl_context`.
RELAX_CA_ENV = "ASTERWELL_FETCH_RELAX_CA_EXTENSIONS"


class UpstreamError(Exception):
    """A pin is malformed, or the bytes on disk do not match it."""


@dataclass(frozen=True)
class Pin:
    """One pinned upstream release asset and the members the build reads."""

    key: str
    """Section name in ``upstream.toml``; also the local file/directory stem."""

    name: str
    version: str
    url: str
    sha256: str
    size: int
    members: Mapping[str, str]
    """Path inside the archive → sha256 of that member's bytes."""

    license: str | None = None
    repo: str | None = None
    ref: str | None = None
    """The pinned commit or tag, whichever the section carries."""

    @property
    def suffix(self) -> str:
        """Archive extension taken from the pinned URL (``.tar.bz2``, ``.zip``)."""
        filename = self.url.rsplit("/", 1)[-1]
        for suffix in ARCHIVE_SUFFIXES:
            if filename.endswith(suffix):
                return suffix
        raise UpstreamError(
            f"[{self.key}] url does not end in a supported archive extension "
            f"({', '.join(ARCHIVE_SUFFIXES)}): {self.url}"
        )

    def archive_path(self, upstream_dir: Path) -> Path:
        """Where the release asset itself is kept."""
        return upstream_dir / f"{self.key}-{self.version}{self.suffix}"

    def extract_dir(self, upstream_dir: Path) -> Path:
        """Directory the pinned members are extracted into, keeping their paths."""
        return upstream_dir / self.key


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def default_root() -> Path:
    """Repository root.

    Normally the checkout this package was installed from in editable mode
    (``uv sync``); falls back to the working directory, which is where the
    ``mise`` tasks run from, if this file is not living inside the checkout.
    """
    root = Path(__file__).resolve().parents[2]
    if (root / "sources" / "upstream.toml").is_file():
        return root
    return Path.cwd()


def pin_file_for(root: Path) -> Path:
    return root / "sources" / "upstream.toml"


def upstream_dir_for(root: Path) -> Path:
    return root / "build" / "upstream"


def read_verified(root: Path | None = None) -> dict[str, object]:
    """Load ``build/upstream/.verified``; raise if the fetch has not run."""
    path = upstream_dir_for(root if root is not None else default_root()) / VERIFIED_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UpstreamError(
            f"{path} is missing — run `mise run fetch` to download and verify the "
            "pinned upstream archives"
        ) from exc


# --------------------------------------------------------------------------- #
# Pin file
# --------------------------------------------------------------------------- #


def load_pins(pin_file: Path) -> list[Pin]:
    """Parse and validate ``upstream.toml``, in file order."""
    try:
        raw = tomllib.loads(pin_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UpstreamError(f"missing pin file: {pin_file}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise UpstreamError(f"{pin_file}: not valid TOML: {exc}") from exc

    pins = [_pin_from_section(pin_file, key, section) for key, section in raw.items()]
    if not pins:
        raise UpstreamError(f"{pin_file}: no upstream sections")
    return pins


def _pin_from_section(pin_file: Path, key: str, section: object) -> Pin:
    where = f"{pin_file}: [{key}]"
    if not isinstance(section, dict):
        raise UpstreamError(f"{where} is not a table")

    def string(field: str, *, required: bool = True) -> str | None:
        value = section.get(field)
        if value is None:
            if required:
                raise UpstreamError(f"{where} is missing `{field}`")
            return None
        if not isinstance(value, str) or not value:
            raise UpstreamError(f"{where} `{field}` must be a non-empty string")
        return value

    # Validated in the order a hand-edited pin is most usefully corrected.
    name = string("name")
    version = string("version")
    url = string("url")
    sha256 = _checked_digest(str(string("sha256")), f"{where} `sha256`")

    size = section.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise UpstreamError(f"{where} `size` must be a positive integer")

    members_raw = section.get("members")
    if not isinstance(members_raw, dict) or not members_raw:
        raise UpstreamError(f"{where} needs a non-empty `members` table")
    members: dict[str, str] = {}
    for member, digest in members_raw.items():
        if not isinstance(digest, str):
            raise UpstreamError(f"{where} member `{member}` must map to a sha256 string")
        members[member] = _checked_digest(digest, f"{where} member `{member}`")

    return Pin(
        key=key,
        name=str(name),
        version=str(version),
        url=str(url),
        sha256=sha256,
        size=size,
        members=members,
        license=string("license", required=False),
        repo=string("repo", required=False),
        ref=string("commit", required=False) or string("tag", required=False),
    )


def _checked_digest(value: str, where: str) -> str:
    digest = value.strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise UpstreamError(f"{where} is not a sha256 hex digest: {value!r}")
    return digest


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #


def sha256_file(path: Path) -> str:
    """sha256 of a file's bytes, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream(source: BinaryIO, destination: Path) -> tuple[str, int]:
    """Copy ``source`` to ``destination``, returning its sha256 and byte count."""
    digest = hashlib.sha256()
    written = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as out:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            written += len(chunk)
            out.write(chunk)
    return digest.hexdigest(), written


def _verifies(path: Path, sha256: str, size: int | None = None) -> bool:
    """True when the file exists with the pinned size and digest."""
    if not path.is_file():
        return False
    if size is not None and path.stat().st_size != size:
        return False
    return sha256_file(path) == sha256


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #


def _ssl_context() -> ssl.SSLContext | None:
    """TLS settings for downloads; ``None`` leaves urllib on its own default.

    Python 3.13's default context turns on ``VERIFY_X509_STRICT``, an RFC 5280
    audit of the certificates in the chain. It rejects a CA certificate that
    omits the ``keyUsage`` extension — which is how some TLS-intercepting
    corporate proxies mint their root, so `fetch` cannot reach github.com from
    behind one at all. Setting ``ASTERWELL_FETCH_RELAX_CA_EXTENSIONS=1`` drops
    *only* that audit: the chain is still built to a trusted root and the
    hostname is still verified, exactly as on Python 3.12 and as ``curl`` does.

    It is deliberately opt-in and never affects what the build accepts: an
    archive is trusted for its pinned size and sha256, never for its transport.
    """
    if os.environ.get(RELAX_CA_ENV, "").strip().lower() not in ("1", "true", "yes"):
        return None
    context = ssl.create_default_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def _tls_hint(exc: BaseException) -> str:
    """Point at :data:`RELAX_CA_ENV` when a download died on certificate checks."""
    reason = getattr(exc, "reason", None)
    if isinstance(exc, ssl.SSLCertVerificationError) or isinstance(
        reason, ssl.SSLCertVerificationError
    ):
        return (
            "\n  hint: behind a TLS-intercepting proxy whose CA omits the keyUsage "
            f"extension, set {RELAX_CA_ENV}=1 — chain and hostname verification stay on"
        )
    return ""


def _download(pin: Pin, destination: Path, log: Log) -> None:
    """Stream the pinned URL to ``destination``, verifying size and sha256.

    A mismatch removes the file just written: a half-downloaded or wrong
    archive must never be left behind for the next run to trust.
    """
    log(f"  downloading {pin.url}")
    try:
        with urllib.request.urlopen(
            pin.url, timeout=NETWORK_TIMEOUT, context=_ssl_context()
        ) as response:
            digest, size = _stream(response, destination)
    except urllib.error.HTTPError as exc:
        destination.unlink(missing_ok=True)
        raise UpstreamError(f"[{pin.key}] {pin.url}: HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise UpstreamError(f"[{pin.key}] {pin.url}: {exc}{_tls_hint(exc)}") from exc

    if size != pin.size:
        destination.unlink(missing_ok=True)
        raise UpstreamError(
            f"[{pin.key}] {pin.url}: pinned size {pin.size} bytes, got {size}; "
            f"deleted {destination}"
        )
    if digest != pin.sha256:
        destination.unlink(missing_ok=True)
        raise UpstreamError(
            f"[{pin.key}] {pin.url}: pinned sha256 {pin.sha256}, got {digest}; "
            f"deleted {destination}"
        )
    log(f"  verified {destination.name} ({size} bytes)")


def _ensure_archive(pin: Pin, upstream_dir: Path, *, offline: bool, log: Log) -> Path | None:
    """Return the verified archive, downloading it unless it is already good.

    ``None`` means "not on disk and we are offline" — tolerable as long as
    every pinned member is already extracted and verifies.
    """
    archive = pin.archive_path(upstream_dir)
    if _verifies(archive, pin.sha256, pin.size):
        log(f"  {archive.name} already verified")
        return archive
    if offline:
        if archive.exists():
            raise UpstreamError(
                f"[{pin.key}] {archive} does not match the pin (size/sha256) and "
                "--offline forbids re-downloading it"
            )
        return None
    _download(pin, archive, log=log)
    return archive


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def _destination_for(extract_dir: Path, member: str) -> Path:
    """Where a member lands, refusing any name that escapes ``extract_dir``."""
    destination = (extract_dir / member).resolve()
    if not destination.is_relative_to(extract_dir.resolve()):
        raise UpstreamError(f"member name escapes {extract_dir}: {member!r}")
    return destination


class _ArchiveReader:
    """Just enough of an archive to read one named member as a stream."""

    def __enter__(self) -> _ArchiveReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def open_member(self, name: str) -> BinaryIO:  # pragma: no cover - overridden
        raise NotImplementedError


class _ZipReader(_ArchiveReader):
    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            self._zip = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise UpstreamError(f"{path}: not a readable zip archive: {exc}") from exc

    def close(self) -> None:
        self._zip.close()

    def open_member(self, name: str) -> BinaryIO:
        try:
            return self._zip.open(name)
        except KeyError as exc:
            raise UpstreamError(f"{self._path}: no member named {name!r}") from exc


class _TarReader(_ArchiveReader):
    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            self._tar = tarfile.open(path, "r:*")
        except tarfile.TarError as exc:
            raise UpstreamError(f"{path}: not a readable tar archive: {exc}") from exc

    def close(self) -> None:
        self._tar.close()

    def open_member(self, name: str) -> BinaryIO:
        try:
            member = self._tar.getmember(name)
        except KeyError as exc:
            raise UpstreamError(f"{self._path}: no member named {name!r}") from exc
        stream = self._tar.extractfile(member)
        if stream is None:
            raise UpstreamError(f"{self._path}: member {name!r} is not a regular file")
        return stream


def _open_archive(pin: Pin, archive: Path) -> _ArchiveReader:
    """Reader for the archive kind the pinned URL's extension names."""
    return _ZipReader(archive) if pin.suffix == ".zip" else _TarReader(archive)


def _extract_member(
    pin: Pin,
    archive: Path,
    reader: _ArchiveReader,
    member: str,
    sha256: str,
    extract_dir: Path,
) -> Path:
    """Extract one member and verify it, removing the file if it is wrong."""
    destination = _destination_for(extract_dir, member)
    with reader.open_member(member) as stream:
        digest, _size = _stream(stream, destination)
    if digest != sha256:
        destination.unlink(missing_ok=True)
        raise UpstreamError(
            f"[{pin.key}] {archive.name}!{member}: pinned sha256 {sha256}, got "
            f"{digest}; deleted {destination}"
        )
    return destination


def _ensure_members(
    pin: Pin,
    archive: Path | None,
    upstream_dir: Path,
    *,
    offline: bool,
    log: Log,
) -> dict[str, Path]:
    """Make every pinned member present and verified under ``build/upstream``."""
    extract_dir = pin.extract_dir(upstream_dir)
    missing = {
        member: sha256
        for member, sha256 in pin.members.items()
        if not _verifies(_destination_for(extract_dir, member), sha256)
    }

    if missing and archive is None:
        raise UpstreamError(
            f"[{pin.key}] --offline: {len(missing)} pinned member(s) missing or "
            f"corrupt under {extract_dir} and {pin.archive_path(upstream_dir)} is not "
            "on disk to re-extract them from; run `mise run fetch` with network access"
        )
    if missing and offline:
        log(f"  re-extracting {len(missing)} member(s) from the cached archive")

    if missing:
        assert archive is not None
        with _open_archive(pin, archive) as reader:
            for member, sha256 in missing.items():
                _extract_member(pin, archive, reader, member, sha256, extract_dir)

    for member in pin.members:
        log(f"  verified {extract_dir.name}/{member}")
    return {member: _destination_for(extract_dir, member) for member in pin.members}


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def _logger(quiet: bool) -> Log:
    def log(message: str) -> None:
        if not quiet:
            print(message)

    return log


def fetch(
    root: Path | None = None,
    *,
    offline: bool = False,
    pin_file: Path | None = None,
    quiet: bool = False,
) -> dict[str, object]:
    """Download (unless offline), verify and extract every pin.

    Returns the manifest that was written to ``build/upstream/.verified``. The
    manifest is written only once every archive and member has been verified,
    so its presence means the whole tree matches ``sources/upstream.toml``.
    """
    root = root if root is not None else default_root()
    pin_file = pin_file if pin_file is not None else pin_file_for(root)
    upstream_dir = upstream_dir_for(root)
    log = _logger(quiet)

    pins = load_pins(pin_file)
    upstream_dir.mkdir(parents=True, exist_ok=True)

    archives: dict[str, object] = {}
    member_count = 0
    for pin in pins:
        log(f"{pin.key}: {pin.name} {pin.version}")
        archive = _ensure_archive(pin, upstream_dir, offline=offline, log=log)
        paths = _ensure_members(pin, archive, upstream_dir, offline=offline, log=log)
        member_count += len(paths)
        entry: dict[str, object] = {
            "name": pin.name,
            "version": pin.version,
            "url": pin.url,
            "sha256": pin.sha256,
            "size": pin.size,
            "archive": (
                str(archive.relative_to(upstream_dir)) if archive is not None else None
            ),
            "dir": pin.key,
            "members": dict(pin.members),
        }
        for field, value in (("license", pin.license), ("repo", pin.repo), ("ref", pin.ref)):
            if value is not None:
                entry[field] = value
        archives[pin.key] = entry

    manifest: dict[str, object] = {"schema": MANIFEST_SCHEMA, "archives": archives}
    verified = upstream_dir / VERIFIED_NAME
    verified.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"verified {member_count} member(s) from {len(pins)} archive(s) → {verified}")
    return manifest


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``fetch``'s own options (called from :mod:`asterwell_build.cli`)."""
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "verify what is already in build/upstream without opening a socket "
            "(the CI cache-hit path); fails if anything is missing"
        ),
    )


def run(args: argparse.Namespace) -> int:
    """``asterwell-build fetch`` — see :func:`fetch`."""
    try:
        fetch(offline=getattr(args, "offline", False))
    except UpstreamError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
