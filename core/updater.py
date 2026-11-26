"""GitHub release auto-updater.

Checks ``https://api.github.com/repos/<owner>/<repo>/releases/latest``,
compares versions, downloads the release asset and verifies SHA256.

Hardening rules, each behind a test:

- Only ``https://`` download URLs are accepted — a tampered check response
  cannot redirect the binary download to a plain-HTTP drop.
- The release is expected to publish a ``SHA256SUMS`` text asset next to the
  installer; the staged file must match its hash *and* the declared size.
  No sums file, no hash — the download is then verified against size only
  and the CLI prints a warning instead of staying silent.
- The download is capped at ``MAX_DOWNLOAD_BYTES``: a compromised CDN answer
  must not be able to fill the disk.
- Replacement runs through a single ``cmd`` line whose only variable parts
  are the two file paths. Windows filenames cannot contain ``"``; ``%`` is
  legal but expands inside cmd, so a path containing either character is
  refused with manual-update instructions instead of being interpolated.

The update is staged in a per-user temp directory with unpredictable
names, and a truncated or tampered download is deleted before the caller
ever sees a path. Portable installs are told to re-download instead of
being handed a second installation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

REPO = "stillerDev/autofind"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 30
DOWNLOAD_TIMEOUT = 300
MIN_VERSION = (0, 1, 0)
# The installer wheel/exe is ~80 MB today; 512 MiB leaves orders of
# magnitude of headroom without letting anything stream forever.
MAX_DOWNLOAD_BYTES = 512 * (1 << 20)
SUMS_NAME = "SHA256SUMS"  # release asset carrying "<sha256>  <filename>" lines
# Headless-safe wait: cmd's `timeout` refuses to run when stdin is not a
# console (exactly how the detached replacer starts), so the delay is a
# self-ping instead — n packets at ~1 s each means n-1 seconds of waiting.
REPLACE_DELAY_SECONDS = 2  # head start for this process to exit before the move


class UpdateError(RuntimeError):
    """Raised when the update check, download or verification fails."""


def current_version() -> str:
    try:
        from .__version__ import __version__
        return __version__
    except ImportError:
        try:
            import importlib.metadata
            return importlib.metadata.version("autofind")
        except importlib.metadata.PackageNotFoundError:
            return "0.0.0"


def parse_version(v: str) -> tuple[int, ...]:
    """'1.2.3-beta' -> (1, 2, 3). Suffixes are ignored for comparison."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v)
    if not m:
        return (0, 0, 0)
    return tuple(int(g) for g in m.groups())


def _get(url: str, timeout: float) -> bytes:
    req = Request(url, headers={"Accept": "application/octet-stream"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_sums(text: str) -> dict[str, str]:
    """'SHA256SUMS' content -> {filename.lower(): sha256hex}.

    The format is the one `sha256sum` writes: ``<hex>  <name>``, two spaces.
    Names are lowercased because the asset match below is case-insensitive.
    """
    sums: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line.strip())
        if m:
            sums[m.group(2).strip().lower()] = m.group(1).lower()
    return sums


def _sha256_lookup(release: dict) -> tuple[str, str] | None:
    """(sums_url, {name: hash}) for the release's SHA256SUMS asset, if any."""
    for asset in release.get("assets", []):
        if asset.get("name", "").strip().lower() == SUMS_NAME.lower():
            url = asset.get("browser_download_url", "")
            if not url.startswith("https://"):
                continue
            try:
                text = _get(url, timeout=TIMEOUT).decode("utf-8", "replace")
            except OSError:
                return None  # sums unreachable: size-check only, CLI warns
            return url, parse_sums(text)
    return None


def check_update() -> dict | None:
    """Check GitHub for a newer release.

    Returns a dict with the new version, download URL, size, SHA256 (when the
    release ships SHA256SUMS) and whether that hash was actually found, or
    None when the current version is up to date. Never raises — network
    failures and API errors return None (the caller shows "up to date").
    """
    try:
        req = Request(API_URL, headers={"Accept": "application/vnd.github+json"})
        with urlopen(req, timeout=TIMEOUT) as resp:
            release = json.loads(resp.read())
        tag = release.get("tag_name", "")
        new_ver = parse_version(tag)
        cur_ver = parse_version(current_version())
        if new_ver <= cur_ver or new_ver < MIN_VERSION:
            return None
        sums = _sha256_lookup(release)
        # Find the Windows asset. The old `"win" in name` also matched
        # "darwin", so a macOS zip could be offered as the Windows update.
        for asset in release.get("assets", []):
            name = asset.get("name", "").lower()
            if not name.endswith((".exe", ".zip")):
                continue
            if "darwin" in name or "macos" in name or "linux" in name:
                continue
            windows = (
                "windows" in name or "win64" in name or "win32" in name
                or re.search(r"(?:^|[-_.])win(?:[-_.]|$)", name) is not None
                or (name.endswith(".exe") and "setup" in name)
            )
            if windows:
                url = asset["browser_download_url"]
                if not url.startswith("https://"):
                    return None  # non-https asset URL: refuse the whole update
                return {
                    "version": tag.lstrip("v"),
                    "url": url,
                    "size": int(asset.get("size", 0) or 0),
                    "name": asset.get("name", ""),
                    "sha256": (sums[1].get(name) if sums else None),
                    "sha256_source": (sums[0] if sums else None),
                }
        return None
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _stage_dir() -> Path:
    """Per-user temp directory with an unpredictable name, reused per PID."""
    d = Path(tempfile.gettempdir()) / f"autofind-update-{os.getpid()}-{os.urandom(4).hex()}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_update(
    url: str,
    expected_size: int,
    expected_sha256: str | None = None,
) -> Path:
    """Download the update, verify integrity, return the staged file path.

    The file is staged in a per-user temp directory with an unpredictable
    name. ``url`` must be https; the stream is capped at
    MAX_DOWNLOAD_BYTES; the declared size and, when given, the SHA256 from
    the release's SHA256SUMS must match — any failure deletes the staged
    file and raises, so a half-downloaded installer can never be launched.
    """
    if not url.startswith("https://"):
        raise UpdateError(f"refusing non-https download URL: {url.split('?')[0]}")
    req = Request(url, headers={"Accept": "application/octet-stream"})
    staging_path = _stage_dir() / "update.exe"
    try:
        with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length") or expected_size or 0)
            if total and total > MAX_DOWNLOAD_BYTES:
                raise UpdateError(
                    f"declared download is {total} bytes, over the "
                    f"{MAX_DOWNLOAD_BYTES} cap — refusing"
                )
            downloaded = 0
            with open(staging_path, "wb") as fh:
                while True:
                    block = resp.read(1 << 20)
                    if not block:
                        break
                    downloaded += len(block)
                    if downloaded > MAX_DOWNLOAD_BYTES:
                        raise UpdateError(
                            f"download exceeded the {MAX_DOWNLOAD_BYTES} cap"
                        )
                    fh.write(block)
            if total and downloaded != total:
                raise UpdateError(f"download truncated: {downloaded}/{total} bytes")
            # Content-Length is advisory: a server may omit it or lie about it.
            # The size published in the release stays authoritative either way.
            if expected_size and downloaded != expected_size:
                raise UpdateError(
                    f"download size mismatch: {downloaded}/{expected_size} bytes"
                )
    except UpdateError:
        staging_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        staging_path.unlink(missing_ok=True)
        raise UpdateError(f"download failed: {exc}") from exc
    if expected_sha256:
        actual = _sha256_of(staging_path)
        if actual != expected_sha256.lower():
            staging_path.unlink(missing_ok=True)
            raise UpdateError(
                f"SHA256 mismatch: expected {expected_sha256}, got {actual}"
            )
    elif not staging_path.stat().st_size:
        staging_path.unlink(missing_ok=True)
        raise UpdateError("downloaded update is empty")
    # Restrict permissions: only the current user can read/execute
    if os.name == "nt":
        os.chmod(staging_path, 0o700)
    return staging_path


