"""Distil the e5 encoder into a static token-embedding table.

Reads the token embedding matrix straight out of model.onnx (no forward
passes: the matrix is an initializer in the graph), quantises it to int8 with
per-row scales and writes the pack `static-e5/table.bin` next to the e5
weights. After that the query path needs neither onnxruntime nor the encoder —
see core/embed/static.py for the runtime side and the quality trade.

One-time, offline:

    pip install 'autofind[onnx]' onnx
    python tools/distill_static.py            # finds e5 in the default model dir
    python tools/distill_static.py --model-dir ~/.autofind/models

After distilling, reindex: the static embedder has its own name, and an index
built with another embedder refuses to mix vector spaces.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.embed.static import HEADER, MAGIC  # noqa: E402

MODEL_SUBDIR = "e5-small"
PACK_SUBDIR = "static-e5"


def find_embedding_matrix(model_path: Path, dim: int):
    """The (vocab, dim) token-embedding initializer from the ONNX graph."""
    import onnx  # type: ignore

    model = onnx.load(str(model_path), load_external_data=False)
    candidates = []
    for initializer in model.graph.initializer:
        if len(initializer.dims) == 2 and initializer.dims[1] == dim:
            candidates.append(initializer)
    if not candidates:
        sys.exit(f"no (vocab, {dim}) initializer in {model_path} — is this the e5 encoder export?")
    if len(candidates) > 1:
        print(f"warning: {len(candidates)} matching initializers, taking the largest")
        candidates.sort(key=lambda item: item.dims[0], reverse=True)
    return onnx.numpy_helper.to_array(candidates[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--model-dir",
        default=str(Path.home() / ".autofind" / "models"),
        help="directory holding e5-small/model.onnx (default: ~/.autofind/models)",
    )
    args = parser.parse_args()

    directory = Path(args.model_dir)
    model_path = directory / MODEL_SUBDIR / "model.onnx"
    tokenizer_path = directory / MODEL_SUBDIR / "tokenizer.json"
    if not model_path.exists() or not tokenizer_path.exists():
        sys.exit(f"e5 weights not found in {directory / MODEL_SUBDIR} (model.onnx + tokenizer.json)")

    from tokenizers import Tokenizer  # type: ignore

    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    vocab = tokenizer.get_vocab_size(with_added_tokens=True)

    started = time.perf_counter()
    print(f"reading the embedding matrix from {model_path} …")
    matrix = find_embedding_matrix(model_path, dim=384).astype(np.float32)
    rows = min(matrix.shape[0], vocab)
    print(f"  {matrix.shape[0]} x {matrix.shape[1]}, quantising {rows} rows …")

    # Per-row int8: one scale per row keeps every token's direction intact,
    # which is all the mean-pool at query time ever sees.
    scales = np.abs(matrix[:rows]).max(axis=1)
    scales[scales == 0] = 1.0
    quantised = np.clip(np.rint(matrix[:rows] / scales[:, None]), -127, 127).astype(np.int8)

    out_dir = directory / PACK_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    table = out_dir / "table.bin"
    with open(table, "wb") as handle:
        handle.write(HEADER.pack(MAGIC, matrix.shape[1], rows))
        handle.write(scales.tobytes())
        handle.write(quantised.tobytes())
    shutil.copyfile(tokenizer_path, out_dir / "tokenizer.json")

    megabytes = table.stat().st_size / 1e6
    print(f"wrote {table} ({megabytes:.0f} MB) in {time.perf_counter() - started:.1f} s")
    print("done — reindex to move the index onto the static embedder")


if __name__ == "__main__":
    main()
