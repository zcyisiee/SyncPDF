"""Local compiler acceptance: real PDF composition, bounded renderer seam."""

import json
from types import SimpleNamespace

import pymupdf
import pytest
from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.block_compile import BlockCompiler
from babeldoc_tools.serve.block_compile import valid_box
from babeldoc_tools.serve.jobs import JobRecord
from babeldoc_tools.serve.jobs import utc_now
from babeldoc_tools.serve.store import DocumentStore


@pytest.fixture
def local(tmp_path, monkeypatch):
    workdir = tmp_path / "paper"
    (workdir / "output").mkdir(parents=True)
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=400)
        page.insert_text((30, 60), "Old block one")
        page.insert_text((30, 180), "Untouched block two")
        pdf.save(workdir / "output/paper.mono.pdf")
    store = DocumentStore.for_root(tmp_path)
    compiler = BlockCompiler(store, None)
    rows = [
        SimpleNamespace(
            id="P1",
            page=1,
            target="New block one",
            geometry={"src_box": [20, 320, 200, 370]},
        ),
        SimpleNamespace(
            id="P2",
            page=1,
            target="New block two",
            geometry={"src_box": [20, 190, 200, 250]},
        ),
    ]
    monkeypatch.setattr(compiler, "_rows", lambda _did: rows)
    calls = []

    def render(_workdir, pid, target, box, temporary, _cache, **_kwargs):
        calls.append(pid)
        path = temporary / "stamp.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=box[2] - box[0], height=box[3] - box[1])
            page.insert_text((5, 20), target)
            pdf.save(path)
        return SimpleNamespace(ok=True, pdf_path=str(path), font_size=11), False

    monkeypatch.setattr("babeldoc_tools.serve.block_compile.render_request", render)
    return compiler, calls, rows


def record(pid="P1"):
    return JobRecord(
        job_id="j-local",
        did="paper",
        action="compile",
        status="running",
        created_at=utc_now(),
        paragraph_id=pid,
        revision=0,
    )


def test_only_one_stamp_and_other_text_survives(local):
    compiler, calls, _ = local
    result = compiler.compile(record())
    assert calls == ["P1"]
    from babeldoc_tools.serve.asset_store import AssetStore

    assets = AssetStore(compiler.store.store_base, compiler.store.database)
    with pymupdf.open(assets.resolve(result["asset"])) as pdf:
        text = pdf[0].get_text()
        assert "New block one" in text
        assert "Old block one" not in text
        assert "Untouched block two" in text
    compiler.compile(record("P2"))
    assert calls == ["P1", "P2"]


def test_shrunk_stamp_is_rerendered_with_expanded_box(local, monkeypatch):
    """P6：贴片被缩字时按译文版面下扩一段重渲染，patch 记录扩后的 box。"""
    compiler, _, _ = local
    from babeldoc.tools.agent import layout_refine
    from babeldoc_tools.serve import block_compile

    boxes: list[list[float]] = []

    def render(_workdir, _pid, target, box, temporary, _cache, **_kwargs):
        boxes.append(list(box))
        path = temporary / f"stamp-{len(boxes)}.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=box[2] - box[0], height=box[3] - box[1])
            page.insert_text((5, 20), target)
            pdf.save(path)
        first = len(boxes) == 1
        return (
            SimpleNamespace(
                ok=True,
                pdf_path=str(path),
                font_size=11,
                scale=0.8 if first else 1.0,
            ),
            False,
        )

    monkeypatch.setattr(block_compile, "render_request", render)
    monkeypatch.setattr(
        block_compile.BlockCompiler, "_layout_detector", lambda _self: object()
    )
    seen: list[tuple] = []

    def fake_plan(_page, box, detector, **_kwargs):
        seen.append((box, detector))
        return (box[0], box[1] - 40.0, box[2], box[3])

    monkeypatch.setattr(layout_refine, "plan_page_expansion", fake_plan)

    result = compiler.compile(record())

    assert len(boxes) == 2
    assert boxes[0][1] == 320.0 and boxes[1][1] == 280.0
    # 只有被缩字的那一次触发检测；重渲染后不再重复扩框。
    assert len(seen) == 1
    assert result["cache_hit"] is False


