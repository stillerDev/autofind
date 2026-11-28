"""Package the committed tree as a release archive with SHA256SUMS.

kensaku ships a plain zip plus a SHA256SUMS file, and the benchmark could
verify its binary against them — AutoFind had no equivalent, so its bench
row honestly said "uncommitted working tree". This produces the same
contract from `git archive`: the exact committed tree (never a dirty
working tree), named with the version, plus a checksum file for it.

Usage:  python tools/make_release.py [--out DIR]
Writes  <out>/autofind-<version>.zip and <out>/SHA256SUMS
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def version() -> str:
    text = (ROOT / "core" / "__version__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("core/__version__.py does not define __version__")


def git(*args: str) -> bytes:
    done = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True)
    if done.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed: {done.stderr.decode(errors='replace')}")
    return done.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "dist"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ver = version()
    name = f"autofind-{ver}.zip"
    archive = out / name
    archive.write_bytes(
        git("archive", "--format=zip", f"--prefix=autofind-{ver}/", "HEAD"))
    status = git("status", "--porcelain").decode(
        encoding="utf-8", errors="replace").strip()
    if status:
        # the archive packages HEAD: a dirty tree must never quietly become
        # the release, that was the benchmark's apples-to-oranges mistake
        print(f"NOTE: the working tree is dirty; the archive packages HEAD:\n{status}")
    lines = [f"{sha256(artifact)}  {artifact.name}"
             for artifact in sorted(out.glob("autofind-*.zip"))]
    sums = out / "SHA256SUMS"
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # self-check the same way a downloader would verify them
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, _, fname = line.partition("  ")
        if not fname or sha256(out / fname) != digest:
            raise SystemExit(f"checksum self-check failed for {fname!r}")
    print(f"{name}: {archive.stat().st_size} bytes")
    print(f"sha256: {lines[-1].split()[0]}")
    print(f"wrote {sums}")
    print("checksum self-check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
