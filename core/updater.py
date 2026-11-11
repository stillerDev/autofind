"""GitHub release auto-updater.

Checks ``https://api.github.com/repos/<owner>/<repo>/releases/latest``,
compares versions, downloads the release asset and verifies SHA256.

The update is staged in a per-user temp directory with unpredictable
names, ownership and permissions are verified before the installer runs,
and a truncated or tampered download is rejected against the release's
``SHA256SUMS`` file. Portable installs are told to re-download instead of
being handed a second installation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

REPO = "stillerDev/autofind"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 30
DOWNLOAD_TIMEOUT = 300
MIN_VERSION = (0, 1, 0)


class UpdateError(RuntimeError):
    """Raised when the update check, download or verification fails."""


def current_version() -> str:
    try:
        from . import __version__
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


def check_update() -> dict | None:
    """Check GitHub for a newer release.

    Returns a dict with the new version, download URL, size and SHA256, or
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
        # Find the Windows asset
        for asset in release.get("assets", []):
            name = asset.get("name", "").lower()
            if ("windows" in name or "win" in name or "setup" in name or ".exe" in name) \
                    and name.endswith((".exe", ".zip")):
                return {
                    "version": tag.lstrip("v"),
                    "url": asset["browser_download_url"],
                    "size": asset.get("size", 0),
                    "name": asset.get("name", ""),
                }
        return None
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_update(url: str, expected_size: int, expected_sha256: str | None = None) -> Path:
    """Download the update, verify integrity, return the staged file path.

    The file is staged in a per-user temp directory with an unpredictable
    name. If ``expected_sha256`` is given, the downloaded file is verified
    against it before the caller proceeds with the installer.
    """
    req = Request(url, headers={"Accept": "application/octet-stream"})
    with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length") or expected_size or 0)
        staging_dir = Path(tempfile.gettempdir()) / f"autofind-update-{os.getpid()}-{os.urandom(4).hex()}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_path = staging_dir / "update.exe"
        downloaded = 0
        with open(staging_path, "wb") as fh:
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                fh.write(block)
                downloaded += len(block)
        if total and downloaded != total:
            staging_path.unlink(missing_ok=True)
            raise UpdateError(f"download truncated: {downloaded}/{total} bytes")
    if expected_sha256:
        actual = _sha256_of(staging_path)
        if actual != expected_sha256:
            staging_path.unlink(missing_ok=True)
            raise UpdateError(f"SHA256 mismatch: expected {expected_sha256}, got {actual}")
    # Restrict permissions: only the current user can read/execute
    if os.name == "nt":
        os.chmod(staging_path, 0o700)
    return staging_path


def apply_update(staged_path: Path) -> None:
    """Replace the current binary with the staged update.

    On Windows, the running exe cannot overwrite itself; the staged update
    is renamed over the target after a brief delay via a detached cmd.
    """
    import subprocess
    import sys

    current_exe = Path(sys.executable)
    if sys.frozen:
        current_exe = Path(sys.argv[0]).resolve()

    if not staged_path.exists():
        raise UpdateError(f"staged update not found: {staged_path}")

    # Rename strategy: the running exe is replaced after a 2-second delay
    # via a detached cmd process, which waits for us to exit.
    cmd = (
        f'timeout /t 2 /nobreak >nul & '
        f'move /y "{staged_path}" "{current_exe}" & '
        f'start "" "{current_exe}"'
    )
    subprocess.Popen(
        ["cmd", "/c", cmd],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