def test_upward_expansion_is_rerendered_with_raised_top(local, monkeypatch):
    """向上扩也算升级：重渲染用抬高上沿的新框，patch 记录新框。"""
    compiler, _, _ = local
    from babeldoc.tools.agent import layout_refine
    from babeldoc_tools.serve import block_compile

    boxes: list[list[float]] = []

    def render(_workdir, _pid, target, box, temporary, _cache, **_kwargs):
        boxes.append(list(box))
        path = temporary / f"stamp-{len(boxes)}.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=box[2] - box[0], height=box[3] - box[1])
            page.insert_text((5, 20), target)
            pdf.save(path)
        return (
            SimpleNamespace(ok=True, pdf_path=str(path), font_size=11, scale=0.8),
            False,
        )

    monkeypatch.setattr(block_compile, "render_request", render)
    monkeypatch.setattr(
        block_compile.BlockCompiler, "_layout_detector", lambda _self: object()
    )
    monkeypatch.setattr(
        layout_refine,
        "plan_page_expansion",
        lambda _page, box, _detector, **_kwargs: (
            box[0],
            box[1],
            box[2],
            box[3] + 20.0,
        ),
    )

    result = compiler.compile(record())

    assert len(boxes) == 2
    assert boxes[0][3] == 370.0 and boxes[1][3] == 390.0
    with compiler.store.database._lock:
        row = compiler.store.database.connection.execute(
            "SELECT payload FROM local_pages WHERE document_id='paper' AND page=1"
        ).fetchone()
    assert json.loads(row[0])["patches"]["P1"]["box"][3] == 390.0
    assert result["asset"]


def test_unshrunk_stamp_is_not_rerendered(local, monkeypatch):
    """未缩字（scale=1.0）时不检测版面、不重渲染。"""
    compiler, _, _ = local
    from babeldoc.tools.agent import layout_refine
    from babeldoc_tools.serve import block_compile

    boxes: list[list[float]] = []

    def render(_workdir, _pid, target, box, temporary, _cache, **_kwargs):
        boxes.append(list(box))
        path = temporary / f"stamp-{len(boxes)}.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=box[2] - box[0], height=box[3] - box[1])
            page.insert_text((5, 20), target)
            pdf.save(path)
        return (
            SimpleNamespace(ok=True, pdf_path=str(path), font_size=11, scale=1.0),
            False,
        )

    monkeypatch.setattr(block_compile, "render_request", render)
    monkeypatch.setattr(
        block_compile.BlockCompiler, "_layout_detector", lambda _self: object()
    )
    monkeypatch.setattr(
        layout_refine,
        "plan_page_expansion",
        lambda *_args, **_kwargs: pytest.fail("未缩字不应扩框"),
    )

    compiler.compile(record())
    assert len(boxes) == 1


def test_failed_expansion_retry_keeps_original_stamp(local, monkeypatch):
    """扩框重渲染失败（超时/TeX 错）不算升级：保留被缩字但可用的原贴片。"""
    compiler, _, _ = local
    from babeldoc.tools.agent import layout_refine
    from babeldoc_tools.serve import block_compile

    boxes: list[list[float]] = []

    def render(_workdir, _pid, target, box, temporary, _cache, **_kwargs):
        boxes.append(list(box))
        if len(boxes) == 2:
            raise ToolError("compile_failed", "重渲染失败")
        path = temporary / "stamp.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=box[2] - box[0], height=box[3] - box[1])
            page.insert_text((5, 20), target)
            pdf.save(path)
        return (
            SimpleNamespace(ok=True, pdf_path=str(path), font_size=11, scale=0.8),
            False,
        )

    monkeypatch.setattr(block_compile, "render_request", render)
    monkeypatch.setattr(
        block_compile.BlockCompiler, "_layout_detector", lambda _self: object()
    )
    monkeypatch.setattr(
        layout_refine,
        "plan_page_expansion",
        lambda _page, box, _detector, **_kwargs: (box[0], box[1] - 40.0, box[2], box[3]),
    )

    result = compiler.compile(record())

    assert len(boxes) == 2
    with compiler.store.database._lock:
        row = compiler.store.database.connection.execute(
            "SELECT payload FROM local_pages WHERE document_id='paper' AND page=1"
        ).fetchone()
    patch = json.loads(row[0])["patches"]["P1"]
    assert patch["box"][1] == 320.0  # 原框，不是扩后的 280.0
    assert result["asset"]