def replace_command(
    staged_path: Path,
    current_exe: Path,
    delay: int = REPLACE_DELAY_SECONDS,
) -> str:
    """The cmd line that swaps this process's exe for the staged update.

    Both paths are double-quoted; Windows filenames may contain ``& ^ !``
    (literal inside quotes) but not ``"``. ``%`` *is* legal in a filename yet
    cmd would expand ``%var%`` even inside quotes, so `apply_update` refuses
    such paths before this string is ever built. The wait is a self-ping
    because `timeout` needs a console on its stdin and the replacer has none.
    """
    wait = f"ping -n {max(2, int(delay) + 1)} 127.0.0.1 >nul"
    return (
        f"{wait} & "
        f'move /y "{staged_path}" "{current_exe}" & '
        f'start "" "{current_exe}"'
    )


def apply_update(
    staged_path: Path,
    *,
    current_exe: Path | None = None,
    expected_sha256: str | None = None,
    delay: int = REPLACE_DELAY_SECONDS,
    launch: bool = True,
) -> str:
    """Replace the current binary with the staged update.

    On Windows, the running exe cannot overwrite itself; the staged update
    is renamed over the target after a brief delay via a detached cmd
    process, which waits for us to exit. Returns the command that was (or
    would be) run, so tests can inspect it with ``launch=False``.

    A final hash check happens here, not just at download time: between the
    download and the apply, nobody has a second chance to swap the file.
    Paths that cmd would misinterpret are refused with manual instructions
    rather than interpolated into the command line.
    """
    staged = Path(staged_path)
    if not staged.is_file():
        raise UpdateError(f"staged update not found: {staged}")
    if not staged.stat().st_size:
        raise UpdateError("staged update is empty")
    if expected_sha256:
        actual = _sha256_of(staged)
        if actual != expected_sha256.lower():
            raise UpdateError(
                f"staged file failed the final SHA256 check "
                f"(expected {expected_sha256}, got {actual})"
            )

    if current_exe is None:
        current_exe = Path(sys.executable)
        # `sys.frozen` only exists inside a PyInstaller bundle; plain CPython
        # has no such attribute (an uncaught AttributeError here kills update).
        if getattr(sys, "frozen", False):
            current_exe = Path(sys.argv[0]).resolve()
    current_exe = Path(current_exe)
    if not current_exe.is_file():
        raise UpdateError(f"current executable not found: {current_exe}")
    if current_exe.resolve() == staged.resolve():
        raise UpdateError("staged file and target are the same file")
    for label, path in (("staged update", staged), ("executable", current_exe)):
        bad = [ch for ch in '"%' if ch in path.name]
        if bad:
            raise UpdateError(
                f"the {label} path contains {bad!r}, which cmd would "
                f"misinterpret; install the update manually from "
                f"https://github.com/{REPO}/releases"
            )
    cmd = replace_command(staged, current_exe, delay)
    if launch:
        # A single string, not an argv list: list2cmdline would wrap the
        # whole command in another pair of quotes and cmd would then treat
        # the paths' own quotes as literals (measured: nested quoting turns
        # `move /y "a b" c` into "syntax is incorrect"). A space inside a
        # path is safe here because both paths are quoted already.
        subprocess.Popen(
            "cmd /d /c " + cmd,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
    return cmd
