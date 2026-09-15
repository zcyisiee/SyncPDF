"""Link audit tests.

U0 adds a strict xfail for the unfinished public audit tool test. U4 is
expected to rewrite this test and remove the marker; XPASS(strict) intentionally
fails so the marker cannot be forgotten after implementation.
"""

from pathlib import Path

import pymupdf
import pytest
from babeldoc.tools.agent.link_audit import audit_links
from babeldoc.tools.agent.link_audit import inventory


def _pdf(path, labels=("3", "13")):
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    page = doc[0]
    for i, text in enumerate(labels):
        point = pymupdf.Point(50, 50 + i * 30)
        page.insert_text(point, text)
        rect = pymupdf.Rect(49, point.y - 13, 70, point.y + 3)
        page.insert_link(
            {
                "kind": pymupdf.LINK_GOTO,
                "from": rect,
                "page": 1,
                "to": pymupdf.Point(40, 70 + i * 20),
            }
        )
    doc.save(path)
    doc.close()


def test_audit_checks_exact_label_not_just_any_digit(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _pdf(source)
    _pdf(output, ("13", "3"))
    report = audit_links(source, output, report_path=tmp_path / "report.json")
    assert report["targets_preserved"]
    assert report["summary"]["wrong_label"] == 2
    assert report["summary"]["output_invalid"] == 0
    assert Path(report["report"]).is_file()


def test_audit_internal_targets_even_when_uri_set_and_count_unchanged(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _pdf(source)
    with pymupdf.open(source) as doc:
        page = doc[0]
        link = page.get_links()[0]
        link["to"] = pymupdf.Point(40, 300)
        page.update_link(link)
        doc.save(output)
    report = audit_links(source, output)
    assert not report["targets_preserved"]
    assert report["summary"]["missing"] == 1


def test_audit_finds_undefined_named_dest_omitted_by_get_links(tmp_path):
    source = tmp_path / "source.pdf"
    _pdf(source)
    with pymupdf.open(source) as doc:
        link = doc[0].get_links()[0]
        doc.xref_set_key(link["xref"], "A/D", "(nonexistent-destination)")
        data = doc.tobytes()
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        rows = inventory(doc)
        assert len(rows) == 2
        assert sum(r["target_status"] == "invalid" for r in rows) == 1


def test_repeated_destinations_count_each_source_occurrence(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _pdf(source)
    with pymupdf.open(source) as doc:
        doc[0].delete_link(doc[0].get_links()[1])
        doc.save(output)
    report = audit_links(source, output)
    assert report["summary"]["preserved"] == 1
    assert report["summary"]["missing"] == 1


def test_external_file_action_not_called_invalid(tmp_path):
    source = tmp_path / "source.pdf"
    _pdf(source)
    with pymupdf.open(source) as doc:
        doc[0].insert_link(
            {
                "kind": pymupdf.LINK_LAUNCH,
                "from": pymupdf.Rect(10, 10, 30, 30),
                "file": "www.example.org",
            }
        )
        data = doc.tobytes()
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        row = next(r for r in inventory(doc) if r["target"]["type"] == "file")
        assert row["target_status"] == "external_unchecked"


@pytest.mark.xfail(
    strict=True,
    reason="audit_links 工具化属于 U4，届时重写本测试",
)
def test_public_audit_tool(tmp_path):
    from babeldoc_tools.registry import dispatch

    source = tmp_path / "source.pdf"
    _pdf(source)
    result = dispatch(
        "audit_links",
        {"source_pdf": str(source), "pdf": str(source), "workdir": str(tmp_path)},
    )
    assert result["ok"]
    assert result["data"]["targets_preserved"]
    assert result["data"]["anchors_verified"]


def test_audit_detects_swapped_roles_with_identical_numbers(tmp_path):
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    page = doc[0]
    page.insert_text((30, 70), "MinerU1, Figure 1.", fontsize=12)
    rects = page.search_for("1")
    target = doc[1].xref
    doc.xref_set_key(
        doc.pdf_catalog(),
        "Names",
        f"<< /Dests << /Names [(Hfootnote.1) [{target} 0 R /XYZ 30 700 0] (figure.1) [{target} 0 R /XYZ 30 500 0]] >> >>",
    )
    for rect, name in zip(rects, ("Hfootnote.1", "figure.1"), strict=True):
        page.insert_link({"kind": 4, "from": rect, "nameddest": name})
    doc.save(source)
    doc.close()
    with pymupdf.open(source) as doc:
        for i, link in enumerate(doc[0].get_links()):
            doc.xref_set_key(
                link["xref"], "BabelDOCLink", pymupdf.get_pdf_str(f"0:{i}")
            )
            rect = rects[1 - i] * ~doc[0].transformation_matrix
            doc.xref_set_key(
                link["xref"], "Rect", "[" + " ".join(str(x) for x in rect) + "]"
            )
        doc.save(output)
    report = audit_links(source, output)
    assert report["targets_preserved"]
    assert report["summary"]["wrong_role"] == 2
    assert not report["anchors_verified"]