def test_failed_stamp_preserves_page_and_previous_patch(local, monkeypatch):
    compiler, _, _ = local
    compiler.compile(record())
    database = compiler.store.database
    before = database.connection.execute("SELECT payload FROM local_pages").fetchone()[
        0
    ]

    def fail(*_args, **_kwargs):
        raise ToolError("compile_failed", "failed stamp")

    monkeypatch.setattr("babeldoc_tools.serve.block_compile.render_request", fail)
    with pytest.raises(ToolError, match="failed stamp"):
        compiler.compile(record())
    assert (
        database.connection.execute("SELECT payload FROM local_pages").fetchone()[0]
        == before
    )
    assert list((compiler.store.store_base / "tmp").iterdir()) == []


def test_revision_change_during_render_does_not_publish(local, monkeypatch):
    compiler, _, _ = local
    from babeldoc_tools.serve import block_compile

    render = block_compile.render_request

    def changed(*args, **kwargs):
        result = render(*args, **kwargs)
        compiler.store.database.save_draft(
            "paper", {"revision": 1, "paragraphs": {}}, expected_revision=0
        )
        return result

    monkeypatch.setattr(block_compile, "render_request", changed)
    with pytest.raises(ToolError) as error:
        compiler.compile(record())
    assert error.value.code == "stale_job"
    assert (
        compiler.store.database.connection.execute(
            "SELECT COUNT(*) FROM local_pages"
        ).fetchone()[0]
        == 0
    )


def test_block_endpoint_lifecycle_and_asset_download(local, monkeypatch):
    import time

    from babeldoc_tools.serve.app import create_app
    from fastapi.testclient import TestClient

    compiler, calls, rows = local
    monkeypatch.setattr(BlockCompiler, "_rows", lambda _self, _did: rows)
    with TestClient(create_app(compiler.store)) as client:
        response = client.post(
            "/api/v1/documents/paper/blocks/P1/compile", json={"base_revision": 0}
        )
        assert response.status_code == 202, response.text
        jid = response.json()["job_id"]
        for _ in range(200):
            job = client.get(f"/api/v1/jobs/{jid}").json()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        assert job["status"] == "succeeded", job
        assert calls == ["P1"]
        preview = compiler.store.database.connection.execute(
            "SELECT asset_sha256 FROM local_previews"
        ).fetchone()[0]
        downloaded = client.get(f"/api/v1/documents/paper/assets/{preview}")
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b"%PDF")
        denied = client.get("/api/v1/documents/paper/assets/" + "a" * 64)
        assert denied.status_code == 404


# --------------------------------------------------------------------------- #
# 浮动阶梯（P7）：跨栏横向扩 + 跨页整框迁移 + 样式覆盖透传
# --------------------------------------------------------------------------- #
def _multi_page_store(tmp_path, monkeypatch, *, pages: int = 2):
    """与 ``local`` fixture 同构，但 baseline 有 ``pages`` 页、只有 P1 一个块。"""
    workdir = tmp_path / "paper"
    (workdir / "output").mkdir(parents=True)
    with pymupdf.open() as pdf:
        for index in range(pages):
            page = pdf.new_page(width=400, height=400)
            page.insert_text((30, 60), f"Old page {index + 1}")
        pdf.save(workdir / "output/paper.mono.pdf")
    store = DocumentStore.for_root(tmp_path)
    compiler = BlockCompiler(store, None)
    rows = [
        SimpleNamespace(
            id="P1",
            page=1,
            target="New block one",
            geometry={"src_box": [20, 320, 200, 370]},
        )
    ]
    monkeypatch.setattr(compiler, "_rows", lambda _did: rows)
    return compiler, rows


