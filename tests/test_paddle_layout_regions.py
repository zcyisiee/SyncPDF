"""PaddleLayoutRegions 的设备选择与 CoreML 运行期降级（不碰真模型）。"""

from __future__ import annotations

import numpy as np
import pytest
from babeldoc.docvision.paddle_layout_regions import PaddleLayoutRegions


class _FailingEngine:
    def __call__(self, images):
        raise RuntimeError("CoreMLExecutionProvider: output_features has no value")


class _OkEngine:
    def __call__(self, images):
        return [
            type(
                "Result",
                (),
                {
                    "json": {
                        "res": {
                            "boxes": [
                                {
                                    "label": "text",
                                    "score": 0.9,
                                    "coordinate": [10, 20, 30, 45],
                                }
                            ]
                        }
                    }
                },
            )()
        ]


def test_device_default_reads_env(monkeypatch):
    monkeypatch.setenv("BDT_PADDLE_DEVICE", "CPU")
    assert PaddleLayoutRegions().device == "cpu"
    monkeypatch.setenv("BDT_PADDLE_DEVICE", "")
    assert PaddleLayoutRegions().device == "auto"
    monkeypatch.delenv("BDT_PADDLE_DEVICE", raising=False)
    assert PaddleLayoutRegions().device == "auto"
    assert PaddleLayoutRegions(device="cpu").device == "cpu"


def test_coreml_runtime_failure_falls_back_to_cpu(monkeypatch):
    detector = PaddleLayoutRegions(device="auto")
    engines: list = []

    def fake_build(_self):
        # 与真 ``_build`` 同语义：引擎按实例缓存，只在 _engine 被清空后才重建。
        # （降级路径正是靠「清空 + 重建」换掉 EP 的。）
        if _self._engine is not None:
            return _self._engine
        engines.append(_FailingEngine() if len(engines) == 0 else _OkEngine())
        _self._engine = engines[-1]
        return _self._engine

    monkeypatch.setattr(PaddleLayoutRegions, "_build", fake_build)
    image = np.zeros((50, 50, 3), dtype=np.uint8)

    regions = detector.detect_image(image, page_width=100.0, page_height=100.0)

    # 降级后第二次推理成功；像素 [10,20,30,45] × 2（100pt/50px）并翻回 IL y 向上。
    assert detector.device == "cpu"
    assert len(regions) == 1
    assert regions[0].label == "text"
    assert regions[0].box == (20.0, 10.0, 60.0, 60.0)
    # 只建两次：CoreML 那次 + 降级后的 CPU 那次。首次检测前的小图预热就是撞掉
    # CoreML 的那一次，真实页面因此直接用已降级的引擎（不再白付一次失败推理）。
    assert len(engines) == 2


def test_cpu_device_does_not_retry_on_failure(monkeypatch):
    detector = PaddleLayoutRegions(device="cpu")
    builds: list = []

    def fake_build(_self):
        builds.append(_FailingEngine())
        return builds[-1]

    monkeypatch.setattr(PaddleLayoutRegions, "_build", fake_build)
    with pytest.raises(RuntimeError, match="CoreMLExecutionProvider"):
        detector._predict(np.zeros((4, 4, 3), dtype=np.uint8))
    assert len(builds) == 1


def test_concurrent_first_detections_downgrade_once(monkeypatch):
    """多 worker 并发首检：只让一个线程去撞 CoreML，其余复用降级后的引擎。

    没有预热锁时，16 个预览 worker 同时首检会各自失败一次、各自重建一次引擎
    （实测 5 次），白付 5 次失败推理与 5 次引擎构建。
    """
    import threading

    detector = PaddleLayoutRegions(device="auto")
    engines: list = []
    guard = threading.Lock()

    def fake_build(_self):
        with guard:
            if _self._engine is not None:
                return _self._engine
            engines.append(_FailingEngine() if len(engines) == 0 else _OkEngine())
            _self._engine = engines[-1]
            return _self._engine

    monkeypatch.setattr(PaddleLayoutRegions, "_build", fake_build)
    image = np.zeros((50, 50, 3), dtype=np.uint8)
    results: list = []
    errors: list = []

    def worker():
        try:
            results.append(
                detector.detect_image(image, page_width=100.0, page_height=100.0)
            )
        except Exception as exc:  # noqa: BLE001 - 收集后统一断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 8
    assert all(len(regions) == 1 for regions in results)
    assert detector.device == "cpu"
    # 失败的 CoreML 引擎 + 降级后的 CPU 引擎，且只此两次。
    assert len(engines) == 2
