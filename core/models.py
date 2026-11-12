"""Model pack downloads: pinned artifacts, verified, mirror-backed.

The embedder layers expect weights that were never shipped in the wheel
(they add ~135 MB): `models/e5-small/` for the ONNX transformer. Until now
the only way to get them was a manual drop-in, which is a barrier nobody
should cross twice. This module mirrors `core/ocr/indra.py`'s downloader and
Kensaku's `models.rs` policy:

* every artifact is pinned by byte length and SHA-256, so a truncated,
  tampered or wrong-revision download can never be installed;
* the revision (a full commit hash) is part of the URL, so a repository
  update cannot silently change the bytes behind the pins;
* each mirror serves the same pinned bytes, huggingface first, hf-mirror as
  the fallback for networks where HF is blocked;
* files land as `.part` and are renamed only after verification, so a killed
  download never leaves a plausible-looking half-model behind;
* an existing file with the right checksum is kept, so an interrupted run
  resumes instead of re-downloading.

The license note that applies to Indra applies here too: the weights come
from Hugging Face under their own terms and are downloaded on demand — never
bundled into the app or its installers.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# E5 pack: int8-quantized multilingual-e5-small export plus its tokenizer,
# pinned to the Xenova ONNX export at a fixed commit. `model_int8.onnx` on
# the hub is installed as `model.onnx` — the name the embedder looks for.
_REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"

_PACK_URLS = (
    f"https://huggingface.co/Xenova/multilingual-e5-small/resolve/{_REVISION}/{{name}}",
    # Mirror fallback: huggingface is blocked on some networks.
    f"https://hf-mirror.com/Xenova/multilingual-e5-small/resolve/{_REVISION}/{{name}}",
)


@dataclass(frozen=True)
class Artifact:
    """One pack file, pinned: the remote path, the local name, byte length
    and SHA-256."""

    remote: str
    name: str
    bytes_expected: int
    sha256: str


@dataclass(frozen=True)
class Pack:
    """A named set of artifacts sharing one destination directory."""

    name: str
    directory: str
    artifacts: tuple[Artifact, ...]
    why: str


E5_ARTIFACTS: tuple[Artifact, ...] = (
    Artifact(
        "onnx/model_int8.onnx",
        "model.onnx",
        118_054_593,
        "4d24e2bc01a447951524466ef533e52944bf48509e6552810bcee1a2711cb02c",
    ),
    Artifact(
        "tokenizer.json",
        "tokenizer.json",
        17_082_730,
        "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    ),
)

PACKS: dict[str, Pack] = {
    "e5": Pack(
        "e5",
        "e5-small",
        E5_ARTIFACTS,
        "semantic text embeddings (the transformer behind `--embedder e5`)",
    ),
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def pack_dir(pack: str) -> Path:
    """Where the pack lives. `AUTOFIND_MODELS_DIR` overrides the root."""
    override = os.environ.get("AUTOFIND_MODELS_DIR")
    root = Path(override) if override else _default_models_dir()
    return root / PACKS[pack].directory


def _default_models_dir() -> Path:
    from .config import default_home

    return default_home() / "models"


def verify_pack(pack: str) -> str | None:
    """Why the pack on disk cannot be trusted yet, or `None` when it can. A
    half-downloaded pack is present and useless: doctor and `model status`
    must tell them apart."""
    directory = pack_dir(pack)
    for artifact in PACKS[pack].artifacts:
        path = directory / artifact.name
        if not path.is_file():
            return f"missing {artifact.name}"
        try:
            size = path.stat().st_size
        except OSError:
            return f"{artifact.name}: unreadable"
        if size != artifact.bytes_expected:
            return f"{artifact.name}: {size} bytes, expected {artifact.bytes_expected}"
        if file_sha256(path) != artifact.sha256:
            return f"{artifact.name}: checksum mismatch"
    return None


def pack_status(pack: str) -> dict:
    """What a user can act on, for the CLI and doctor."""
    spec = PACKS[pack]
    directory = pack_dir(pack)
    problem = verify_pack(pack)
    return {
        "available": problem is None,
        "pack_dir": str(directory),
        "why": spec.why,
        "problem": problem,
    }


def download_pack(pack: str, progress=None) -> Path:
    """Download and verify every pack file. Returns the pack directory.

    An existing file with the right checksum is kept, so an interrupted run
    resumes. Each file downloads to a `.part` temp file and is renamed in
    place after verification — a killed download never leaves a plausible
    looking half-model behind.
    """
    directory = pack_dir(pack)
    directory.mkdir(parents=True, exist_ok=True)
    for artifact in PACKS[pack].artifacts:
        path = directory / artifact.name
        if path.is_file() and path.stat().st_size == artifact.bytes_expected:
            if file_sha256(path) == artifact.sha256:
                continue
            path.unlink()  # wrong bytes on disk: redownload
        errors: list[str] = []
        for url in _PACK_URLS:
            target = url.format(name=artifact.remote)
            try:
                if progress:
                    progress(artifact.name, 0, artifact.bytes_expected)
                tmp = path.with_name(path.name + ".part")
                with urllib.request.urlopen(target, timeout=120) as response, open(tmp, "wb") as out:
                    total = 0
                    digest = hashlib.sha256()
                    while True:
                        block = response.read(1 << 20)
                        if not block:
                            break
                        out.write(block)
                        digest.update(block)
                        total += len(block)
                        if progress:
                            progress(artifact.name, total, artifact.bytes_expected)
                if total != artifact.bytes_expected or digest.hexdigest() != artifact.sha256:
                    raise RuntimeError(f"checksum mismatch after download ({total} bytes)")
                tmp.replace(path)
                break
            except Exception as exc:  # noqa: BLE001 - try the next mirror
                errors.append(f"{url.split('/')[2]}: {exc}")
                try:
                    path.with_name(path.name + ".part").unlink()
                except OSError:
                    pass
        else:
            raise RuntimeError(f"cannot download {artifact.name}: " + "; ".join(errors))
    return directory