def _stamp_render(boxes, *, scale=0.7):
    """假 render_request：记录每次 box，返回可缩字贴片。"""

    def render(_workdir, _pid, target, box, temporary, _cache, **_kwargs):
        boxes.append(list(box))
        path = temporary / f"stamp-{len(boxes)}.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=box[2] - box[0], height=box[3] - box[1])
            page.insert_text((5, 20), target)
            pdf.save(path)
        first = len(boxes) == 1
        return (
            SimpleNamespace(
                ok=True, pdf_path=str(path), font_size=11, scale=scale if first else 1.0
            ),
            False,
        )

    return render


def _page_text(store, page_number):
    from babeldoc_tools.serve.asset_store import AssetStore

    with store.database._lock:
        row = store.database.connection.execute(
            "SELECT page_asset FROM pages WHERE document_id='paper' AND page=?",
            (page_number,),
        ).fetchone()
    assert row is not None, f"page {page_number} asset missing"
    assets = AssetStore(store.store_base, store.database)
    with pymupdf.open(assets.resolve(row[0])) as pdf:
        return pdf[0].get_text()


def test_float_widen_rerenders_with_wider_box(tmp_path, monkeypatch):
    """同栏无净空、右邻栏空闲：横向扩框重渲染，贴片仍在本页。"""
    from babeldoc.tools.agent import layout_refine
    from babeldoc_tools.serve import block_compile

    compiler, _rows = _multi_page_store(tmp_path, monkeypatch, pages=1)
    boxes: list[list[float]] = []
    monkeypatch.setattr(block_compile, "render_request", _stamp_render(boxes))
    monkeypatch.setattr(
        block_compile.BlockCompiler, "_layout_detector", lambda _self: object()
    )
    monkeypatch.setattr(
        layout_refine, "plan_page_expansion", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        layout_refine,
        "plan_widen_page_expansion",
        lambda _page, box, _detector, **_kw: (box[0], box[1], box[2] + 60.0, box[3]),
    )

    result = compiler.compile(record())

    assert len(boxes) == 2
    assert boxes[1][2] == boxes[0][2] + 60.0
    patch = _patch(compiler, page=1)["P1"]
    assert patch["page"] == 1 and patch["box"][2] == pytest.approx(260.0)
    assert result["stamp_page"] == 1
    assert "New block one" in _page_text(compiler.store, 1)


def test_float_to_next_page_moves_stamp(tmp_path, monkeypatch):
    """同栏/跨栏都无净空：整框迁到下一页空闲区间，两页都重新合成。"""
    from babeldoc.tools.agent import layout_refine
    from babeldoc_tools.serve import block_compile

    compiler, _rows = _multi_page_store(tmp_path, monkeypatch, pages=2)
    boxes: list[list[float]] = []
    monkeypatch.setattr(block_compile, "render_request", _stamp_render(boxes))
    monkeypatch.setattr(
        block_compile.BlockCompiler, "_layout_detector", lambda _self: object()
    )
    monkeypatch.setattr(
        layout_refine, "plan_page_expansion", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        layout_refine, "plan_widen_page_expansion", lambda *_a, **_k: None
    )
    moved = (20.0, 300.0, 200.0, 350.0)
    monkeypatch.setattr(
        layout_refine, "plan_next_page_float", lambda *_a, **_k: moved
    )

    result = compiler.compile(record())

    assert len(boxes) == 2 and boxes[1] == list(moved)
    patch = _patch(compiler, page=1)["P1"]
    assert patch["page"] == 2 and patch["box"] == list(moved)
    assert result["stamp_page"] == 2 and result["previous_stamp_page"] == 1
    # 主页不再有新贴片文本，下一页有；两页资产都重新发布。
    assert "New block one" not in _page_text(compiler.store, 1)
    assert "New block one" in _page_text(compiler.store, 2)


