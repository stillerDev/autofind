"""Environment and bundle check.

The plan promises a <=150 MB installer and the document itself lists 120 MB of
e5 weights, so the number is worth measuring rather than assuming. `doctor`
prints what is actually present, what each missing piece would add, and whether
the sum still fits the budget.
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from pathlib import Path

from .config import IndexConfig
from .embed import get_embedder, get_visual_embedder
from .ocr import engine_report
from .privacy import describe as describe_privacy
from .store import Store

BUDGET_MB = 150

# Rough sizes of the parts that always ship. The Python runtime is an embedded
# distribution (~15 MB zipped), numpy and Pillow are wheels, onnxruntime is the
# heavy one. Measured on the build machine, not guessed from memory.
RUNTIME_PARTS = (
    ("python-embed", 15, "runtime"),
    ("numpy", 18, "runtime"),
    ("pillow", 4, "runtime"),
    ("winrt bindings", 3, "runtime"),
    ("onnxruntime", 50, "optional: only with e5/CLIP"),
    ("app code + web", 1, "runtime"),
)


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _model_files(model_dir: Path) -> dict:
    if not model_dir.exists():
        return {}
    found = {}
    for item in sorted(model_dir.rglob("*")):
        if item.is_file():
            found[str(item.relative_to(model_dir))] = item.stat().st_size
    return found


def estimate_bundle(config: IndexConfig, include_onnx: bool = True) -> dict:
    parts = []
    for name, size, kind in RUNTIME_PARTS:
        if name == "onnxruntime" and not include_onnx:
            continue
        parts.append({"part": name, "mb": size, "kind": kind})

    manifest_path = Path(__file__).resolve().parent.parent / "models" / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")).get("models", {})

    present = _model_files(config.model_dir)
    for name, meta in manifest.items():
        actual = sum(size for path, size in present.items() if name.split("-")[0] in path.lower())
        mb = round(actual / 1048576, 1) if actual else float(meta.get("size_mb") or 0)
        parts.append(
            {
                "part": f"model:{name}",
                "mb": mb,
                "kind": "present" if actual else "download or bundle",
                "note": meta.get("role", ""),
            }
        )

    total = round(sum(part["mb"] for part in parts), 1)
    return {
        "parts": parts,
        "total_mb": total,
        "budget_mb": BUDGET_MB,
        "fits": total <= BUDGET_MB,
        "over_by_mb": round(max(0.0, total - BUDGET_MB), 1),
    }


def _app_report() -> dict:
    """Is the desktop window actually possible here?

    Without pywebview the app silently falls back to a browser tab, which is a
    different product. Better to say so than to let someone wonder why it looks
    like a website.
    """
    from .app import available

    window = available()
    out = {"window": window, "tray": False, "hotkey": False, "version": None}
    try:
        import importlib.metadata as metadata

        out["version"] = metadata.version("pywebview")
    except Exception:
        pass
    if window:
        for module, key in (("pystray", "tray"), ("pynput", "hotkey")):
            try:
                __import__(module)
                out[key] = True
            except Exception:
                out[key] = False
    return out


def collect(config: IndexConfig, db_path: Path | None = None) -> dict:
    db_path = db_path or config.db_path
    text_embedder = get_embedder("auto", config.model_dir)
    visual = get_visual_embedder(config.model_dir)

    index = {"path": str(db_path), "exists": db_path.exists(), "bytes": 0, "files": 0,
             "chunks": 0, "images": 0, "vectors": 0, "backend": None, "pack_bytes": 0}
    if db_path.exists():
        index["bytes"] = db_path.stat().st_size
        try:
            store = Store(db_path, text_embedder.name, text_embedder.dim, readonly=True,
                          vector_backend=config.vector_backend)
            try:
                stats = store.stats()
                index["backend"] = store.vector_backend
            finally:
                store.close()
            index.update({key: stats[key] for key in ("files", "chunks", "images", "ocr_images", "vectors")})
            pack = Path(str(db_path) + ".vec")
            if pack.exists():
                index["pack_bytes"] = pack.stat().st_size
        except Exception as exc:  # a broken db must not crash doctor
            index["error"] = f"{type(exc).__name__}: {exc}"

    return {
        "version": "0.1.0",
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}",
        "exe": sys.executable,
        "index": index,
        "text_embedder": text_embedder.name,
        "visual_embedder": visual.name,
        "visual_supports_text": bool(getattr(visual, "supports_text", False)),
        "ocr": engine_report(config.ocr_languages),
        "app": _app_report(),
        "privacy": {"enabled": config.privacy, **describe_privacy()},
        "models_dir": str(config.model_dir),
        "models": {path: size for path, size in _model_files(config.model_dir).items()},
        "bundle": estimate_bundle(config),
    }


def format_report(data: dict) -> str:
    index = data["index"]
    ocr = data["ocr"]
    privacy = data["privacy"]
    bundle = data["bundle"]

    lines = [
        f"semfind      {data['version']}",
        f"python       {data['python']}  ({data['platform']})",
        "",
        f"index        {index['path']}",
        f"             {index['bytes'] / 1048576:.1f} MB · "
        f"{index.get('files', 0)} files · {index.get('chunks', 0)} chunks · "
        f"{index.get('images', 0)} images",
        f"vectors      {index.get('vectors', 0)} · backend {index.get('backend') or '—'}"
        + (f" · pack {index['pack_bytes'] / 1048576:.0f} MB" if index.get("pack_bytes") else ""),
        "",
        f"text         {data['text_embedder']}",
        f"visual       {data['visual_embedder']}"
        + ("" if data["visual_supports_text"] else "   (no text-to-image: needs CLIP)"),
        f"ocr          {ocr['active'] or 'none'}"
        + (f"   available: {', '.join(ocr['available'])}" if ocr["available"] else ""),
        f"ocr cjk      {', '.join(ocr.get('cjk', {}).get('scripts') or []) or 'none'}"
        + (f"   via {', '.join(ocr['cjk']['engines'])}" if ocr.get("cjk", {}).get("engines") else ""),
        "",
        f"app window   {'yes' if data['app']['window'] else 'no (browser fallback)'}"
        + (f"   pywebview {data['app']['version']}" if data["app"].get("version") else "")
        + ("   tray" if data["app"]["tray"] else "")
        + ("   hotkey" if data["app"]["hotkey"] else ""),
        "",
        f"privacy      {'on' if privacy['enabled'] else 'OFF'}"
        f"   {len(privacy['sensitive_dirs'])} dirs, {len(privacy['sensitive_files'])} file patterns",
        "",
        f"bundle       {bundle['total_mb']:.1f} MB of {bundle['budget_mb']} MB budget"
        + ("  fits" if bundle["fits"] else f"  over by {bundle['over_by_mb']:.1f} MB"),
    ]
    for part in bundle["parts"]:
        lines.append(f"  {part['part']:<26} {part['mb']:>7.1f} MB   {part['kind']}")
    return "\n".join(lines)


def missing_tools() -> list[str]:
    tools = []
    for name in ("tesseract", "node", "cargo"):
        if shutil.which(name) is None:
            tools.append(name)
    return tools
