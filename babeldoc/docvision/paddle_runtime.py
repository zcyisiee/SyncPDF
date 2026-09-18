"""Local PP-DocLayoutV3 + PaddleOCR-VL runtime with optional MLX/CoreML.

PaddleX 3.7.2 owns region preparation and postprocessing for every backend.
MLX consumes the original BF16 checkpoint; it never quantizes or resizes inputs
beyond the official per-task processor contract. Native PDF text stays outside
this module and is never replaced by recognized text.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from babeldoc.docvision.layout_labels import PADDLE_LABELS
from babeldoc.docvision.paddle_model_lock import MODEL_LOCK
from babeldoc.docvision.resource_controller import InferenceResourceController

logger = logging.getLogger(__name__)
RUNTIME_CONTRACT = "paddle-regional-pixels-native-fallback-v6"


@contextmanager
def processor_pixel_budget(processor, pixels):
    """Apply PaddleX's task budget to the locked HF image processor.

    The locked remote processor ignores min/max_pixels kwargs for images and
    reads its instance attributes instead. Recognition is serialized; always
    restore defaults so one region cannot change the following region's budget.
    """
    image_processor = processor.image_processor
    previous = {key: getattr(image_processor, key) for key in pixels}
    try:
        for key, value in pixels.items():
            setattr(image_processor, key, value)
        yield
    finally:
        for key, value in previous.items():
            setattr(image_processor, key, value)


class PaddleDependencyError(RuntimeError):
    """The local runtime cannot execute; no remote provider is substituted."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(directory: Path, name: str) -> dict:
    lock = MODEL_LOCK[name]
    for filename, expected in lock["files"].items():
        path = directory / filename
        if not path.is_file():
            raise PaddleDependencyError(
                f"Missing {path}; install {lock['repo']} at revision {lock['revision']}. "
                "See docs/guide/cli.md"
            )
        if sha256_file(path) != expected:
            raise PaddleDependencyError(
                f"Model hash mismatch: {path}; expected locked {lock['revision']}"
            )
    return {
        "repo": lock["repo"],
        "revision": lock["revision"],
        "directory": str(directory),
        "verified_sha256": dict(lock["files"]),
    }


class MlxRecognition:
    """The PaddleX regional VLM interface implemented using unquantized MLX."""

    def __init__(self, model_dir: Path):
        try:
            import mlx.core as mx
            from mlx_vlm import load
        except ImportError as exc:
            raise PaddleDependencyError(
                "MLX requires the locked Apple Silicon optional runtime"
            ) from exc
        self.mx = mx
        if mx.default_device() != mx.gpu:
            raise PaddleDependencyError("MLX is not executing on an Apple GPU")
        self.model, self.processor = load(str(model_dir), trust_remote_code=True)
        if getattr(self.model.config, "quantization", None):
            raise PaddleDependencyError(
                "Quantized VLM models are outside the model contract"
            )
        self.batch_sampler = SimpleNamespace(batch_size=1)
        self.calls: list[dict] = []

    def predict(self, inputs, **kwargs):
        from mlx_vlm import stream_generate
        from mlx_vlm.prompt_utils import apply_chat_template
        from PIL import Image

        for item in inputs:
            start = time.perf_counter()
            prompt = apply_chat_template(
                self.processor, self.model.config, item["query"], num_images=1
            )
            # Match PaddleX's fetch_image exactly: region arrays are consumed
            # directly. An extra channel reversal changes colored diagrams.
            image = Image.fromarray(np.asarray(item["image"]).copy())
            pixels = {
                k: kwargs[k]
                for k in ("min_pixels", "max_pixels")
                if kwargs.get(k) is not None
            }
            max_tokens = kwargs.get("max_new_tokens") or 8192
            chunks = []
            last = None
            with processor_pixel_budget(self.processor, pixels):
                for last in stream_generate(
                    self.model,
                    self.processor,
                    prompt=prompt,
                    image=[image],
                    max_tokens=max_tokens,
                    temperature=0,
                    skip_special_tokens=kwargs.get("skip_special_tokens", True),
                ):
                    chunks.append(last.text)
            content = "".join(chunks)
            stopped = last is not None and self.processor.tokenizer.stopping_criteria(
                last.token
            )
            evidence = {
                "query": item["query"],
                "image_size": list(image.size),
                "processor_pixels": pixels,
                "generation_tokens": last.generation_tokens if last else 0,
                "elapsed_s": time.perf_counter() - start,
                "backend": "mlx-gpu",
                "quantized": False,
                "status": "complete"
                if stopped and content.strip()
                else "native_fallback",
                "reason": None
                if stopped and content.strip()
                else ("empty" if stopped else "token_limit"),
                "max_tokens": max_tokens,
                "raw_text": content,
                "image_sha256": hashlib.sha256(
                    np.asarray(item["image"]).tobytes()
                ).hexdigest(),
            }
            self.calls.append(evidence)
            if evidence["status"] != "complete":
                logger.warning(
                    "VLM reference unavailable (%s, %s); retaining native PDF objects",
                    item["query"],
                    evidence["reason"],
                )
            yield {
                "result": content if evidence["status"] == "complete" else "",
                "input_path": None,
                "recognition": evidence,
            }

    def close(self):
        pass