def test_float_obstacles_include_settled_sibling_stamps(tmp_path, monkeypatch):
    """回归：兄弟贴片的落点必须进下一个块的障碍集，否则两个贴片会叠在一起。

    真实故障（58 页论文）：浮动规划只看原文 baseline 页，看不见已落定的贴片，
    于是 84 个跨页迁移**全部**顶对齐到落点页同一条顶部净空，产生 119 对肉眼
    可见的重叠。这里断言第二个块规划时确实收到了第一个块的框。
    """
    from babeldoc.tools.agent import layout_refine
    from babeldoc_tools.serve import block_compile

    compiler, rows = _multi_page_store(tmp_path, monkeypatch, pages=2)
    rows.append(
        SimpleNamespace(
            id="P2", page=1, target="New block two", geometry={"src_box": [210, 320, 380, 370]}
        )
    )
    # 每次编译都返回「被缩字」的贴片，两个块都会走浮动阶梯。
    def render(_workdir, _pid, target, box, temporary, _cache, **_kwargs):
        path = temporary / f"stamp-{_pid}.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=box[2] - box[0], height=box[3] - box[1])
            page.insert_text((5, 20), target)
            pdf.save(path)
        return SimpleNamespace(ok=True, pdf_path=str(path), font_size=11, scale=0.7), False

    monkeypatch.setattr(block_compile, "render_request", render)
    monkeypatch.setattr(
        block_compile.BlockCompiler,
        "_layout_cache",
        lambda _self: SimpleNamespace(
            detector=object(), evidence=lambda _page, _number: ([(0, 0, 1, 1)], [])
        ),
    )
    monkeypatch.setattr(layout_refine, "plan_page_expansion", lambda *_a, **_k: None)
    monkeypatch.setattr(
        layout_refine, "plan_widen_page_expansion", lambda *_a, **_k: None
    )
    seen: list[list] = []
    landing = (20.0, 300.0, 200.0, 350.0)

    def plan(_page, _box, _detector, *, reserved=(), **_kw):
        seen.append([list(item) for item in reserved])
        return landing

    monkeypatch.setattr(layout_refine, "plan_next_page_float", plan)

    compiler.compile(record())
    compiler.compile(record(pid="P2"))

    # 第一个块规划时落点页还空着（只有 P2 的原文框，它在第 1 页 → 不算障碍）。
    assert landing not in [tuple(box) for box in seen[0]]
    # 第二个块必须看见 P1 已经占住的那块地。
    assert list(landing) in seen[1]


def test_float_back_home_erases_old_foreign_stamp(tmp_path, monkeypatch):
    """迁移后再编译回主页：下一页上的旧贴片要被擦掉（上一版落点页重合成）。"""
    from babeldoc.tools.agent import layout_refine
    from babeldoc_tools.serve import block_compile

    compiler, _rows = _multi_page_store(tmp_path, monkeypatch, pages=2)
    boxes: list[list[float]] = []
    monkeypatch.setattr(block_compile, "render_request", _stamp_render(boxes))
    monkeypatch.setattr(
        block_compile.BlockCompiler, "_layout_detector", lambda _self: object()
    )
    monkeypatch.setattr(
        layout_refine, "plan_page_expansion", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        layout_refine, "plan_widen_page_expansion", lambda *_a, **_k: None
    )
    moved = (20.0, 300.0, 200.0, 350.0)
    monkeypatch.setattr(
        layout_refine, "plan_next_page_float", lambda *_a, **_k: moved
    )
    compiler.compile(record())
    assert "New block one" in _page_text(compiler.store, 2)

    # 第二次编译不再浮动（贴片不缩字），贴片回主页。
    monkeypatch.setattr(
        layout_refine, "plan_next_page_float", lambda *_a, **_k: None
    )
    second: list[list[float]] = []
    monkeypatch.setattr(
        block_compile, "render_request", _stamp_render(second, scale=1.0)
    )
    result = compiler.compile(record())

    assert result["stamp_page"] == 1 and result["previous_stamp_page"] == 2
    assert "New block one" in _page_text(compiler.store, 1)
    assert "New block one" not in _page_text(compiler.store, 2)


