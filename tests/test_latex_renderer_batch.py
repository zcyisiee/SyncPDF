"""BatchStampRenderer（P4 整文档轮次制批编译）测试。

覆盖：

- 批 tex 结构：每段一页、逐页显式纸张/版心长度（**不用** ``\\newgeometry``）、
  段前后 ``@@S/@@E`` 标记、``\vsize`` 放大保证一段一页；
- 日志归属：``at lines X--Y`` 行号优先、``@@S n@@`` 标记兜底（页级 Overfull）；
- 行距阶梯（源行距 → ×1.1 → ×0.9）与第 4 轮起的单段回退；
- 坏段隔离：TeX 错误 / Overfull 只回退本段；批超时按二分隔离；
- 持久缓存：命名空间隔离、跨渲染器命中、命中不计编译尝试；
- 统计属性：``cache_hits`` / ``compile_seconds`` 含单段回退。
"""

from __future__ import annotations

import pymupdf
import pytest
from babeldoc.format.pdf.document_il.backend.latex_bbox import capability
from babeldoc.format.pdf.document_il.backend.latex_bbox import renderer as renderer_mod
from babeldoc.format.pdf.document_il.backend.latex_bbox import (
    renderer_batch as batch_mod,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    BboxStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampRequest
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampResult
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer_batch import (
    BatchStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.stamp_cache import StampCache

_CAPABILITY = capability.probe_latex_capability()
requires_latex = pytest.mark.skipif(
    not _CAPABILITY.available, reason="需要可用的 XeLaTeX + 中文字体"
)

_ZH = "这是一段用于批编译验证的中文正文，长度适中以便快速编译。"


def _request(key: str, body: str | None = None, width: float = 300.0, height: float = 80.0):
    return StampRequest(
        key=key,
        body=body if body is not None else _ZH * 2,
        width=width,
        height=height,
        font_size=10.0,
        lead=13.5,
    )


# --------------------------------------------------------------------------- #
# 批 tex 结构（不需要 xelatex）
# --------------------------------------------------------------------------- #
def test_batch_tex_has_one_page_per_segment():
    renderer = BatchStampRenderer(_CAPABILITY)
    requests = [_request("a"), _request("b", width=250.0, height=60.0), _request("c")]
    tex, ranges = renderer.build_batch_tex(requests)

    assert tex.count("\\message{@@S ") == 3
    assert tex.count("\\message{@@E ") == 3
    assert tex.count("\\newpage") == 2
    assert tex.count("\\vsize=6000.0000bp") == 3
    assert "minipage" not in tex
    for index in range(3):
        assert f"\\message{{@@S {index}@@}}" in tex
        assert f"\\message{{@@E {index}@@}}" in tex
    # 页间用 \newpage 分隔（不在最后一段之后，避免补出空白页）。
    assert tex.count("\\newpage") == 2
    assert len(ranges) == 3


def test_batch_tex_sets_page_geometry_explicitly_per_page():
    """逐页显式设纸张与版心长度：``geometry`` 的 ``\\newgeometry`` 改不了纸张。"""
    renderer = BatchStampRenderer(_CAPABILITY)
    tex, _ranges = renderer.build_batch_tex(
        [_request("a", width=300.0, height=80.0), _request("b", width=250.0, height=60.0)]
    )

    assert "\\newgeometry" not in tex
    assert "\\pdfpagewidth=300.0000bp \\pdfpageheight=80.0000bp" in tex
    assert "\\pdfpagewidth=250.0000bp \\pdfpageheight=60.0000bp" in tex
    for width, height in ((300.0, 80.0), (250.0, 60.0)):
        assert f"\\hsize={width:.4f}bp" in tex
        assert f"\\textwidth={width:.4f}bp" in tex
        assert f"\\textheight={height:.4f}bp" in tex
        assert f"\\columnwidth={width:.4f}bp" in tex
        assert f"\\linewidth={width:.4f}bp" in tex
    # 一段一页靠放大 \vsize（正文直放页面，与单段渲染同构）：不能用 minipage/
    # \vbox 包装，它们会改变首行缩进或 \hangindent 的语义。
    assert "\\vsize=6000.0000bp" in tex
    assert "minipage" not in tex and "\\vbox" not in tex


def test_batch_tex_ranges_match_segment_bodies():
    """行号区间必须覆盖各自的正文行：``at lines X--Y`` 归属靠它。"""
    renderer = BatchStampRenderer(_CAPABILITY)
    body_a = "AAAA" * 5
    body_b = "BBBB" * 5
    tex, ranges = renderer.build_batch_tex([_request("a", body_a), _request("b", body_b)])
    lines = tex.split("\n")

    for body, (start, end) in zip((body_a, body_b), ranges, strict=True):
        segment = "\n".join(lines[start - 1 : end])
        assert body in segment
        assert "\\message{@@S" in segment
        assert "\\message{@@E" in segment


def test_batch_tex_keeps_font_setup_in_preamble():
    """``\\setmainfont`` 只能在导言区：批 tex 里不能在正文里重设字体。"""
    renderer = BatchStampRenderer(_CAPABILITY)
    tex, _ranges = renderer.build_batch_tex([_request("a"), _request("b")])
    document_start = tex.index("\\begin{document}")

    assert tex.index("\\setCJKmainfont") < document_start
    assert "\\setmainfont" not in tex[document_start:] or tex.index(
        "\\setmainfont"
    ) < document_start


# --------------------------------------------------------------------------- #
# 日志归属（纯函数）
# --------------------------------------------------------------------------- #
def test_attribute_log_prefers_line_numbers():
    """``at lines X--Y`` 按行号归属（不受 TeX 日志缓冲顺序影响）。"""
    ranges = [(10, 19), (20, 29)]
    log = "\n".join(
        [
            "@@S 0@@ @@E 0@@",
            "Overfull \\hbox (8.03pt too wide) in paragraph at lines 24--28",
            "@@S 1@@ @@E 1@@",
        ]
    )
    info = batch_mod._attribute_log(log, ranges)
    assert info["segments"][1]["overfull_hbox"] == 1
    assert 0 not in info["segments"]
    assert info["starts"] == {0, 1}
    assert info["ends"] == {0, 1}


def test_attribute_log_falls_back_to_markers_without_line_numbers():
    """页级 Overfull 没有行号：按「最近 ``@@S n@@``」归属第 n 段。"""
    ranges = [(10, 19), (20, 29)]
    log = "\n".join(
        [
            "@@S 0@@",
            "Overfull \\vbox (1.1pt too high) has occurred while \\output is active",
            "@@E 0@@",
            "@@S 1@@ @@E 1@@",
        ]
    )
    info = batch_mod._attribute_log(log, ranges)
    assert info["segments"][0]["overfull_vbox"] == 1
    assert 1 not in info["segments"]


def test_attribute_log_maps_tex_error_by_context_line():
    """``!`` 错误用上下文 ``l.NNN`` 行号归属到出错段。"""
    ranges = [(10, 19), (20, 29)]
    log = "\n".join(
        [
            "@@S 0@@",
            "! Missing $ inserted.",
            "<inserted text>",
            "                $",
            "l.25 body text here",
            "@@E 0@@",
            "@@S 1@@ @@E 1@@",
        ]
    )
    info = batch_mod._attribute_log(log, ranges)
    assert info["segments"][1]["errors"] == ["! Missing $ inserted."]


def test_attribute_log_flags_preamble_error():
    ranges = [(10, 19)]
    log = "\n".join(["! LaTeX Error: Can be used only in preamble.", "@@S 0@@ @@E 0@@"])
    info = batch_mod._attribute_log(log, ranges)
    assert info["preamble_error"] is True


# --------------------------------------------------------------------------- #
# 轮次状态机
# --------------------------------------------------------------------------- #
def test_round_variants_ladder_order():
    """第 1 轮只编首选档；第 2 轮按优先级编完行距 ±10% 与 ×0.95 缩小档。"""
    request = _request("a")
    assert batch_mod._round_variants(request, 1) == [request]

    second = batch_mod._round_variants(request, 2)
    assert [round(v.font_size, 4) for v in second] == [10.0, 10.0, 9.5, 9.025, 8.5738]
    assert [round(v.lead, 3) for v in second] == [
        round(13.5 * 1.1, 3),
        round(13.5 * 0.9, 3),
        round(13.5 * 0.95, 3),
        round(13.5 * 0.9025, 3),
        round(13.5 * 0.857375, 3),
    ]
    # 每档都是独立请求对象（互不覆盖）。
    assert all(variant is not request for variant in second)


def test_batch_empty_and_single_request_short_circuit(tmp_path):
    renderer = BatchStampRenderer(_CAPABILITY)
    assert renderer.render_many([]) == {}

    fallback_calls = []
    original = renderer.fallback.render_many

    def fake_render_many(requests):
        fallback_calls.append([request.key for request, _workdir in requests])
        return original(requests)

    renderer.fallback.render_many = fake_render_many  # type: ignore[method-assign]
    renderer.render_many([(_request("only"), tmp_path)])
    # 单段没有批量收益：直接走单段渲染（不启动批 tex）。
    assert fallback_calls == [["only"]]
    assert renderer.batch_count == 0


def test_batch_rounds_then_falls_back_to_single(tmp_path, monkeypatch):
    """2 轮批编译（首选档 → 剩余阶梯档位）后剩余段交单段渲染。"""
    renderer = BatchStampRenderer(_CAPABILITY, max_workers=1)
    rounds: list[tuple[int, list[float]]] = []

    monkeypatch.setattr(
        BatchStampRenderer, "_run_xelatex", lambda _self, *_a, **_k: ("", False)
    )

    def fake_evaluate(
        _self, items, _ranges, _block_dir, _stem, _log, attempt, _base_by_key
    ):
        rounds.append((attempt, sorted({round(req.lead, 3) for _k, req in items})))
        return {
            key: StampResult(key=key, ok=False, reason="s0:vertical-overflow")
            for key, _request in items
        }

    monkeypatch.setattr(BatchStampRenderer, "_evaluate_block", fake_evaluate)
    fallback_requests: list[str] = []
    monkeypatch.setattr(
        BboxStampRenderer,
        "render_many",
        lambda _self, requests: {
            request.key: (
                fallback_requests.append(request.key)
                or StampResult(key=request.key, ok=False, reason="s0:vertical-overflow")
            )
            for request, _workdir in requests
        },
    )

    results = renderer.render_many(
        [(_request("a"), tmp_path), (_request("b"), tmp_path)]
    )

    assert [attempt for attempt, _leads in rounds] == [1, 2]
    assert rounds[0][1] == [round(13.5, 3)]
    assert rounds[1][1] == sorted(
        round(value, 3)
        for value in (
            13.5 * 0.857375,
            13.5 * 0.9,
            13.5 * 0.9025,
            13.5 * 0.95,
            13.5 * 1.1,
        )
    )
    # 2 轮仍失败 → 两个段都交单段渲染。
    assert sorted(fallback_requests) == ["a", "b"]
    assert all(result.reason == "s0:vertical-overflow" for result in results.values())


def test_batch_content_failure_is_not_retried(tmp_path, monkeypatch):
    """内容级失败（text-mismatch）只编译一轮，不进单段渲染。"""
    renderer = BatchStampRenderer(_CAPABILITY, max_workers=1)
    attempts: list[int] = []
    monkeypatch.setattr(
        BatchStampRenderer, "_run_xelatex", lambda _self, *_a, **_k: ("", False)
    )

    def fake_evaluate(
        _self, items, _ranges, _block_dir, _stem, _log, attempt, _base_by_key
    ):
        attempts.append(attempt)
        return {
            key: StampResult(key=key, ok=False, reason="s0:text-mismatch")
            for key, _request in items
        }

    monkeypatch.setattr(BatchStampRenderer, "_evaluate_block", fake_evaluate)
    fallback_calls: list[str] = []
    monkeypatch.setattr(
        BboxStampRenderer,
        "render_many",
        lambda _self, requests: (
            fallback_calls.extend(request.key for request, _wd in requests) or {}
        ),
    )

    results = renderer.render_many(
        [(_request("a"), tmp_path), (_request("b"), tmp_path)]
    )

    assert attempts == [1]
    assert fallback_calls == []
    assert results["a"].reason == "s0:text-mismatch"


def test_batch_timeout_splits_and_isolates_bad_segment(tmp_path, monkeypatch):
    """批超时按二分重试：坏段单独判 timeout，其余段不受影响。"""
    renderer = BatchStampRenderer(_CAPABILITY, max_workers=1, block_size=4)

    timeouts: list[float] = []

    def fake_run(  # noqa: ARG001 - 与 _run_xelatex 同签名
        _self, tex, _workdir, _stem, timeout
    ):
        timeouts.append(timeout)
        if "TIMEOUTSEG" in tex:
            return "", True
        return "@@S 0@@ @@E 0@@", False

    monkeypatch.setattr(BatchStampRenderer, "_run_xelatex", fake_run)
    monkeypatch.setattr(
        BatchStampRenderer,
        "_evaluate_block",
        lambda _self, items, *_a, **_k: {
            key: StampResult(key=key, ok=False, reason="batch-attribution-failed")
            for key, _request in items
        },
    )
    fallback_ok: list[str] = []
    monkeypatch.setattr(
        BboxStampRenderer,
        "render_many",
        lambda _self, requests: {
            request.key: (
                fallback_ok.append(request.key)
                or StampResult(key=request.key, ok=True, pdf_path="/dev/null")
            )
            for request, _workdir in requests
        },
    )

    results = renderer.render_many(
        [
            (_request("good1"), tmp_path),
            (_request("bad", body="TIMEOUTSEG 坏段"), tmp_path),
            (_request("good2"), tmp_path),
        ]
    )

    # 坏段最终判 timeout；其余段由单段渲染兜住（结果仍可用）。
    assert results["bad"].reason == "timeout"
    assert results["bad"].ok is False
    assert results["good1"].ok is True
    assert results["good2"].ok is True
    assert sorted(fallback_ok) == ["good1", "good2"]
    # 批超时 = 基础 45s + 0.2s × 段数（3 段 → 45.6s；二分后各自按段数算）。
    assert timeouts[0] == pytest.approx(45.0 + 0.2 * 3)
    assert all(value >= 45.0 for value in timeouts)


# --------------------------------------------------------------------------- #
# 真实编译
# --------------------------------------------------------------------------- #
@requires_latex
def test_batch_renders_all_segments_as_single_page_stamps(tmp_path):
    renderer = BatchStampRenderer(_CAPABILITY, max_workers=4)
    requests = [
        _request("a", width=300.0, height=80.0),
        _request("b", width=250.0, height=60.0),
        _request("c", width=260.0, height=70.0),
    ]
    results = renderer.render_many([(request, tmp_path) for request in requests])

    assert all(result.ok for result in results.values()), {
        key: result.reason for key, result in results.items()
    }
    assert renderer.batch_count == 1
    assert renderer.segment_count == 3
    for request in requests:
        result = results[request.key]
        with pymupdf.open(result.pdf_path) as doc:
            assert len(doc) == 1  # 贴片必须是单页
            page = doc[0]
            assert page.rect.width == pytest.approx(request.width, abs=0.01)
            assert page.rect.height == pytest.approx(request.height, abs=0.01)
            assert page.get_text().strip()


@requires_latex
def test_batch_matches_single_render_results(tmp_path):
    """批编译结果与单段渲染逐段一致（文本/行边界/页尺寸）。"""
    requests = [_request("a"), _request("b", width=260.0, height=70.0)]
    batch = BatchStampRenderer(_CAPABILITY, max_workers=2).render_many(
        [(request, tmp_path / "batch") for request in requests]
    )
    single = BboxStampRenderer(_CAPABILITY, max_workers=2).render_many(
        [(request, tmp_path / "single") for request in requests]
    )

    def geometry(pdf_path):
        with pymupdf.open(pdf_path) as doc:
            page = doc[0]
            lines = {
                round(word[1], 1): (round(word[0], 1), round(word[2], 1))
                for word in page.get_text("words")
            }
            return (
                renderer_mod.normalize_rendered_text(page.get_text()),
                sorted(lines),
                (round(page.rect.width, 2), round(page.rect.height, 2)),
            )

    for request in requests:
        assert batch[request.key].ok and single[request.key].ok
        assert geometry(batch[request.key].pdf_path) == geometry(
            single[request.key].pdf_path
        )


@requires_latex
def test_batch_isolates_bad_latex_segment(tmp_path):
    """未闭合数学模式只影响本段：其余段照常编译并贴片。"""
    renderer = BatchStampRenderer(_CAPABILITY, max_workers=4)
    requests = [
        _request("good1"),
        _request("bad", body=r"错误段：$ \alpha 未闭合。"),
        _request("good2"),
    ]
    results = renderer.render_many([(request, tmp_path) for request in requests])

    assert results["good1"].ok is True
    assert results["good2"].ok is True
    assert results["bad"].ok is False
    assert results["bad"].reason.startswith("compile:")


@requires_latex
def test_batch_isolates_overfull_segment(tmp_path):
    """超长不可断 token 只影响本段（Overfull hbox 归属本段）。"""
    renderer = BatchStampRenderer(_CAPABILITY, max_workers=4, timeout_seconds=30.0)
    requests = [
        _request("good"),
        _request("overfull", body="溢出段：" + "x" * 200, width=200.0),
    ]
    results = renderer.render_many([(request, tmp_path) for request in requests])

    assert results["good"].ok is True
    assert results["overfull"].ok is False
    assert "overfull" in results["overfull"].reason


# --------------------------------------------------------------------------- #
# 持久缓存
# --------------------------------------------------------------------------- #
def test_stamp_cache_namespace_isolates_keys(tmp_path):
    request = _request("a")
    first = StampCache(tmp_path, namespace="template-a")
    second = StampCache(tmp_path, namespace="template-b")
    assert first.key_for(request) != second.key_for(request)
    assert first.key_for(request) == first.key_for(request)


def test_stamp_cache_round_trip(tmp_path):
    request = _request("a")
    cache = StampCache(tmp_path, namespace="ns")
    assert cache.get(request) is None

    stamp_pdf = tmp_path / "stamp.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=100, height=40)
    page.insert_text((5, 20), "cached", fontsize=9)
    doc.save(stamp_pdf)
    doc.close()

    stored = cache.put(
        request,
        StampResult(
            key="a",
            ok=True,
            pdf_path=str(stamp_pdf),
            font_size=10.0,
            scale=0.95,
            lead=13.5,
            seconds=1.25,
            compile_attempts=2,
            reason="ok",
        ),
    )
    assert stored is not None and stored.ok
    assert (tmp_path / f"{cache.key_for(request)}.pdf").is_file()

    hit = StampCache(tmp_path, namespace="ns").get(request)
    assert hit is not None and hit.ok
    assert hit.font_size == 10.0 and hit.scale == 0.95 and hit.lead == 13.5
    # 命中没有编译：尝试次数与耗时归零（原始次数只在元数据里留痕）。
    assert hit.compile_attempts == 0 and hit.seconds == 0.0


def test_stamp_cache_ignores_failed_and_missing_results(tmp_path):
    cache = StampCache(tmp_path, namespace="ns")
    request = _request("a")
    assert cache.put(request, StampResult(key="a", ok=False, reason="boom")) is None
    assert (
        cache.put(
            request,
            StampResult(key="a", ok=True, pdf_path=str(tmp_path / "missing.pdf")),
        )
        is None
    )
    assert cache.get(request) is None


@requires_latex
def test_render_one_hits_persistent_cache(tmp_path):
    """二次回放（新渲染器实例）命中落盘缓存，不再编译。"""
    cache = StampCache(tmp_path / "cache", namespace="ns")
    request = _request("a")
    first = BboxStampRenderer(_CAPABILITY, cache=cache).render_one(request, tmp_path)
    assert first.ok and first.compile_attempts >= 1

    second_renderer = BboxStampRenderer(_CAPABILITY, cache=cache)
    second = second_renderer.render_one(request, tmp_path / "again")
    assert second.ok
    assert second_renderer.cache_hits == 1
    assert second.compile_attempts == 0
    assert (tmp_path / "cache") in __import__("pathlib").Path(second.pdf_path).parents


@requires_latex
def test_batch_hits_persistent_cache_on_second_run(tmp_path):
    cache = StampCache(tmp_path / "cache", namespace="ns")
    requests = [_request("a"), _request("b", width=260.0, height=70.0)]
    first = BatchStampRenderer(_CAPABILITY, max_workers=2, cache=cache).render_many(
        [(request, tmp_path / "run1") for request in requests]
    )
    assert all(result.ok for result in first.values())
    assert all(result.compile_attempts >= 1 for result in first.values())

    second_renderer = BatchStampRenderer(_CAPABILITY, max_workers=2, cache=cache)
    second = second_renderer.render_many(
        [(request, tmp_path / "run2") for request in requests]
    )
    assert all(result.ok for result in second.values())
    assert second_renderer.cache_hits == len(requests)
    assert second_renderer.batch_count == 0  # 全命中，不启动批编译


def test_build_stamp_cache_requires_working_dir(tmp_path):
    class _Config:
        working_dir = None

    class _ConfigWithDir:
        working_dir = str(tmp_path)

    assert batch_build_cache(_Config()) is None
    cache = batch_build_cache(_ConfigWithDir())
    assert cache is not None
    assert cache.cache_dir.name == "latex_cache"


def batch_build_cache(config):
    from babeldoc.format.pdf.document_il.backend.latex_bbox.stamp_cache import (
        build_stamp_cache,
    )

    return build_stamp_cache(config, _CAPABILITY)
