"""``babeldoc_tools/serve/paper_meta.py``：标题 / 一作抽取与 ``papers`` 表回填。

两条抽取路径都要覆盖：

- 源 PDF metadata（有 title/author 的真 PDF）；
- provider IR 首页的 title 块 + 紧随其后的 text 块（metadata 空时回退），
  含 ``<sup>a</sup>`` 单位上标被剥掉的真实形状。

以及降级口径：抽不到就 ``None``（不拿文件名冒充标题）；``set_paper_meta`` 只填空，
重复调用幂等。真实文档（``~/.sp``）是**只读**共享库：这里只断言"能读"，不写它。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from babeldoc_tools.serve.paper_meta import PaperMeta
from babeldoc_tools.serve.paper_meta import extract_paper_meta
from babeldoc_tools.serve.paper_meta import first_author_of
from babeldoc_tools.serve.paper_meta import provider_ir_meta
from babeldoc_tools.serve.paper_meta import resolve_paper_meta
from babeldoc_tools.serve.paper_meta import source_pdf_meta
from babeldoc_tools.serve.workdir import WorkdirReader

pymupdf = pytest.importorskip("pymupdf", reason="抽 metadata 需要 pymupdf")

SHARED_LIBRARY = Path.home() / ".sp"

#: 共享库里三篇真文档 → 期望的 (title, first_author)。
#: PDF metadata 是首选出处，三条都已核实（见任务书）。
REAL_DOCUMENTS = (
    (
        "up-vns-20260921-022426",
        "Variable Neighborhood Search: The power of change and simplicity",
        "Jack Brimberg",
    ),
    (
        "up-trc-20260919-091110",
        "Truck–drone hybrid routing problem with time-dependent road travel time",
        "Yong Wang",
    ),
    (
        "up-2602-02908v2-20260920-155426",
        "A Random Matrix Theory Perspective on the Consistency of Diffusion Models",
        "Binxu Wang",
    ),
)


def _make_pdf(path: Path, title: str, author: str) -> None:
    """造一个带 metadata 的最小 PDF（真 pymupdf 写盘，不 mock）。"""
    document = pymupdf.open()
    document.new_page()
    document.set_metadata({"title": title, "author": author})
    document.save(path)
    document.close()


# --------------------------------------------------------------------------- #
# 一作提取
# --------------------------------------------------------------------------- #
def test_first_author_splits_on_semicolon_first():
    """metadata 的多作者串用 ``;`` 分隔：一作就是第一段。"""
    assert first_author_of("Binxu Wang; Jacob Zavatone-Veth; Cengiz Pehlevan") == "Binxu Wang"


def test_first_author_splits_on_comma_when_no_semicolon():
    """IR 作者行没有 ``;``：按 ``,`` 取第一段。"""
    assert first_author_of("Jack Brimberg, Said Salhi, Raca Todosijević") == "Jack Brimberg"


def test_first_author_supports_and_separator():
    assert first_author_of("John Smith and Jane Doe") == "John Smith"


def test_first_author_strips_sup_affiliation_markers():
    """``<sup>`` 标签与它包裹的单位编号都要剥掉（那是单位，不是姓名）。"""
    raw = (
        "Jack Brimberg <sup>a</sup>, Said Salhi <sup>b</sup>, "
        "Raca Todosijević <sup>c,d,∗</sup>, Dragan Urošević <sup>e</sup>"
    )
    assert first_author_of(raw) == "Jack Brimberg"


def test_first_author_strips_bare_affiliation_tail():
    """没被 ``<sup>`` 包住的单位上标（``John Smith a``）也要剥掉。"""
    assert first_author_of("John Smith a, Jane Doe b") == "John Smith"
    assert first_author_of("John Smith 12, Jane Doe 13") == "John Smith"


def test_first_author_missing_returns_none():
    """没有作者 → ``None``（不是空串：空串会让前端渲染出一个空行）。"""
    assert first_author_of(None) is None
    assert first_author_of("") is None
    assert first_author_of("   ") is None
    assert first_author_of("<sup>a</sup>") is None


# --------------------------------------------------------------------------- #
# 源 PDF metadata
# --------------------------------------------------------------------------- #
def test_source_pdf_meta_reads_metadata(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    _make_pdf(pdf, "Attention Is All You Need", "Ashish Vaswani; Noam Shazeer")
    assert source_pdf_meta(pdf) == ("Attention Is All You Need", "Ashish Vaswani; Noam Shazeer")


def test_source_pdf_meta_ignores_file_name_as_title(tmp_path: Path):
    """导出器常把文件名塞进 metadata.title：那不是标题，不采用（留给文件名回退）。"""
    pdf = tmp_path / "paper-final.pdf"
    _make_pdf(pdf, "paper-final", "")
    title, author = source_pdf_meta(pdf)
    assert title is None
    assert author is None


def test_source_pdf_meta_on_broken_file_returns_nones(tmp_path: Path):
    """坏 PDF 只让标题缺失，不能抛异常（否则整个列表端点 500）。"""
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    assert source_pdf_meta(broken) == (None, None)


# --------------------------------------------------------------------------- #
# provider IR 回退
# --------------------------------------------------------------------------- #
def _write_provider_ir(workdir: Path, blocks: list[dict], *, legacy: bool = False) -> Path:
    base = "mineru" if legacy else "provider"
    path = workdir / "agent" / "source" / base / "provider_ir.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"page_count": 1, "pages": [{"page_index": 0, "blocks": blocks}]}),
        encoding="utf-8",
    )
    return path


def _title_block(text: str) -> dict:
    return {
        "block_id": "p0-b0",
        "type": "title",
        "bbox": [35, 164, 431, 180],
        "lines": [{"spans": [{"kind": "text", "content": text}]}],
    }


def _text_block(text: str) -> dict:
    return {
        "block_id": "p0-b3",
        "type": "text",
        "bbox": [35, 184, 358, 197],
        "lines": [{"spans": [{"kind": "text", "content": text}]}],
    }


@pytest.mark.parametrize("legacy", [False, True])
def test_provider_ir_meta_reads_title_and_author_line(tmp_path: Path, legacy: bool):
    """两条 IR 路径（新 ``source/provider`` / 旧 ``source/mineru``）都要能读。"""
    _write_provider_ir(
        tmp_path,
        [
            _text_block("journal header"),
            _title_block("Variable Neighborhood Search: The power of change and simplicity"),
            {"block_id": "p0-b1", "type": "image", "lines": []},
            _text_block("Jack Brimberg <sup>a</sup>, Said Salhi <sup>b</sup>"),
            _text_block("Abstract: this is the abstract."),
        ],
        legacy=legacy,
    )
    title, authors = provider_ir_meta(WorkdirReader(tmp_path))
    assert title == "Variable Neighborhood Search: The power of change and simplicity"
    # 作者行里的 <sup> 单位标记剥掉，多作者保留原样（分隔符统一成 ", "）
    assert authors == "Jack Brimberg, Said Salhi"
    assert first_author_of(authors) == "Jack Brimberg"


def test_provider_ir_meta_without_title_block_returns_nones(tmp_path: Path):
    """没有 ``type == "title"`` 的块 → 两个字段都 ``None``（不猜哪块是标题）。"""
    _write_provider_ir(tmp_path, [_text_block("First paragraph.")])
    assert provider_ir_meta(WorkdirReader(tmp_path)) == (None, None)


def test_provider_ir_meta_skips_long_text_after_title(tmp_path: Path):
    """title 块之后第一个 ``text`` 块是摘要（不是作者行）→ 不认它当作者。"""
    long_text = "We study the problem. " * 20
    _write_provider_ir(
        tmp_path, [_title_block("A Title"), _text_block(long_text)]
    )
    title, authors = provider_ir_meta(WorkdirReader(tmp_path))
    assert title == "A Title"
    assert authors is None


def test_provider_ir_meta_without_artifact_returns_nones(tmp_path: Path):
    assert provider_ir_meta(WorkdirReader(tmp_path)) == (None, None)


# --------------------------------------------------------------------------- #
# extract / resolve：回退链与只填空
# --------------------------------------------------------------------------- #
def test_extract_prefers_pdf_metadata_over_ir(tmp_path: Path):
    """metadata 齐全时用 metadata（作者不是 IR 那份）。"""
    _make_pdf(tmp_path / "source.pdf", "From Metadata", "Metadata Author")
    _write_provider_ir(tmp_path, [_title_block("From IR"), _text_block("IR Author")])
    meta = extract_paper_meta(WorkdirReader(tmp_path))
    assert meta == PaperMeta(title="From Metadata", authors="Metadata Author")
    assert meta.first_author == "Metadata Author"


def test_extract_falls_back_field_by_field(tmp_path: Path):
    """metadata 有标题无作者 → 标题用 metadata、作者回退 IR（两个字段独立回退）。"""
    _make_pdf(tmp_path / "source.pdf", "From Metadata", "")
    _write_provider_ir(tmp_path, [_title_block("From IR"), _text_block("IR Author")])
    meta = extract_paper_meta(WorkdirReader(tmp_path))
    assert meta.title == "From Metadata"
    assert meta.first_author == "IR Author"


def test_extract_without_products_returns_empty_meta(tmp_path: Path):
    """没有任何产物 → 全 ``None``（视图层再回退文件名，不在这里造假）。"""
    assert extract_paper_meta(WorkdirReader(tmp_path)) == PaperMeta()


def test_resolve_writes_then_reads_from_database(tmp_path: Path):
    """首次读产物并落库；第二次（产物已被删）仍从库里拿到同样的值。"""
    from babeldoc_tools.serve.database import MetadataDB

    _make_pdf(tmp_path / "source.pdf", "A Title", "An Author")
    database = MetadataDB(tmp_path / "state")
    reader = WorkdirReader(tmp_path)
    first = resolve_paper_meta(database, reader, "did-1")
    assert first == PaperMeta(title="A Title", authors="An Author")
    assert database.paper_meta("did-1") == ("A Title", "An Author")

    (tmp_path / "source.pdf").unlink()
    second = resolve_paper_meta(database, WorkdirReader(tmp_path), "did-1")
    assert second == first
    database.close()


def test_set_paper_meta_never_clears_existing_values(tmp_path: Path):
    """``None`` 只是「这次没读到」，不能把已有的标题/作者抹掉（幂等）。

    非 ``None`` 的新值会覆写（同一份源 PDF 抽出来都一样，重复运行结果一致）；
    只有 ``None`` 被 ``COALESCE`` 挡住。
    """
    from babeldoc_tools.serve.database import MetadataDB

    database = MetadataDB(tmp_path / "state")
    database.set_paper_meta("did-1", "A Title", "An Author")
    database.set_paper_meta("did-1", None, None)
    assert database.paper_meta("did-1") == ("A Title", "An Author")
    # 只补缺失的作者：两个字段分别判定，不整行覆盖
    database.set_paper_meta("did-1", None, "Second Author")
    assert database.paper_meta("did-1") == ("A Title", "Second Author")
    # 重复写同一个值：结果不变（幂等）
    database.set_paper_meta("did-1", "A Title", "Second Author")
    assert database.paper_meta("did-1") == ("A Title", "Second Author")
    # 没有 papers 行的 did：只写作者也要能建行
    database.set_paper_meta("did-2", None, "Only Author")
    assert database.paper_meta("did-2") == (None, "Only Author")
    # 不存在的 did 只是「没有元数据」，不报错
    assert database.paper_meta("missing") == (None, None)
    database.close()


# --------------------------------------------------------------------------- #
# 真实文档（只读共享库）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("did", "title", "first_author"), REAL_DOCUMENTS)
def test_real_shared_documents_extract_real_metadata(did: str, title: str, first_author: str):
    """三篇真实文档：标题是真标题、一作是真姓名（共享库只读，不写它）。"""
    workdir = SHARED_LIBRARY / did
    if not (workdir / "source.pdf").is_file():
        pytest.skip(f"共享库里没有 {did}（{workdir}）")
    meta = extract_paper_meta(WorkdirReader(workdir))
    assert meta.title == title
    assert meta.first_author == first_author