def test_draft_layout_is_passed_to_render_request(tmp_path, monkeypatch):
    """草稿的排版/样式覆盖（bold 等）要透传给 render_request。"""
    from pathlib import Path

    from babeldoc_tools.serve import block_compile

    compiler, _rows = _multi_page_store(tmp_path, monkeypatch, pages=1)
    compiler.store.database.save_draft(
        "paper",
        {
            "revision": 1,
            "paragraphs": {"P1": {"layout": {"bold": True, "font_scale": 1.2}}},
        },
        expected_revision=0,
    )
    seen: list[dict] = []

    def render(_workdir, _pid, target, box, temporary, _cache, **kwargs):
        seen.append(kwargs)
        path = Path(temporary) / "stamp.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=box[2] - box[0], height=box[3] - box[1])
            page.insert_text((5, 20), target)
            pdf.save(path)
        return (
            SimpleNamespace(ok=True, pdf_path=str(path), font_size=11, scale=1.0),
            False,
        )

    monkeypatch.setattr(block_compile, "render_request", render)
    job = record()
    job.revision = 1
    compiler.compile_block_patch(job)

    assert seen and seen[0].get("layout") == {"bold": True, "font_scale": 1.2}


def test_draft_font_family_is_passed_to_render_request(tmp_path, monkeypatch):
    """草稿的 `layout.font_family` 跟其它排版覆盖一样透传给 render_request。"""
    from pathlib import Path

    from babeldoc_tools.serve import block_compile

    compiler, _rows = _multi_page_store(tmp_path, monkeypatch, pages=1)
    layout = {"font_family": "lxgw-wenkai", "serif": True}
    compiler.store.database.save_draft(
        "paper",
        {"revision": 1, "paragraphs": {"P1": {"layout": layout}}},
        expected_revision=0,
    )
    seen: list[dict] = []

    def render(_workdir, _pid, target, box, temporary, _cache, **kwargs):
        seen.append(kwargs)
        path = Path(temporary) / "stamp.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=box[2] - box[0], height=box[3] - box[1])
            page.insert_text((5, 20), target)
            pdf.save(path)
        return (
            SimpleNamespace(ok=True, pdf_path=str(path), font_size=11, scale=1.0),
            False,
        )

    monkeypatch.setattr(block_compile, "render_request", render)
    job = record()
    job.revision = 1
    compiler.compile_block_patch(job)

    assert seen and seen[0].get("layout") == layout


@pytest.mark.parametrize("family_serif", [True, False])
def test_apply_font_family_writes_meta_and_latin_serif(family_serif):
    """`render_request` 的 meta 写入：族 id 进 meta，拉丁 serif 跟随该族。

    `render_request` 本体要跑真 XeLaTeX（拿不到编译环境），但它的元数据部分是一个
    纯函数（`_apply_font_family`），直接单测这一层，并断言 meta 的形状正是
    `overlay._stamp_request` 读的那些键。
    """
    from babeldoc.format.pdf.document_il.backend.latex_bbox import font_families
    from babeldoc_tools.serve import block_compile

    spec = next(
        item for item in font_families.FONT_FAMILIES if item.serif is family_serif
    )

    # 用户没显式给 serif：拉丁字形改跟随字体族。
    meta = {"serif": not family_serif}
    block_compile._apply_font_family(meta, {"font_family": spec.id}, None)
    assert meta == {"serif": family_serif, "font_family": spec.id}

    # 同族：serif 已经是目标值，不多写一次（不动 meta 里无关键）。
    meta = {"serif": family_serif, "font_scale": 0.9}
    block_compile._apply_font_family(meta, {"font_family": spec.id}, None)
    assert meta == {"serif": family_serif, "font_scale": 0.9, "font_family": spec.id}

    # 用户显式给了 serif：以用户为准，只写 font_family。
    meta = {"serif": not family_serif}
    block_compile._apply_font_family(
        meta, {"font_family": spec.id}, not family_serif
    )
    assert meta == {"serif": not family_serif, "font_family": spec.id}