class OnnxLayout:
    """Production ONNX engine; viewer rendering and caches are not imported."""

    def __init__(
        self,
        model_path: Path,
        cache_dir: Path,
        *,
        device: str,
        threads: int,
        config: dict,
    ):
        import onnxruntime as ort
        from paddlex.inference.models.layout_analysis.processors import (
            LayoutAnalysisProcess,
        )
        from paddlex.inference.models.layout_analysis.result import LayoutAnalysisResult

        expected = MODEL_LOCK["onnx"]["files"]["inference_bbox.onnx"]
        if sha256_file(model_path) != expected:
            raise PaddleDependencyError(f"Unverified layout graph: {model_path}")
        self.result_class = LayoutAnalysisResult
        self.postprocess = LayoutAnalysisProcess(
            labels=list(PADDLE_LABELS), scale_size=[800, 800]
        )
        self.config = config
        self.batch_sampler = SimpleNamespace(batch_size=1)
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.log_severity_level = 3
        options.enable_profiling = True
        cache_dir.mkdir(parents=True, exist_ok=True)
        options.profile_file_prefix = str(cache_dir / f"ort-{time.time_ns()}")
        for dim in ("DynamicDimension.0", "DynamicDimension.1", "DynamicDimension.2"):
            options.add_free_dimension_override_by_name(dim, 1)
        providers = ["CPUExecutionProvider"]
        if (
            device != "cpu"
            and "CoreMLExecutionProvider" in ort.get_available_providers()
        ):
            providers.insert(
                0,
                (
                    "CoreMLExecutionProvider",
                    {
                        "ModelFormat": "MLProgram",
                        "MLComputeUnits": "CPUAndGPU",
                        "ModelCacheDirectory": str(cache_dir / "coreml"),
                    },
                ),
            )
        self.session = ort.InferenceSession(
            str(model_path), options, providers=providers
        )
        self.execution_evidence = None

    def __call__(self, images, **kwargs):
        import cv2

        for image in images:
            height, width = image.shape[:2]
            # Input is BGR from PaddleX ReadImage, identical to the official graph.
            tensor = (
                cv2.resize(image, (800, 800), interpolation=cv2.INTER_CUBIC).astype(
                    np.float32
                )
                / 255
            )
            raw = self.session.run(
                None,
                {
                    "image": np.ascontiguousarray(tensor.transpose(2, 0, 1)[None]),
                    "im_shape": np.array([[800, 800]], np.float32),
                    "scale_factor": np.array([[800 / height, 800 / width]], np.float32),
                },
            )
            if self.execution_evidence is None:
                path = Path(self.session.end_profiling())
                events = json.loads(path.read_text())
                counts = Counter(
                    e.get("args", {}).get("provider")
                    for e in events
                    if e.get("args", {}).get("provider")
                )
                self.execution_evidence = {
                    "profile": str(path),
                    "executed_provider_nodes": dict(counts),
                }
            settings = {
                key: kwargs.get(key)
                if kwargs.get(key) is not None
                else self.config.get(key)
                for key in (
                    "threshold",
                    "layout_nms",
                    "layout_unclip_ratio",
                    "layout_merge_bboxes_mode",
                )
            }
            boxes = self.postprocess(
                [{"boxes": raw[0].copy()}],
                [{"ori_img_size": (width, height)}],
                **settings,
                layout_shape_mode="rect",
                filter_overlap_boxes=kwargs.get("filter_overlap_boxes", False),
                skip_order_labels=kwargs.get("skip_order_labels"),
            )[0]
            yield self.result_class(
                {
                    "input_path": None,
                    "page_index": None,
                    "input_img": image,
                    "boxes": boxes,
                }
            )


