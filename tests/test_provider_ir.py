"""MinerU provider IR（babeldoc.docvision.provider_ir）单元测试。

覆盖：
1. 缓存回放：6 份真实 layout.json → block/line/span 计数与 JSON 原始计数一致；
   reading_order 长度 = 顶层 para block 数；to_json → from_json round-trip 相等。
2. 合成数据：嵌套 children、discarded block、inline_equation span、未知类型聚合、
   id 唯一性、缺 index 的阅读顺序回退。
3. 同步断言：provider_ir.KNOWN_BLOCK_TYPES 覆盖 mineru_doclayout 映射函数的显式分支。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from babeldoc.docvision.provider_ir import KNOWN_BLOCK_TYPES
from babeldoc.docvision.provider_ir import KNOWN_SPAN_KINDS
from babeldoc.docvision.provider_ir import ProviderDocument

CACHE_DIR = Path.home() / ".cache" / "babeldoc" / "mineru-layout.v1"

# 离线回放用的假 token（MinerU model 构造必需，但回放路径不会发请求）。
_DUMMY_TOKEN = "dummy-token"  # noqa: S105

# 仓库 5 份样本 + 1 份重复样本的内容哈希（真实缓存文件名）。
SAMPLE_CACHE_KEYS = (
    "ba68e2e40408125ae6d2f63a9a241b61c73910691c74ec1a2a7023c851eac08d",
    "519f7090f41e66e81d0e5493ae79aba536755297747b2548cc8de637c04bd01f",
    "ebdca8e7e579450770eafdf98243f197b8e60fc115fe07ab15870a31812a326b",
    "f15a0e00eed725df8ed53c5da37250bb2a09ac1dd4c7710013d6b0b8d5ac062e",
    "4a4192fb88b8b09f0d3da2ac9ef71f07502fdaf344a83062ff82f1bff74e1284",
    "773068fa427a7c594c0bd97361aea23eb5bc68dc513548604c628029c3d0c4e9",
)

_CHILD_KEYS = ("children", "blocks")


def _raw_counts(layout_json: dict) -> dict[str, int]:
    """按 JSON 原始结构递归计数（含 para_blocks + discarded_blocks + 嵌套 children）。"""
    blocks = lines = spans = 0

    def walk(block: dict) -> None:
        nonlocal blocks, lines, spans
        blocks += 1
        for line in block.get("lines") or []:
            lines += 1
            spans += len(line.get("spans") or [])
        for key in _CHILD_KEYS:
            for child in block.get(key) or []:
                if isinstance(child, dict):
                    walk(child)

    for page in layout_json.get("pdf_info") or []:
        if not isinstance(page, dict):
            continue
        for key in ("para_blocks", "discarded_blocks"):
            for block in page.get(key) or []:
                if isinstance(block, dict):
                    walk(block)
    return {"blocks": blocks, "lines": lines, "spans": spans}


def _ir_counts(document: ProviderDocument) -> dict[str, int]:
    blocks = lines = spans = 0
    for page in document.pages:
        for block in page.iter_blocks(recursive=True):
            blocks += 1
            lines += len(block.lines)
            for line in block.lines:
                spans += len(line.spans)
    return {"blocks": blocks, "lines": lines, "spans": spans}


def _load_cache(key: str) -> dict:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        pytest.skip(f"MinerU layout cache missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("cache_key", SAMPLE_CACHE_KEYS)
def test_cached_layout_json_round_trips(cache_key: str):
    layout_json = _load_cache(cache_key)

    document = ProviderDocument.from_layout_json(layout_json)

    assert document.page_count >= 1
    assert _ir_counts(document) == _raw_counts(layout_json)
    assert document.backend == layout_json.get("_backend")
    assert document.version_name == layout_json.get("_version_name")

    # reading_order 只含顶层 para_blocks。
    raw_top_para = sum(
        len(page.get("para_blocks") or []) for page in layout_json["pdf_info"]
    )
    ir_top_para = sum(len(page.reading_order) for page in document.pages)
    assert ir_top_para == raw_top_para

    # 序列化往返。
    restored = ProviderDocument.from_json(document.to_json())
    assert restored.to_dict() == document.to_dict()


def test_deepseek_sample_inline_equation_count():
    """DeepSeek 样本顶层 inline_equation span 数为 103（板块 2 的验收基准）。"""
    layout_json = _load_cache(SAMPLE_CACHE_KEYS[0])
    document = ProviderDocument.from_layout_json(layout_json)

    inline = sum(
        1
        for page in document.pages
        for block in page.iter_blocks()
        for line in block.lines
        for span in line.spans
        if span.kind == "inline_equation"
    )
    assert document.page_count == 51
    assert inline == 103


def test_unknown_types_empty_for_cached_samples():
    """6 份真实样本的 block/span 类型都应在已知集合内（无假 unknown）。"""
    for cache_key in SAMPLE_CACHE_KEYS:
        layout_json = _load_cache(cache_key)
        document = ProviderDocument.from_layout_json(layout_json)
        assert document.unknown_types == [], (cache_key, document.unknown_types)


def test_all_known_types_are_consistent_with_layout_label_mapping(caplog):
    """KNOWN_BLOCK_TYPES 与 mineru_doclayout 映射函数显式分支保持同步。

    对集合内每个类型调用映射函数都不应打印 "Unknown MinerU block type" 警告；
    反之，映射函数没有显式分支的类型（如 "chart_caption" 之外的自造类型）
    不应混进集合。
    """
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    model = MinerUDocLayoutModel(api_token=_DUMMY_TOKEN)

    for block_type in sorted(KNOWN_BLOCK_TYPES):
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            model._map_mineru_block_to_layout_label(
                {"type": block_type, "lines": [{"bbox": [0, 0, 1, 1], "spans": []}]}
            )
        assert "Unknown MinerU block type" not in caplog.text, block_type

    # span kind 必须覆盖 MinerU 实际产出的 6 种（含图/表/图表正文 span）。
    assert {
        "text",
        "inline_equation",
        "interline_equation",
        "image",
        "table",
        "chart",
    }.issubset(KNOWN_SPAN_KINDS)


def _mini_layout() -> dict:
    """合成 layout.json：嵌套 children、discarded、inline_equation、未知类型。"""
    return {
        "_version_name": "3.4.4",
        "_backend": "hybrid",
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [595, 841],
                "para_blocks": [
                    {
                        "type": "title",
                        "bbox": [10, 10, 200, 30],
                        "index": 1,
                        "level": 1,
                        "angle": 0,
                        "lines": [
                            {
                                "bbox": [10, 10, 200, 30],
                                "spans": [
                                    {
                                        "bbox": [10, 10, 200, 30],
                                        "type": "text",
                                        "content": "Hello",
                                        "score": 0.99,
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        # 容器块：子块走 "blocks" 键（MinerU 旧版键名）
                        "type": "table",
                        "bbox": [10, 40, 300, 200],
                        "index": 5,
                        "blocks": [
                            {
                                "type": "table_body",
                                "bbox": [10, 40, 300, 150],
                                "index": 6,
                                "angle": 0,
                                "lines": [],
                            },
                            {
                                "type": "mystery_block",
                                "bbox": [10, 160, 300, 200],
                                "index": 7,
                                "lines": [],
                            },
                        ],
                    },
                    {
                        # 缺 index：阅读顺序应排到有 index 的之后
                        "type": "text",
                        "bbox": [10, 210, 300, 260],
                        "angle": 0,
                        "lines": [
                            {
                                "bbox": [10, 210, 300, 230],
                                "spans": [
                                    {
                                        "bbox": [10, 210, 300, 230],
                                        "type": "text",
                                        "content": "x = ",
                                        "score": 1.0,
                                    },
                                    {
                                        "bbox": [70, 210, 100, 230],
                                        "type": "inline_equation",
                                        "content": "a + b",
                                        "score": 0.8,
                                    },
                                    {
                                        "bbox": [100, 210, 300, 230],
                                        "type": "mystery_span",
                                        "content": "?",
                                    },
                                ],
                            }
                        ],
                    },
                ],
                "discarded_blocks": [
                    {
                        "type": "page_number",
                        "bbox": [280, 800, 320, 820],
                        "index": 9,
                        "lines": [],
                    }
                ],
            }
        ],
    }


def test_synthetic_nested_tree_and_unknown_types():
    document = ProviderDocument.from_layout_json(_mini_layout())

    page = document.pages[0]
    # page_count = max(page_idx)+1
    assert document.page_count == 1

    # 阅读顺序只含 para_blocks，且缺 index 的排在最后。
    assert len(page.reading_order) == 3
    ordered_types = [page.block_by_id(block_id).type for block_id in page.reading_order]
    assert ordered_types == ["title", "table", "text"]

    # 顶层 block 列表带上 discarded（source 标记不同）。
    sources = {block.source for block in page.blocks}
    assert sources == {"para_blocks", "discarded_blocks"}

    # 嵌套：table → table_body / mystery_block，parent_block_id 链正确。
    table = next(b for b in page.blocks if b.type == "table")
    assert table.parent_block_id is None
    child_types = [child.type for child in table.children]
    assert child_types == ["table_body", "mystery_block"]
    assert all(child.parent_block_id == table.block_id for child in table.children)

    # 递归计数：title(1) + table(1) + 2 children + text(1) + page_number(1) = 6
    assert len(list(page.iter_blocks(recursive=True))) == 6
    # 顶层计数：title + table + text + page_number = 4
    assert len(list(page.iter_blocks())) == 4

    # span 属性完整保留。
    text_block = next(b for b in page.blocks if b.type == "text")
    spans = text_block.lines[0].spans
    assert [span.kind for span in spans] == [
        "text",
        "inline_equation",
        "mystery_span",
    ]
    assert spans[1].content == "a + b"
    assert spans[1].score == 0.8
    assert spans[1].page_index == 0

    # 未知类型聚合：mystery_block（block）+ mystery_span（span），按页聚合计数。
    unknown = {(item["type"], item["where"]): item for item in document.unknown_types}
    assert unknown[("mystery_block", "block")]["count"] == 1
    assert unknown[("mystery_span", "span")]["count"] == 1
    assert all(item["page_index"] == 0 for item in document.unknown_types)


def test_synthetic_block_ids_are_unique_and_nested_paths():
    document = ProviderDocument.from_layout_json(_mini_layout())

    page = document.pages[0]
    block_ids = [block.block_id for block in page.iter_blocks(recursive=True)]
    assert len(block_ids) == len(set(block_ids)) == 6
    for block in page.iter_blocks(recursive=True):
        assert block.block_id.startswith("p0-b")
        for line in block.lines:
            assert line.line_id.startswith(block.block_id + "-l")
            for span in line.spans:
                assert span.span_id.startswith(line.line_id + "-s")


def test_invalid_layout_json_raises():
    with pytest.raises(ValueError):
        ProviderDocument.from_layout_json({})


def test_page_lookup_and_missing_pages():
    """节选 layout（page_idx 跳号）时 page_count 取 max+1，缺失页返回 None。"""
    layout_json = _mini_layout()
    layout_json["pdf_info"].append(
        {
            "page_idx": 3,
            "page_size": [595, 841],
            "para_blocks": [],
            "discarded_blocks": [],
        }
    )
    document = ProviderDocument.from_layout_json(layout_json)

    assert document.page_count == 4
    assert document.page(0) is not None
    assert document.page(1) is None
    assert document.page(3) is not None
    assert document.page(3).reading_order == []


# --------------------------------------------------------------------------- #
# MinerUDocLayoutModel 接线：三条 layout 来源都要构建并落盘 provider IR
# --------------------------------------------------------------------------- #
def _fixture_layout() -> dict:
    fixture = Path("tests/fixtures/mineru/layout_v275_s41586_excerpt.json")
    return json.loads(fixture.read_text(encoding="utf-8"))


def _model_with_ir_dir(tmp_path):
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    model = MinerUDocLayoutModel(api_token=_DUMMY_TOKEN)
    ir_dir = tmp_path / "agent"
    translate_config = SimpleNamespace(
        input_file="dummy.pdf",
        raise_if_cancelled=lambda: None,
        provider_ir_dir=ir_dir,
    )
    return model, translate_config, ir_dir / "source" / "mineru" / "provider_ir.json"


def test_handle_document_replay_builds_and_persists_provider_ir(tmp_path, monkeypatch):
    """BABELDOC_MINERU_LAYOUT_JSON 回放路径：构建 provider IR 并落盘。"""
    layout_json = _fixture_layout()
    replay_file = tmp_path / "layout.json"
    replay_file.write_text(json.dumps(layout_json), encoding="utf-8")
    monkeypatch.setenv("BABELDOC_MINERU_LAYOUT_JSON", str(replay_file))

    model, translate_config, ir_path = _model_with_ir_dir(tmp_path)
    pages = [SimpleNamespace(page_number=i) for i in (0, 1, 2, 6)]

    outputs = list(
        model.handle_document(
            pages,
            mupdf_doc=SimpleNamespace(page_count=4),
            translate_config=translate_config,
            save_debug_image=None,
        )
    )

    assert len(outputs) == 4
    assert model.provider_document is not None
    assert model.provider_document.page_count == 7
    assert ir_path.exists()
    restored = ProviderDocument.from_json(ir_path.read_text(encoding="utf-8"))
    assert restored.to_dict() == model.provider_document.to_dict()


def test_handle_document_cache_hit_builds_and_persists_provider_ir(
    tmp_path, monkeypatch
):
    """缓存命中路径（api_token + 缓存文件存在）：同样构建并落盘 provider IR。"""
    layout_json = _fixture_layout()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "dummy-hash.json").write_text(
        json.dumps(layout_json), encoding="utf-8"
    )

    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    monkeypatch.setattr(MinerUDocLayoutModel, "LAYOUT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        MinerUDocLayoutModel,
        "_layout_cache_path",
        lambda *_args: cache_dir / "dummy-hash.json",
    )
    monkeypatch.delenv("BABELDOC_MINERU_LAYOUT_JSON", raising=False)

    model, translate_config, ir_path = _model_with_ir_dir(tmp_path)
    # mupdf_doc 只被回放路径使用；缓存路径用 page_count 做页覆盖校验。
    pages = [SimpleNamespace(page_number=i) for i in (0, 1, 2, 6)]

    outputs = list(
        model.handle_document(
            pages,
            mupdf_doc=SimpleNamespace(page_count=7),
            translate_config=translate_config,
            save_debug_image=None,
        )
    )

    assert len(outputs) == 4
    assert model.provider_document is not None
    assert model.provider_document.page_count == 7
    assert ir_path.exists()


def test_handle_document_without_workdir_skips_persist(tmp_path, monkeypatch):
    """无 provider_ir_dir / working_dir 时不落盘，但内存 IR 仍构建。"""
    layout_json = _fixture_layout()
    replay_file = tmp_path / "layout.json"
    replay_file.write_text(json.dumps(layout_json), encoding="utf-8")
    monkeypatch.setenv("BABELDOC_MINERU_LAYOUT_JSON", str(replay_file))

    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    model = MinerUDocLayoutModel(api_token=_DUMMY_TOKEN)
    translate_config = SimpleNamespace(
        input_file="dummy.pdf",
        raise_if_cancelled=lambda: None,
        provider_ir_dir=None,
        working_dir=None,
    )
    pages = [SimpleNamespace(page_number=i) for i in (0, 1, 2, 6)]

    list(
        model.handle_document(
            pages,
            mupdf_doc=SimpleNamespace(page_count=4),
            translate_config=translate_config,
            save_debug_image=None,
        )
    )

    assert model.provider_document is not None
    assert not (tmp_path / "agent" / "source").exists()


def test_handle_document_resets_provider_document_between_calls(tmp_path, monkeypatch):
    """每次 handle_document 重置 provider IR，避免跨调用残留。"""
    layout_json = _fixture_layout()
    replay_file = tmp_path / "layout.json"
    replay_file.write_text(json.dumps(layout_json), encoding="utf-8")
    monkeypatch.setenv("BABELDOC_MINERU_LAYOUT_JSON", str(replay_file))

    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    model = MinerUDocLayoutModel(api_token=_DUMMY_TOKEN)
    translate_config = SimpleNamespace(
        input_file="dummy.pdf",
        raise_if_cancelled=lambda: None,
        provider_ir_dir=tmp_path / "agent",
    )
    full_pages = [SimpleNamespace(page_number=i) for i in (0, 1, 2, 6)]

    list(
        model.handle_document(
            full_pages,
            mupdf_doc=SimpleNamespace(page_count=4),
            translate_config=translate_config,
            save_debug_image=None,
        )
    )
    first = model.provider_document
    assert first is not None and len(first.pages) == 4

    # 只请求一页：回放路径要求 layout_pages == requested_pages，
    # 这里改用 page_count 校验的另一分支会失败，因此显式构造单页 layout。
    single = {
        **layout_json,
        "pdf_info": [p for p in layout_json["pdf_info"] if p["page_idx"] == 0],
    }
    replay_file.write_text(json.dumps(single), encoding="utf-8")
    list(
        model.handle_document(
            [SimpleNamespace(page_number=0)],
            mupdf_doc=SimpleNamespace(page_count=1),
            translate_config=translate_config,
            save_debug_image=None,
        )
    )

    assert model.provider_document is not first
    assert len(model.provider_document.pages) == 1


def test_handle_document_tolerates_broken_layout_json(tmp_path, monkeypatch):
    """provider IR 构建失败不应阻断 YoloResult 解析路径。"""
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"not_pdf_info": []}), encoding="utf-8")
    monkeypatch.setenv("BABELDOC_MINERU_LAYOUT_JSON", str(broken))

    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    model = MinerUDocLayoutModel(api_token=_DUMMY_TOKEN)
    translate_config = SimpleNamespace(
        input_file="dummy.pdf",
        raise_if_cancelled=lambda: None,
        provider_ir_dir=tmp_path / "agent",
    )
    pages = [SimpleNamespace(page_number=0)]

    with pytest.raises(RuntimeError):
        # pdf_info 缺失时 YoloResult 路径本身也会报错（既有行为），
        # provider IR 构建失败只 warning、不改写也不吞掉既有错误。
        list(
            model.handle_document(
                pages,
                mupdf_doc=SimpleNamespace(page_count=1),
                translate_config=translate_config,
                save_debug_image=None,
            )
        )
    assert model.provider_document is None


def test_resolve_mineru_json_prefers_explicit_json(tmp_path):
    from babeldoc.tools.agent.markdown_view import _resolve_mineru_json

    explicit = tmp_path / "explicit.json"
    assert _resolve_mineru_json(str(explicit), "deadbeef") == str(explicit)


def test_resolve_mineru_cache_key_missing_raises(tmp_path, monkeypatch):
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel
    from babeldoc.tools.agent.markdown_view import _resolve_mineru_json

    monkeypatch.setattr(MinerUDocLayoutModel, "LAYOUT_CACHE_DIR", tmp_path / "cache")
    with pytest.raises(ValueError, match="缓存未命中"):
        _resolve_mineru_json(None, "deadbeef")


def test_resolve_mineru_cache_key_hit_returns_path(tmp_path, monkeypatch):
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel
    from babeldoc.tools.agent.markdown_view import _resolve_mineru_json

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cached = cache_dir / "abc.json"
    cached.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(MinerUDocLayoutModel, "LAYOUT_CACHE_DIR", cache_dir)

    assert _resolve_mineru_json(None, "abc") == str(cached)
    assert _resolve_mineru_json(None, None) is None


# --------------------------------------------------------------------------- #
# 跨页/跨栏合并修复（repair_merged_cross_page_blocks）
# --------------------------------------------------------------------------- #
def _line(bbox: list[float], content: str = "x") -> dict:
    return {
        "bbox": bbox,
        "spans": [{"bbox": bbox, "type": "text", "content": content}],
    }


def _merged_two_page_layout() -> dict:
    """右栏底块合并了下一页左栏顶的续文，续页留下 lines_deleted 空块。"""
    return {
        "pdf_info": [
            {
                "page_idx": 0,
                "para_blocks": [
                    {
                        "type": "text",
                        "bbox": [314, 598, 560, 711],
                        "lines": [
                            _line([317, 600, 559, 609], "own page"),
                            _line([52, 83, 293, 96], "continuation"),
                        ],
                    }
                ],
            },
            {
                "page_idx": 1,
                "para_blocks": [
                    {
                        "type": "text",
                        "bbox": [50, 83, 296, 173],
                        "lines": [],
                        "lines_deleted": True,
                    }
                ],
            },
        ]
    }


def test_cross_page_merged_lines_move_to_receiver_page():
    layout = _merged_two_page_layout()
    original = json.loads(json.dumps(layout))

    document = ProviderDocument.from_layout_json(layout)

    source = document.page(0).blocks[0]
    receiver = document.page(1).blocks[0]
    assert [line.spans[0].content for line in source.lines] == ["own page"]
    assert [line.spans[0].content for line in receiver.lines] == ["continuation"]
    assert receiver.lines[0].spans[0].page_index == 1
    stats = document.metadata["cross_page_repair"]
    assert stats["moved_lines"] == 1
    assert stats["receiver_pages"] == [1]
    # 输入 dict 不被修改（from_layout_json 在深拷贝上修复）。
    assert layout == original


def test_cross_page_legit_lines_inside_own_block_never_move():
    """前页正常行不会被同位置的续页空块误搬（行在本块 bbox 内即豁免）。"""
    layout = {
        "pdf_info": [
            {
                "page_idx": 0,
                "para_blocks": [
                    {
                        # 正常块：bbox 覆盖全部行。
                        "type": "text",
                        "bbox": [50, 85, 296, 174],
                        "lines": [_line([52, 90, 294, 100], "legit")],
                    }
                ],
            },
            {
                "page_idx": 1,
                "para_blocks": [
                    {
                        # 与前页块几乎同位置的同构空块。
                        "type": "text",
                        "bbox": [50, 83, 296, 173],
                        "lines": [],
                        "lines_deleted": True,
                    }
                ],
            },
        ]
    }
    document = ProviderDocument.from_layout_json(layout)
    assert len(document.page(0).blocks[0].lines) == 1
    assert len(document.page(1).blocks[0].lines) == 0
    assert document.metadata == {}


def test_cross_page_receiver_without_marker_still_receives():
    """旧版回放数据可能缺 lines_deleted 标记：空文本块仍可接收。"""
    layout = _merged_two_page_layout()
    del layout["pdf_info"][1]["para_blocks"][0]["lines_deleted"]
    document = ProviderDocument.from_layout_json(layout)
    receiver = document.page(1).blocks[0]
    assert [line.spans[0].content for line in receiver.lines] == ["continuation"]


def test_cross_page_right_column_source_skips_same_page_left_receiver():
    """右栏源块的续文只能去下一页：同页左栏空块在阅读顺序上位于它之前。

    两页左栏顶的近似 bbox 会互相包含，这是「最近页距」必须叠加阅读顺序
    过滤的原因（真实样本 ccs3764 的 p2/p3 截胡场景）。
    """
    layout = {
        "pdf_info": [
            {
                "page_idx": 0,
                "para_blocks": [
                    {
                        # 左栏正常块 + 左栏顶空块（接收同页更早内容用）。
                        "type": "text",
                        "bbox": [50, 300, 296, 400],
                        "lines": [_line([52, 310, 294, 320], "legit-left")],
                    },
                    {
                        "type": "text",
                        "bbox": [50, 85, 296, 174],
                        "lines": [],
                        "lines_deleted": True,
                    },
                    {
                        # 右栏底块：合并了下一页左栏顶的续文。
                        "type": "text",
                        "bbox": [314, 598, 560, 711],
                        "lines": [_line([52, 90, 294, 100], "overflow")],
                    },
                ],
            },
            {
                "page_idx": 1,
                "para_blocks": [
                    {
                        "type": "text",
                        "bbox": [50, 83, 296, 173],
                        "lines": [],
                        "lines_deleted": True,
                    }
                ],
            },
        ]
    }
    document = ProviderDocument.from_layout_json(layout)
    # 同页左栏空块（阅读顺序在前）不得截胡；续文进下一页。
    assert len(document.page(0).blocks[1].lines) == 0
    assert [
        line.spans[0].content for line in document.page(1).blocks[0].lines
    ] == ["overflow"]


def test_cross_column_same_page_continuation_moves_to_right_column():
    """两栏排版：左栏底块的续文在同页右栏顶（阅读顺序的下一站）。"""
    layout = {
        "pdf_info": [
            {
                "page_idx": 0,
                "para_blocks": [
                    {
                        "type": "text",
                        "bbox": [50, 677, 296, 711],
                        "lines": [
                            _line([52, 680, 294, 690], "own-column"),
                            _line([317, 330, 559, 338], "next-column"),
                        ],
                    },
                    {
                        "type": "text",
                        "bbox": [314, 307, 561, 407],
                        "lines": [],
                        "lines_deleted": True,
                    },
                ],
            }
        ]
    }
    document = ProviderDocument.from_layout_json(layout)
    source = document.page(0).blocks[0]
    receiver = document.page(0).blocks[1]
    assert [line.spans[0].content for line in source.lines] == ["own-column"]
    assert [line.spans[0].content for line in receiver.lines] == ["next-column"]


def test_cross_page_image_block_never_receives_lines():
    """image 等图形型空块合法地没有行，绝不能当接收块。"""
    layout = _merged_two_page_layout()
    layout["pdf_info"][1]["para_blocks"][0]["type"] = "image"
    document = ProviderDocument.from_layout_json(layout)
    # image 块不是接收者：溢出行无处可去，保持原位（宁可不动）。
    assert len(document.page(1).blocks[0].lines) == 0
    assert len(document.page(0).blocks[0].lines) == 2
    assert document.metadata == {}


def test_cached_samples_have_no_residual_misplaced_lines():
    """真实样本修复后：所有行都落在本块 bbox 内（残缺样本自动 skip）。"""
    for cache_key in SAMPLE_CACHE_KEYS:
        layout_json = _load_cache(cache_key)
        document = ProviderDocument.from_layout_json(layout_json)
        for page in document.pages:
            for block in page.iter_blocks(recursive=True):
                if block.bbox is None or not block.lines:
                    continue
                for line in block.lines:
                    if line.bbox is None:
                        continue
                    cx = (line.bbox[0] + line.bbox[2]) / 2
                    cy = (line.bbox[1] + line.bbox[3]) / 2
                    assert (
                        block.bbox[0] - 1.0 <= cx <= block.bbox[2] + 1.0
                        and block.bbox[1] - 1.0 <= cy <= block.bbox[3] + 1.0
                    ), (cache_key, block.block_id, line.line_id)