def test_apply_font_family_ignores_unknown_and_missing_ids():
    """未登记的 id / 没给键 / 非字符串 → meta 一行不改（行为与改前一致）。"""
    from babeldoc_tools.serve import block_compile

    for layout in ({"font_family": "nope"}, {}, None, {"font_family": 3}):
        meta = {"serif": False, "font_size": 10.0}
        block_compile._apply_font_family(meta, layout, None)
        assert meta == {"serif": False, "font_size": 10.0}, layout

    # 显式 serif 覆盖时只写 font_family，不动调用方已经写好的 serif。
    meta = {"serif": True, "font_size": 10.0}
    block_compile._apply_font_family(meta, {"font_family": "lxgw-wenkai"}, True)
    assert meta == {"serif": True, "font_size": 10.0, "font_family": "lxgw-wenkai"}


def _patch(compiler, *, page):
    with compiler.store.database._lock:
        row = compiler.store.database.connection.execute(
            "SELECT payload FROM local_pages WHERE document_id='paper' AND page=?",
            (page,),
        ).fetchone()
    assert row is not None
    return json.loads(row[0])["patches"]


# --------------------------------------------------------------------------- #
# 批量块编译（shift 多选）：同页串行、每页只合成一次、单块失败不拖垮整批
# --------------------------------------------------------------------------- #
def _batch_record(pids):
    job = record()
    job.effective_scope = "blocks"
    job.paragraph_ids = list(pids)
    return job


