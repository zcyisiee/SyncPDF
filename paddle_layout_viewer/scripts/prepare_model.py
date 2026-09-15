#!/usr/bin/env python3
"""Download PP-DocLayoutV3 (official ONNX) and emit a CoreML-friendly detection-only graph.

Why the graph surgery is needed
-------------------------------
The official export `PaddlePaddle/PP-DocLayoutV3_onnx` cannot run on the Apple GPU as-is:

1. It ships a **mask head** (output `fetch_name_2`, 300x200x200 instance masks) built on
   `GridSample`. We only need bboxes, and those `GridSample` nodes are unsupported by the
   CoreML MLProgram compiler, so they force graph partitions back onto the CPU.
2. Its stem `MaxPool` carries `ceil_mode=1` together with `auto_pad=SAME_UPPER`, which the
   MLProgram compiler rejects outright ("ceil_mode must be False when pad_type is equal to
   same"). When compilation fails, onnxruntime silently runs CoreML **on the CPU** - the
   model appears to work but there is zero GPU acceleration. The pooling has stride 1 and
   kernel 2 on a 400x400 feature map, so `ceil_mode` cannot change the result; clearing it
   is provably lossless (verified bit-identical).
3. Paddle2ONNX writes stale `value_info` for ~2800 intermediate tensors, some of them with a
   bogus leading dimension (256). They are harmless while the batch dim is dynamic, but they
   break shape merging the moment the batch dim is fixed to 1. They are dropped.

Output: `model/inference_bbox.onnx` (inputs unchanged, 2 outputs instead of 3).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = REPO_ROOT / "model"

HF_REPO = "PaddlePaddle/PP-DocLayoutV3_onnx"
FILES = ("inference.onnx", "inference.yml")

RAW_ONNX = MODEL_DIR / "inference.onnx"
BBOX_ONNX = MODEL_DIR / "inference_bbox.onnx"
LABELS_YML = MODEL_DIR / "inference.yml"

# Outputs of the raw graph: boxes, tiled count, mask logits.
KEEP_OUTPUTS = ["fetch_name_0", "fetch_name_1"]
GRAPH_INPUTS = ["im_shape", "image", "scale_factor"]


def _endpoint() -> str:
    """Allow the HF mirror (e.g. https://hf-mirror.com) via environment."""
    return os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")


def download(repo: str, filename: str, dest: Path, force: bool = False) -> Path:
    if dest.exists() and not force:
        print(f"  [skip] {dest.name} already present ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest
    url = f"{_endpoint()}/{repo}/resolve/main/{filename}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  [get ] {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as fh:  # noqa: S310 - 固定 https huggingface 端点
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
            done += len(chunk)
            if total:
                pct = 100 * done / total
                sys.stdout.write(f"\r        {done / 1e6:7.1f} / {total / 1e6:.1f} MB ({pct:5.1f}%)")
                sys.stdout.flush()
    sys.stdout.write("\n")
    shutil.move(tmp, dest)
    return dest


def fetch_weights(force: bool = False) -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {HF_REPO} -> {MODEL_DIR}")
    for name in FILES:
        download(HF_REPO, name, MODEL_DIR / name, force=force)


def build_bbox_graph(src: Path, dst: Path, force: bool = False) -> Path:
    import onnx
    import onnx.utils
    from onnx import helper

    if dst.exists() and not force:
        print(f"  [skip] {dst.name} already present - use --force to rebuild")
        return dst

    # --- 1. drop every output we do not consume (removes the whole GridSample mask head) ---
    tmp = dst.with_suffix(".pruned.onnx")
    onnx.utils.extract_model(str(src), str(tmp), GRAPH_INPUTS, KEEP_OUTPUTS)
    model = onnx.load(str(tmp), load_external_data=False)
    tmp.unlink()

    # --- 2. make the stem MaxPool palatable to the MLProgram compiler ---
    patched = 0
    for node in model.graph.node:
        if node.op_type != "MaxPool":
            continue
        kept, seen = [], False
        for attr in node.attribute:
            if attr.name == "ceil_mode":
                if seen:            # collapse accidental duplicates
                    continue
                seen = True
                attr.i = 0
            kept.append(attr)
        if not seen:
            kept.append(helper.make_attribute("ceil_mode", 0))
        del node.attribute[:]
        node.attribute.extend(kept)
        patched += 1

    # --- 3. drop stale Paddle2ONNX value_info (bogus leading dims break static shapes) ---
    stale = len(model.graph.value_info)
    del model.graph.value_info[:]

    onnx.save(model, str(dst))
    print(f"  [ok  ] {dst.name}: pruned 1 output, ceil_mode fixed on {patched} MaxPool, "
          f"dropped {stale} stale value_info records")
    print(f"         {dst.stat().st_size / 1e6:.1f} MB")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="re-download and rebuild everything")
    ap.add_argument("--check", action="store_true", help="verify the prepared graph loads")
    args = ap.parse_args()

    fetch_weights(force=args.force)
    build_bbox_graph(RAW_ONNX, BBOX_ONNX, force=args.force)

    if args.check:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.log_severity_level = 3
        for dim in ("DynamicDimension.0", "DynamicDimension.1", "DynamicDimension.2"):
            so.add_free_dimension_override_by_name(dim, 1)
        sess = ort.InferenceSession(str(BBOX_ONNX), so, providers=["CPUExecutionProvider"])
        print("  [ok  ] graph loads; inputs =",
              [(i.name, i.shape) for i in sess.get_inputs()],
              "outputs =", [o.name for o in sess.get_outputs()])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
