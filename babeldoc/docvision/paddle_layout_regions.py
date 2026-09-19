"""PP-DocLayoutV3 版面区域检测（只跑 layout，不加载识别模型）。

用途：``编译后按译文版面扩框``。对**已编译的译文页**做一次快速布局识别，
拿到实际墨迹区域，交给 :mod:`babeldoc.tools.agent.layout_refine` 计算某段
下方还能扩多少。

与 :class:`babeldoc.docvision.paddle_doclayout.PaddleDocLayoutModel` 的区别：
那条路径绑定 IR 页面并会一并加载 PaddleOCR-VL（识别）。本模块只用 layout
子模型，输入是一张页面位图，ONNX 图 + CoreML EP（Apple GPU）约 0.1s/页。

模型/依赖缺失时 :attr:`PaddleLayoutRegions.available` 为 False，调用方跳过
扩框，不改变编译行为。
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: 与 ``PaddleRuntime.onnx_model`` 默认路径一致（prepare_model.py 的产物）。
DEFAULT_MODEL = Path.home() / ".cache/babeldoc/paddle-models/inference_bbox.onnx"
DEFAULT_CACHE = Path.home() / ".cache/babeldoc/paddle-runtime"
#: 检测分辨率：144dpi 下短边像素足够 PP-DocLayoutV3 稳定，且渲染开销小。
DEFAULT_DPI = 144

#: 页图 → 区域列表的可注入预言机：``func(bgr_uint8) -> list[dict]``；
#: 每个 dict 至少含 ``label``/``score``/``coordinate``（图内像素 [x0,y0,x1,y1]）。
Predictor = Callable[[np.ndarray], list[dict[str, Any]]]


@dataclass(frozen=True)
class Region:
    """一块版面区域，坐标是 IL 约定（页面坐标，y 向上，单位 pt）。"""

    label: str
    score: float
    box: tuple[float, float, float, float]


class PaddleLayoutRegions:
    """懒加载的 PP-DocLayoutV3 检测器。

    ``device`` 沿用产品约定：``auto`` / 非 ``cpu`` 时走 ONNX Runtime 的 CoreML
    Execution Provider（Apple Silicon 上用 MLProgram 跑 GPU/ANE），``cpu`` 强制
    CPU。``predictor`` 显式给出时完全不碰模型运行时（测试/替换后端用）；否则按需
    构建引擎，构建失败只记录一次并保持不可用。
    """

    def __init__(
        self,
        *,
        device: str = "auto",
        dpi: int = DEFAULT_DPI,
        predictor: Predictor | None = None,
        model_path: Path | str | None = None,
        cache_dir: Path | str | None = None,
        threads: int = 0,
    ):
        if dpi < 72:
            raise ValueError("检测 dpi 不能低于 72")
        self.device = device
        self.dpi = int(dpi)
        self._predictor = predictor
        self.model_path = Path(model_path) if model_path else DEFAULT_MODEL
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE
        self.threads = int(threads)
        self._engine = None
        self._engine_error: str | None = None

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """能否检测：注入的 predictor、已建好的引擎，或模型 + 依赖齐备。"""
        if self._predictor is not None or self._engine is not None:
            return True
        if self._engine_error is not None:
            return False
        if not self.model_path.is_file():
            return False
        return all(
            importlib.util.find_spec(name) is not None
            for name in ("onnxruntime", "paddlex", "cv2")
        )

    def _build(self):
        if self._engine is not None or self._engine_error is not None:
            return self._engine
        try:
            import paddlex
            import yaml

            from babeldoc.docvision.paddle_runtime import OnnxLayout

            config = yaml.safe_load(
                (
                    Path(paddlex.__file__).parent
                    / "configs/pipelines/PaddleOCR-VL-1.6.yaml"
                ).read_text()
            )["SubModules"]["LayoutDetection"]
            # ONNX 图与锁定版本的哈希校验在 OnnxLayout 内完成（与 PaddleRuntime 同口径）。
            self._engine = OnnxLayout(
                self.model_path,
                self.cache_dir,
                device=self.device,
                threads=self.threads,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 - 检测不可用只是跳过扩框
            self._engine_error = f"{type(exc).__name__}: {exc}"
            logger.warning("PP-DocLayoutV3 检测器不可用，跳过扩框", exc_info=True)
        return self._engine

    # ------------------------------------------------------------------
    def detect_page(self, page) -> list[Region]:
        """检测一页（pymupdf Page）的版面区域；不可用或无区域时返回 []。"""
        if not self.available:
            return []
        image = render_page_bgr(page, self.dpi)
        if image is None:
            return []
        return self.detect_image(
            image,
            page_width=float(page.rect.width),
            page_height=float(page.rect.height),
        )

    def detect_image(
        self,
        image: np.ndarray,
        *,
        page_width: float,
        page_height: float,
    ) -> list[Region]:
        """检测一张页图并换算回 IL 坐标。

        图内像素按 ``pt = px * page_pt / px`` 缩放，y 需从「图上向下」翻成
        IL 的「向上」。调用方保证页面无旋转（overlay 的旋转页门禁）。
        """
        if image is None or image.size == 0:
            return []
        raw = self._predict(image)
        if not raw:
            return []
        height_px, width_px = image.shape[:2]
        if height_px <= 0 or width_px <= 0 or page_height <= 0 or page_width <= 0:
            return []
        scale_x = page_width / width_px
        scale_y = page_height / height_px
        regions: list[Region] = []
        for item in raw:
            coords = item.get("coordinate")
            if not coords or len(coords) != 4:
                continue
            try:
                x0, y0, x1, y1 = (float(value) for value in coords)
            except (TypeError, ValueError):
                continue
            if not all(np.isfinite([x0, y0, x1, y1])) or x1 <= x0 or y1 <= y0:
                continue
            label = str(item.get("label") or "")
            score = float(item.get("score") or 0.0)
            regions.append(
                Region(
                    label=label,
                    score=score,
                    box=(
                        x0 * scale_x,
                        page_height - y1 * scale_y,
                        x1 * scale_x,
                        page_height - y0 * scale_y,
                    ),
                )
            )
        return regions

    def _predict(self, image: np.ndarray) -> list[dict[str, Any]]:
        if self._predictor is not None:
            return list(self._predictor(image) or [])
        engine = self._build()
        if engine is None:
            return []
        result = next(iter(engine([image])))
        return list((result.json.get("res") or {}).get("boxes") or [])


def render_page_bgr(page, dpi: int) -> np.ndarray | None:
    """把 pymupdf 页面渲染成 BGR uint8 数组（PP-DocLayoutV3 的输入约定）。"""
    try:
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    except Exception:  # noqa: BLE001 - 渲染失败只是本次检测跳过
        logger.warning("页面渲染失败，跳过版面检测", exc_info=True)
        return None
    if pixmap.width <= 0 or pixmap.height <= 0:
        return None
    array = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, pixmap.n
    )
    return np.ascontiguousarray(array[:, :, :3][:, :, ::-1])