def test_store_database_lazy_init_is_thread_safe(tmp_path):
    """并发首次访问共享库只建一条连接、不报 database is locked。"""
    import threading

    store = DocumentStore.for_root(tmp_path)
    seen, errors = [], []
    barrier = threading.Barrier(8)

    def touch():
        try:
            barrier.wait()
            seen.append(store.database)
        except Exception as exc:  # noqa: BLE001 - 测试要收集失败
            errors.append(exc)

    threads = [threading.Thread(target=touch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len({id(item) for item in seen}) == 1


def test_batch_compile_composes_each_page_once(local, monkeypatch):
    compiler, calls, _rows = local
    compose_calls: list[int] = []
    original = BlockCompiler.compose_page_asset

    def counting(self, record, page_number, **kwargs):
        compose_calls.append(page_number)
        return original(self, record, page_number, **kwargs)

    monkeypatch.setattr(BlockCompiler, "compose_page_asset", counting)

    result = compiler.compile_blocks(_batch_record(["P1", "P2"]))

    assert sorted(calls) == ["P1", "P2"] or set(calls) == {"P1", "P2"}
    # 两块都在第 1 页：只合成一次该页（不是每块一次）。
    assert compose_calls.count(1) == 1
    patches = _patch(compiler, page=1)
    assert set(patches) == {"P1", "P2"}
    assert result["blocks"] == 2 and result["pages"] == [1]


def test_batch_compile_failure_isolates_other_blocks(local, monkeypatch):
    compiler, _calls, _rows = local
    from babeldoc_tools.serve import block_compile

    real = block_compile.render_request

    def flaky(_workdir, pid, target, box, temporary, _cache, **kwargs):
        if pid == "P1":
            raise ToolError("compile_failed", "P1 故意失败")
        return real(_workdir, pid, target, box, temporary, _cache, **kwargs)

    monkeypatch.setattr(block_compile, "render_request", flaky)

    with pytest.raises(ToolError) as error:
        compiler.compile_blocks(_batch_record(["P1", "P2"]))

    assert error.value.code == "compile_failed"
    failures = error.value.extra.get("failures")
    assert failures and failures[0]["block_id"] == "P1"
    # P2 的贴片照常发布。
    assert set(_patch(compiler, page=1)) == {"P2"}


def test_batch_endpoint_lifecycle(local, monkeypatch):
    import time

    from babeldoc_tools.serve.app import create_app
    from fastapi.testclient import TestClient

    compiler, calls, rows = local
    monkeypatch.setattr(BlockCompiler, "_rows", lambda _self, _did: rows)
    with TestClient(create_app(compiler.store)) as client:
        response = client.post(
            "/api/v1/documents/paper/blocks/compile",
            json={"base_revision": 0, "block_ids": ["P2", "P1", "P2"]},
        )
        assert response.status_code == 202, response.text
        jid = response.json()["job_id"]
        for _ in range(200):
            job = client.get(f"/api/v1/jobs/{jid}").json()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        assert job["status"] == "succeeded", job
        assert job["effective_scope"] == "blocks"
        # 去重保序：P2、P1 各编一次。
        assert sorted(calls) == ["P1", "P2"]
        assert job["paragraph_ids"] == ["P2", "P1"]
        assert '"blocks": 2' in (job["envelope"] or "")

        # revision 过期 → 409。
        bad = client.post(
            "/api/v1/documents/paper/blocks/compile",
            json={"base_revision": 99, "block_ids": ["P1"]},
        )
        assert bad.status_code == 409
        # 空/未知块 → 4xx，不建 job。
        unknown = client.post(
            "/api/v1/documents/paper/blocks/compile",
            json={"base_revision": 0, "block_ids": ["NOPE"]},
        )
        assert unknown.status_code in (404, 422)


@pytest.mark.parametrize(
    "box", [[0, 0, float("nan"), 10], [0, 0, 401, 10], [1, 1, 0, 0]]
)
def test_invalid_bbox(box):
    with pytest.raises(ToolError) as error:
        valid_box(box, 400, 400)
    assert error.value.code == "bbox_invalid"


def test_export_only_compiles_dirty_block_and_reuses_clean_patch(local):
    compiler, calls, _ = local
    database = compiler.store.database
    database.save_draft(
        "paper",
        {"revision": 1, "paragraphs": {"P1": {"target": "Edited one"}}},
        expected_revision=0,
    )
    job = record()
    job.revision = 1
    result = compiler.export(job)
    assert calls == ["P1"]
    assert result["compiled_blocks"] == 1
    from babeldoc_tools.serve.asset_store import AssetStore

    assets = AssetStore(compiler.store.store_base, database)
    with pymupdf.open(assets.resolve(result["asset"])) as pdf:
        assert "Edited one" in pdf[0].get_text()
        assert "Untouched block two" in pdf[0].get_text()
    again = compiler.export(job)
    assert again["compiled_blocks"] == 0
    assert calls == ["P1"]


def test_failed_dirty_export_keeps_previous_revision(local, monkeypatch):
    compiler, _, _ = local
    database = compiler.store.database
    job = record()
    compiler.export(job)
    previous = tuple(
        database.connection.execute(
            "SELECT asset_sha256,revision FROM exports"
        ).fetchone()
    )
    database.save_draft(
        "paper",
        {"revision": 1, "paragraphs": {"P1": {"target": "Edited"}}},
        expected_revision=0,
    )
    job.revision = 1

    def fail(*_args, **_kwargs):
        raise ToolError("compile_failed", "stamp failed")

    monkeypatch.setattr("babeldoc_tools.serve.block_compile.render_request", fail)
    with pytest.raises(ToolError):
        compiler.export(job)
    assert (
        tuple(
            database.connection.execute(
                "SELECT asset_sha256,revision FROM exports"
            ).fetchone()
        )
        == previous
    )
    assert (
        json.loads(
            database.connection.execute("SELECT payload FROM drafts").fetchone()[0]
        )["revision"]
        == 1
    )
