"""Fetch the CLIP ViT-B/32 ONNX export into the model directory.

Text-to-image search ("найди скриншот с красным графиком") needs three files
that are too heavy to bundle with every installer: the vision tower, the text
tower and the BPE tokenizer. They live in the Xenova/clip-vit-base-patch32
export of the MIT-licensed OpenAI model; this tool pins those exact files into
the model dir and verifies the pair end-to-end before declaring success.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

BASE = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main"
# The multilingual text tower (sentence-transformers/clip-ViT-B-32-multilingual-v1,
# MIT) shares the image tower's space, so Russian and Chinese queries work
# without a translation hop. Files land under their role names; ClipOnnx
# prefers textual_multi/tokenizer_multi when both are present.
FILES = {
    "visual.onnx": f"{BASE}/onnx/vision_model_quantized.onnx",
    "textual.onnx": f"{BASE}/onnx/text_model_quantized.onnx",
    "tokenizer.json": f"{BASE}/tokenizer.json",
    "textual_multi.onnx": "https://huggingface.co/aurantium/clip-ViT-B-32-multilingual-v1/resolve/main/onnx/model_quantized.onnx",
    "tokenizer_multi.json": "https://huggingface.co/aurantium/clip-ViT-B-32-multilingual-v1/resolve/main/tokenizer.json",
}


def _report(blocks: int, block_size: int, total: int) -> None:
    if total <= 0:
        return
    done = min(blocks * block_size, total)
    sys.stdout.write(f"\r  {done / 1048576:7.1f} / {total / 1048576:.1f} MB")
    if done >= total:
        sys.stdout.write("\n")


def fetch(model_dir: Path) -> Path:
    """Download the missing pieces, return the directory that now holds them."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        target = model_dir / name
        if target.exists() and target.stat().st_size > 0:
            print(f"  {name}: already present")
            continue
        print(f"  {name}: downloading")
        tmp = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, open(tmp, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            blocks = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                blocks += 1
                _report(blocks, 1 << 20, total)
        # a .part rename is atomic on Windows: a killed download never leaves
        # a half-written .onnx that ClipOnnx would try to load
        tmp.replace(target)
    return model_dir


def verify(model_dir: Path) -> bool:
    """Encode one text and one image; the shapes are the contract."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PIL import Image

    from core.embed.visual import ClipOnnx

    clip = ClipOnnx(model_dir)
    text = clip.encode_text("screenshot with a red chart")
    image = clip.encode_image(Image.new("RGB", (64, 64), (200, 30, 30)))
    print(f"  text embedding: {text.shape}, image embedding: {image.shape}")
    if text.shape != (512,) or image.shape != (512,):
        print("  WRONG SHAPE - the export does not match this build", file=sys.stderr)
        return False
    print(f"  active text tower: {clip.name}")
    print("  visual search ready")
    return True


def main() -> int:
    from core.config import IndexConfig

    model_dir = IndexConfig().model_dir
    print(f"CLIP visual search setup -> {model_dir}")
    fetch(model_dir)
    return 0 if verify(model_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
