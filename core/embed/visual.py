"""Visual embedders.

`LayoutEmbedder` is the default: a 228-dimension descriptor of colour, layout
and edge density. It answers "find screenshots that look like this one" with no
model weights, and it is honest about what it cannot do — there is no text
encoder, so a free-text query never reaches it.

`ClipOnnx` takes over when the int8 export is present and adds text-to-image
search. Both expose the same interface, so the pipeline does not branch.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from ..images import visual_features

LAYOUT_DIM = 228
CLIP_DIM = 512


class LayoutEmbedder:
    name = "layout-228"
    dim = LAYOUT_DIM
    supports_text = False

    def encode_image(self, image: Image.Image) -> np.ndarray:
        return visual_features(image)

    def encode_text(self, text: str) -> np.ndarray | None:
        return None


class ClipOnnx:
    name = "clip-vit-b32-int8"
    dim = CLIP_DIM
    supports_text = True

    def __init__(self, model_dir: str | Path) -> None:
        directory = Path(model_dir)
        visual = directory / "visual.onnx"
        tokenizer = directory / "tokenizer.json"
        # The image tower comes from English CLIP, but the text tower has two
        # flavours: the original English one and a multilingual distilled one
        # whose output lives in the same 512-d image space. The multilingual
        # tower makes «найди скриншот с красным графиком» work natively, so it
        # wins whenever it has been fetched (autofind clip).
        textual = directory / "textual_multi.onnx"
        text_tokenizer = directory / "tokenizer_multi.json"
        if not (textual.exists() and text_tokenizer.exists()):
            textual = directory / "textual.onnx"
            text_tokenizer = tokenizer
        if not (visual.exists() and textual.exists() and tokenizer.exists()
                and text_tokenizer.exists()):
            raise FileNotFoundError(f"CLIP export not found in {directory}")
        if textual.name == "textual_multi.onnx":
            self.name = "clip-vit-b32-int8-multi"
        try:
            import onnxruntime as ort  # type: ignore
            from tokenizers import Tokenizer  # type: ignore
        except ImportError as exc:
            raise FileNotFoundError("onnxruntime and tokenizers are required for CLIP") from exc

        options = ort.SessionOptions()
        options.log_severity_level = 3
        self.visual = ort.InferenceSession(str(visual), options, providers=["CPUExecutionProvider"])
        self.textual = ort.InferenceSession(str(textual), options, providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(tokenizer))
        self.tokenizer.enable_truncation(max_length=77)
        self.tokenizer.enable_padding(length=77)
        self.text_tokenizer = Tokenizer.from_file(str(text_tokenizer))
        self.text_tokenizer.enable_truncation(max_length=77)
        self.text_tokenizer.enable_padding(length=77)

    def encode_image(self, image: Image.Image) -> np.ndarray:
        array = np.asarray(
            image.convert("RGB").resize((224, 224), Image.Resampling.BICUBIC), dtype=np.float32
        ) / 255.0
        mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
        tensor = ((array - mean) / std).transpose(2, 0, 1)[None, ...]
        feed = {"pixel_values": tensor}
        return _normalise(_embed_output(self.visual.run(None, self._accepted(self.visual, feed))))

    def encode_text(self, text: str) -> np.ndarray:
        encoded = self.text_tokenizer.encode(text)
        feed = {
            "input_ids": np.array([encoded.ids], dtype=np.int64),
            "attention_mask": np.array([encoded.attention_mask], dtype=np.int64),
        }
        return _normalise(_embed_output(self.textual.run(None, self._accepted(self.textual, feed))))

    @staticmethod
    def _accepted(session, feed: dict) -> dict:
        """Keep only the inputs this export actually declares.

        Exporters disagree on whether attention_mask is wired up; asking a
        session for an input it lacks aborts the run, so filter by signature.
        """
        declared = {item.name for item in session.get_inputs()}
        return {name: value for name, value in feed.items() if name in declared}


def _embed_output(outputs) -> np.ndarray:
    """Pick the projected embedding out of a session's outputs.

    Exporters disagree on output order and naming: some hand back the projected
    (batch, dim) vector first, some the raw (batch, tokens, dim) states. Shape
    is the only contract both honour, so pick the 2-D tensor whose last axis is
    the projection width instead of trusting position.
    """
    for candidate in outputs:
        array = np.asarray(candidate)
        if array.ndim == 2 and array.shape[1] == CLIP_DIM:
            return array[0]
    raise RuntimeError(f"CLIP export produced no ({CLIP_DIM},) embedding: "
                       + ", ".join(f"{a.shape}" for a in outputs))


def _normalise(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return (vector / norm).astype(np.float32) if norm else vector.astype(np.float32)


def get_visual_embedder(model_dir: str | Path | None = None, prefer: str = "auto"):
    if prefer in ("auto", "clip") and model_dir is not None:
        try:
            return ClipOnnx(model_dir)
        except FileNotFoundError:
            if prefer == "clip":
                raise
    return LayoutEmbedder()
