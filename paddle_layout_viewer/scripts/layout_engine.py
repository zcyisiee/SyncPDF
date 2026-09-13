#!/usr/bin/env python3
"""PP-DocLayoutV3 inference engine tuned for Apple Silicon.

Pipeline contract (all three points were established empirically against this ONNX export,
not guessed - deviating from any of them yields near-zero confidence scores):

* input scaling   : pixels must be scaled to [0, 1] (``is_scale`` in the deploy config),
                    otherwise the best score collapses from ~0.96 to ~0.05.
* ``scale_factor``: ``[h_scale, w_scale]`` = ``[800/H, 800/W]`` of the *source* image.
                    PaddleX reverses its internal ``[w, h]`` order before feeding the graph.
* output layout   : ``fetch_name_0`` is ``(300, 7)`` = ``[class_id, score, x1, y1, x2, y2,
                    order_rank]``, one row per decoder query. ``order_rank`` is the model's
                    reading-order prediction; sorting rows by it reproduces the reading order.

Apple Silicon acceleration
--------------------------
onnxruntime ships a CoreML execution provider which drives the M-series GPU/ANE through
MLProgram. Two things are required to actually get GPU execution rather than a silent CPU
fallback: the graph must compile (see ``prepare_model.py``) and the input shapes must be
static, which we pin with free-dimension overrides. Measured on an M5 Pro at 800x800:

    CPU (all cores)                       ~305 ms/page
    CoreML MLProgram / CPUAndGPU          ~87 ms/page   3.5x
    CoreML MLProgram / ALL (incl. ANE)    ~99 ms/page

Some CoreML configurations (notably ``ModelFormat=NeuralNetwork`` and the provider defaults)
compile fine but return *wrong* boxes, so the engine self-checks the GPU against the CPU once
and caches the verdict in ``model/coreml_verify.json``.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = REPO_ROOT / "model"
DEFAULT_MODEL = MODEL_DIR / "inference_bbox.onnx"
DEFAULT_CACHE = MODEL_DIR / "coreml_cache"
VERIFY_CACHE = MODEL_DIR / "coreml_verify.json"

#: CoreML EP builds its model URL from a plain path and chokes on non-ASCII characters
#: (this project can live under e.g. ``~/Desktop/博0/杂项/...``). When that happens the
#: graph is staged into a pure-ASCII cache dir under $HOME instead. A symlink is not
#: enough: CoreML resolves it back to the non-ASCII target.
ASCII_STAGE_DIR = Path.home() / ".cache" / "paddle-layout-viewer"

NET_SIZE = 800  # the graph is compiled for 1x3x800x800
FREE_DIMS = ("DynamicDimension.0", "DynamicDimension.1", "DynamicDimension.2")
RAW_ONNX = MODEL_DIR / "inference.onnx"

#: PP-DocLayoutV3 classes, in the order of ``label_list`` in ``inference.yml``.
LABELS: tuple[str, ...] = (
    "abstract", "algorithm", "aside_text", "chart", "content", "display_formula",
    "doc_title", "figure_title", "footer", "footer_image", "footnote", "formula_number",
    "header", "header_image", "image", "inline_formula", "number", "paragraph_title",
    "reference", "reference_content", "seal", "table", "text", "vertical_text",
    "vision_footnote",
)

#: Display colours, grouped so that semantically similar classes share a hue family:
#: body text = blue, headings = purple, formulas/math = orange, media = green, furniture = rose.
PALETTE: dict[str, str] = {
    # body text / prose
    "text": "#2563eb",
    "content": "#1d4ed8",
    "abstract": "#1e40af",
    "reference": "#3b82f6",
    "reference_content": "#60a5fa",
    "footnote": "#0ea5e9",
    "vision_footnote": "#06b6d4",
    "aside_text": "#0891b2",
    "vertical_text": "#64748b",
    # headings
    "doc_title": "#7c3aed",
    "paragraph_title": "#a855f7",
    "figure_title": "#c026d3",
    # math
    "display_formula": "#ea580c",
    "inline_formula": "#f59e0b",
    "formula_number": "#d97706",
    "algorithm": "#b45309",
    # media / floats
    "image": "#059669",
    "chart": "#10b981",
    "table": "#0d9488",
    "header_image": "#14b8a6",
    "footer_image": "#2dd4bf",
    "seal": "#65a30d",
    # page furniture
    "header": "#dc2626",
    "footer": "#e11d48",
    "number": "#f43f5e",
}
FALLBACK_COLOR = "#475569"


@dataclass
class Box:
    """One detected layout region, in *source image pixel* coordinates."""

    label: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float
    order: int          # 1-based reading order within the page
    query_rank: int     # raw reading-order value predicted by the model

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("x0", "y0", "x1", "y1"):
            d[k] = round(d[k], 2)
        d["score"] = round(d["score"], 4)
        return d


def preprocess(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, tuple[int, int]]:
    """RGB uint8 HxWx3 -> (1,3,800,800) float32 tensor + scale_factor + (H, W).

    Resizing squashes to a square without preserving aspect ratio; that is what the deploy
    config declares (``keep_ratio: false``) and what the model was trained with. The inverse
    mapping is carried by ``scale_factor``, so detection quality is unaffected.
    """
    import cv2

    h, w = rgb.shape[:2]
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])          # PaddleX feeds BGR (cv2.imread order)
    resized = cv2.resize(bgr, (NET_SIZE, NET_SIZE), interpolation=cv2.INTER_CUBIC)
    tensor = resized.astype(np.float32) / 255.0          # <- the 1/255 the deploy yml implies
    tensor = np.transpose(tensor, (2, 0, 1))[None]
    scale_factor = np.array([[NET_SIZE / h, NET_SIZE / w]], dtype=np.float32)
    return np.ascontiguousarray(tensor), scale_factor, (h, w)


def _is_ascii(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def stage_for_coreml(path: Path) -> Path:
    """Return an ASCII-only copy of ``path`` when the original path is not ASCII."""
    if _is_ascii(path):
        return path
    ASCII_STAGE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ASCII_STAGE_DIR / path.name
    src = path.stat()
    if not dest.exists() or dest.stat().st_size != src.st_size \
            or dest.stat().st_mtime < src.st_mtime:
        shutil.copy2(path, dest)
    return dest


def stage_for_coreml_dir(path: Path) -> Path:
    """Same as :func:`stage_for_coreml` but for a directory (the CoreML model cache)."""
    if _is_ascii(path):
        return path
    dest = ASCII_STAGE_DIR / path.name
    dest.mkdir(parents=True, exist_ok=True)
    return dest


class LayoutEngine:
    """Runs PP-DocLayoutV3 on one page image at a time."""

    def __init__(
        self,
        model_path: Path | str = DEFAULT_MODEL,
        device: str = "auto",            # auto | gpu | cpu
        units: str = "CPUAndGPU",        # CoreML MLComputeUnits: CPUAndGPU | ALL | CPUAndNeuralEngine
        threads: int = 0,
        cache_dir: Path | str | None = DEFAULT_CACHE,
        verify: bool = True,
        verbose: bool = True,
    ) -> None:
        import onnxruntime as ort

        self.ort = ort
        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"{self.model_path} not found - run `python scripts/prepare_model.py` first"
            )
        self.requested_device = device
        self.units = units
        self.threads = threads
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.verbose = verbose

        # CoreML needs an ASCII model path; stage a copy if the repo path is not ASCII.
        self.staged_path = stage_for_coreml(self.model_path)
        if self.cache_dir is not None:
            self.cache_dir = stage_for_coreml_dir(self.cache_dir)

        self.device = "cpu"
        self.session = self._build_cpu()
        if device in ("auto", "gpu"):
            self.session, self.device = self._build_best_gpu(verify=verify)
        if verbose:
            print(f"[engine] {self.device}  ({self._describe()})")
            if self.staged_path != self.model_path:
                print(f"[engine] CoreML staging copy: {self.staged_path}")

    # ------------------------------------------------------------------ session plumbing
    def _session_options(self, **extra: str):
        ort = self.ort
        so = ort.SessionOptions()
        so.log_severity_level = 3                      # silence CoreML partition chatter
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if self.threads:
            so.intra_op_num_threads = self.threads
        for dim in FREE_DIMS:                          # pin batch=1 -> static shapes
            so.add_free_dimension_override_by_name(dim, 1)
        for k, v in extra.items():
            so.add_session_config_entry(k, v)
        return so

    def _build_cpu(self):
        return self.ort.InferenceSession(
            str(self.model_path), self._session_options(), providers=["CPUExecutionProvider"]
        )

    def _build_gpu(self, units: str, cache_dir: Path | None):
        opts = {"ModelFormat": "MLProgram", "MLComputeUnits": units}
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
            opts["ModelCacheDirectory"] = str(cache_dir)
        providers = [("CoreMLExecutionProvider", opts)]
        try:
            self.ort.get_available_providers().index("CoreMLExecutionProvider")
        except ValueError:
            raise RuntimeError("CoreMLExecutionProvider unavailable in this onnxruntime build")
        return self.ort.InferenceSession(
            str(self.staged_path), self._session_options(),
            providers=providers + ["CPUExecutionProvider"],
        )

    def _build_best_gpu(self, verify: bool):
        try:
            gpu = self._build_gpu(self.units, self.cache_dir)
        except Exception as exc:  # pragma: no cover - depends on host CoreML
            if self.verbose:
                print(f"[engine] CoreML unavailable ({type(exc).__name__}: {exc}) -> CPU")
            return self.session, "cpu"

        if not verify:
            return gpu, f"coreml/{self.units}"

        verdict = self._cached_verdict()
        if verdict is None:
            delta = self._cross_check(gpu)
            ok = delta is not None and delta < 0.05
            verdict = {"ok": ok, "max_score_delta": delta, "units": self.units}
            self._store_verdict(verdict)
            if self.verbose:
                print(f"[engine] GPU self-check vs CPU: max score delta = "
                      f"{'n/a' if delta is None else f'{delta:.2e}'} -> "
                      f"{'using GPU' if ok else 'MISMATCH, falling back to CPU'}")
        if verdict.get("ok"):
            return gpu, f"coreml/{self.units}"
        return self.session, "cpu"

    # ------------------------------------------------------------------ verification cache
    def _verify_key(self) -> str:
        import hashlib
        h = hashlib.sha256(self.model_path.read_bytes()).hexdigest()[:16]
        return f"{h}:{self.units}:{self.ort.__version__}"

    def _cached_verdict(self):
        if not VERIFY_CACHE.exists():
            return None
        try:
            data = json.loads(VERIFY_CACHE.read_text())
        except Exception:
            return None
        return data.get(self._verify_key())

    def _store_verdict(self, verdict: dict) -> None:
        data = {}
        if VERIFY_CACHE.exists():
            try:
                data = json.loads(VERIFY_CACHE.read_text())
            except Exception:
                data = {}
        data[self._verify_key()] = verdict
        try:
            VERIFY_CACHE.write_text(json.dumps(data, indent=2))
        except OSError:
            pass

    def _cross_check(self, gpu_session) -> float | None:
        """Compare GPU vs CPU class scores on a synthetic page."""
        rng = np.random.default_rng(0)
        canvas = np.full((900, 700, 3), 255, dtype=np.uint8)
        canvas[80:140, 60:640] = 30
        canvas[200:260, 60:640] = 200
        canvas[300:420, 60:640] = rng.integers(0, 255, (120, 580, 3), dtype=np.uint8)
        tensor, sf, _ = preprocess(canvas)
        feed = {"image": tensor, "im_shape": np.array([[NET_SIZE, NET_SIZE]], np.float32),
                "scale_factor": sf}
        try:
            a = self.session.run(None, feed)[0]
            b = gpu_session.run(None, feed)[0]
        except Exception:
            return None
        return float(np.abs(a[:, 1] - b[:, 1]).max())

    # ------------------------------------------------------------------ inference
    def infer_rgb(self, rgb: np.ndarray, threshold: float = 0.5,
                  session=None, ) -> list[Box]:
        """Detect layout regions on an RGB uint8 page image."""
        sess = session or self.session
        tensor, scale_factor, (h, w) = preprocess(rgb)
        raw = sess.run(None, {
            "image": tensor,
            "im_shape": np.array([[NET_SIZE, NET_SIZE]], dtype=np.float32),
            "scale_factor": scale_factor,
        })[0]
        return decode(raw, threshold=threshold, width=w, height=h)

    def benchmark(self, page: np.ndarray, runs: int = 8) -> float:
        import time
        self.infer_rgb(page)                              # warm up
        times = []
        for _ in range(runs):
            t0 = time.perf_counter()
            self.infer_rgb(page)
            times.append(time.perf_counter() - t0)
        return float(np.median(times) * 1000.0)

    def _describe(self) -> str:
        return f"{self.model_path.name}, onnxruntime {self.ort.__version__}"


def decode(raw: np.ndarray, threshold: float, width: int, height: int) -> list[Box]:
    """(300, 7) raw graph output -> reading-order-sorted ``Box`` list."""
    rows = np.asarray(raw)
    if rows.ndim != 2 or rows.shape[1] < 7 or rows.size == 0:
        return []
    rows = rows[rows[:, 1] >= threshold]
    if rows.size == 0:
        return []
    rows = rows[np.argsort(rows[:, 6], kind="stable")]

    boxes: list[Box] = []
    for i, (cls, score, x0, y0, x1, y1, rank) in enumerate(rows, start=1):
        x0, x1 = sorted((float(x0), float(x1)))
        y0, y1 = sorted((float(y0), float(y1)))
        x0, x1 = max(0.0, x0), min(float(width), x1)
        y0, y1 = max(0.0, y0), min(float(height), y1)
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            continue
        idx = int(cls)
        boxes.append(Box(
            label=LABELS[idx] if 0 <= idx < len(LABELS) else f"class_{idx}",
            score=float(score), x0=x0, y0=y0, x1=x1, y1=y1,
            order=i, query_rank=int(rank),
        ))
    return boxes


def class_counts(pages: Iterable[Sequence[Box]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for boxes in pages:
        for b in boxes:
            counts[b.label] = counts.get(b.label, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def color_for(label: str) -> str:
    return PALETTE.get(label, FALLBACK_COLOR)