class PaddleRuntime:
    def __init__(
        self,
        *,
        device="auto",
        layout_model_dir=None,
        vlm_model_dir=None,
        onnx_model=None,
        cache_dir=None,
    ):
        if device not in {"auto", "mlx", "cpu"}:
            raise ValueError("Paddle device must be auto, mlx, or cpu")
        self.device = device
        self.layout_dir = Path(
            layout_model_dir or Path.home() / ".paddlex/official_models/PP-DocLayoutV3"
        )
        self.vlm_dir = Path(
            vlm_model_dir or Path.home() / ".paddlex/official_models/PaddleOCR-VL-1.6"
        )
        self.cache_dir = Path(
            cache_dir or Path.home() / ".cache/babeldoc/paddle-runtime"
        )
        self.onnx_model = Path(
            onnx_model
            or Path.home() / ".cache/babeldoc/paddle-models/inference_bbox.onnx"
        )
        self.monitor = InferenceResourceController()
        self.pipeline = None
        self.last_page_report = None
        self.region_reports = []
        self.metadata = {
            "backend": "paddle",
            "models": {},
            "fallbacks": [],
            "resource_policy": "telemetry-only",
            "contract": RUNTIME_CONTRACT,
            "document_scope": "digital PDF; rectangular regions; native text is authoritative",
        }

    def _load(self):
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        try:
            import paddlex
            import yaml
            from paddlex.inference.pipelines.paddleocr_vl.pipeline import (
                _PaddleOCRVLPipeline,
            )
        except ImportError as exc:
            raise PaddleDependencyError(
                "Install the locked paddlex / paddleocr / paddlepaddle optional dependencies; see docs/guide/cli.md"
            ) from exc
        try:
            versions = {
                p: importlib.metadata.version(p)
                for p in ("paddlex", "paddleocr", "paddlepaddle")
            }
        except importlib.metadata.PackageNotFoundError as exc:
            raise PaddleDependencyError(
                "Install the locked paddlex / paddleocr / paddlepaddle optional dependencies; "
                f"missing distribution: {exc.name}"
            ) from exc
        if versions != {
            "paddlex": "3.7.2",
            "paddleocr": "3.7.0",
            "paddlepaddle": "3.3.1",
        }:
            raise PaddleDependencyError(
                f"Unverified Paddle runtime versions: {versions}"
            )
        self.metadata["versions"] = versions
        self.metadata["models"] = {
            "layout": verify_model(self.layout_dir, "layout"),
            "vlm": verify_model(self.vlm_dir, "vlm"),
        }
        config = yaml.safe_load(
            (
                Path(paddlex.__file__).parent
                / "configs/pipelines/PaddleOCR-VL-1.6.yaml"
            ).read_text()
        )
        config.update(
            batch_size=1,
            use_queues=False,
            use_chart_recognition=True,
            use_seal_recognition=True,
            use_ocr_for_image_block=True,
        )
        config["SubModules"]["LayoutDetection"]["model_dir"] = str(self.layout_dir)
        config["SubModules"]["VLRecognition"]["model_dir"] = str(self.vlm_dir)
        runtime = self
        use_mlx = self.device == "mlx" or (
            self.device == "auto"
            and platform.system() == "Darwin"
            and platform.machine() == "arm64"
        )

        class Pipeline(_PaddleOCRVLPipeline):
            def _paddleocr_vl_assemble_parsing_results(
                self, blocks, batches, indices, drop_figures_set, vis_image_labels
            ):
                runtime.region_reports = []
                for batch in batches.values():
                    for (page_index, block_index), result in zip(
                        batch["vlm_block_ids"], batch["vlm_results"], strict=True
                    ):
                        block = blocks[page_index][block_index]
                        runtime.region_reports.append(
                            {
                                "label": block["label"],
                                "bbox": [float(x) for x in block["box"]],
                                "recognition": result.get(
                                    "recognition",
                                    {
                                        "status": "unverified",
                                        "backend": "paddle-cpu",
                                        "reason": "native API does not expose generation termination",
                                    },
                                ),
                            }
                        )
                return super()._paddleocr_vl_assemble_parsing_results(
                    blocks, batches, indices, drop_figures_set, vis_image_labels
                )

            def create_model(self, model_config, *args, **kwargs):
                if model_config.get("module_name") == "vl_recognition":
                    if use_mlx:
                        try:
                            model = MlxRecognition(runtime.vlm_dir)
                            runtime.metadata["vlm_device"] = "mlx-gpu"
                            return model
                        except (ImportError, PaddleDependencyError) as exc:
                            if runtime.device == "mlx":
                                raise
                            runtime.metadata["fallbacks"].append(
                                f"MLX unavailable: {exc}"
                            )
                            logger.warning(
                                "MLX unavailable; running VLM on CPU: %s", exc
                            )
                    runtime.metadata["vlm_device"] = "cpu"
                if (
                    model_config.get("module_name") == "layout_detection"
                    and runtime.onnx_model.is_file()
                ):
                    return OnnxLayout(
                        runtime.onnx_model,
                        runtime.cache_dir,
                        device=runtime.device,
                        threads=runtime.monitor.cpu_threads,
                        config=model_config,
                    )
                return super().create_model(model_config, *args, **kwargs)

        with self.monitor.measure("mlx-gpu" if use_mlx else "cpu", job="load-models"):
            self.pipeline = Pipeline(config, device="cpu")
        if not isinstance(self.pipeline.layout_det_model, OnnxLayout):
            self.metadata["layout_device"] = "paddle-cpu"
            self.metadata["fallbacks"].append(
                "No verified ONNX graph; using official PP-DocLayoutV3 CPU"
            )

    def predict(self, rgb: np.ndarray, layout_threshold=None) -> dict:
        if self.pipeline is None:
            self._load()
        vlm = self.pipeline.vl_rec_model
        call_start = len(getattr(vlm, "calls", []))
        sample_start = len(self.monitor.samples)
        job_start = len(self.monitor.jobs)
        predict_kwargs = {}
        if layout_threshold is not None:
            predict_kwargs["layout_threshold"] = layout_threshold
        with self.monitor.measure(
            self.metadata["vlm_device"], job=f"page-{len(self.monitor.jobs)}"
        ):
            result = next(
                self.pipeline.predict(
                    np.ascontiguousarray(rgb[:, :, ::-1]),
                    use_queues=False,
                    layout_shape_mode="rect",
                    **predict_kwargs,
                )
            )
        layout = self.pipeline.layout_det_model
        if isinstance(layout, OnnxLayout):
            self.metadata["layout_execution"] = layout.execution_evidence
            self.metadata["layout_device"] = (
                "coreml/CPUAndGPU"
                if layout.execution_evidence["executed_provider_nodes"].get(
                    "CoreMLExecutionProvider"
                )
                else "onnx-cpu"
            )
        self.last_page_report = copy.deepcopy(self.metadata)
        self.last_page_report.update(
            vlm_calls=copy.deepcopy(getattr(vlm, "calls", [])[call_start:]),
            jobs=copy.deepcopy(self.monitor.jobs[job_start:]),
            samples=[
                vars(sample).copy() for sample in self.monitor.samples[sample_start:]
            ],
        )
        raw = result.json["res"]
        references = {
            (r["label"], tuple(r["bbox"])): r["recognition"]
            for r in self.region_reports
        }
        for block in raw.get("parsing_res_list", []):
            block["recognition"] = references.get(
                (block["block_label"], tuple(block["block_bbox"])),
                {"status": "unverified", "reason": "no_matching_region_evidence"},
            )
        return raw

    def report(self):
        report = dict(self.metadata)
        report["resources"] = self.monitor.report()
        vlm = getattr(self.pipeline, "vl_rec_model", None)
        report["vlm_calls"] = getattr(vlm, "calls", [])
        return report
