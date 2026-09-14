"""Audit serialized PDF link destinations and citation labels independently of remapping.

External actions are inspected, never followed. A preserved URI/file action does not
prove network reachability. Unresolved geometry is not an invalid destination.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections import defaultdict
from pathlib import Path

import pymupdf
from babeldoc.format.pdf.document_il.backend.link_text import anchor_variants
from babeldoc.format.pdf.document_il.backend.link_text import covered_text
from babeldoc.format.pdf.document_il.backend.link_text import normalize_anchor
from babeldoc.format.pdf.document_il.backend.link_text import rect_context
from babeldoc.format.pdf.document_il.backend.link_text import reference_role
from babeldoc.format.pdf.document_il.backend.link_text import role_matches


def _point(value):
    if hasattr(value, "x"):
        return [float(value.x), float(value.y)]
    if isinstance(value, tuple | list) and len(value) == 2:
        return list(value)
    return value


def _target(doc, link):
    from babeldoc.format.pdf.document_il.backend.link_remap import normalize_link_action

    link = normalize_link_action(doc, link)
    kind = link.get("kind")
    # MuPDF may report /Launch with a Filespec dictionary as LINK_GOTOR.
    # Audit the actual serialized action, so this is never mistaken for GoToR.
    if (
        link.get("xref", 0) > 0
        and doc.xref_get_key(link["xref"], "A/S")[1] == "/Launch"
    ):
        return {"type": "file", "file": link.get("file")}, "external_unchecked"
    if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED):
        page = link.get("page")
        if isinstance(page, int) and 0 <= page < len(doc):
            return {
                "type": "internal",
                "page": page,
                "to": _point(link.get("to")),
                "zoom": link.get("zoom", 0),
            }, "valid"
        return {
            "type": "internal",
            "page": page,
            "name": link.get("nameddest") or link.get("to"),
        }, "invalid"
    if kind == pymupdf.LINK_URI:
        uri = link.get("uri") or ""
        status = (
            "invalid"
            if not uri or uri.startswith("bdoclink:")
            else "external_unchecked"
        )
        return {"type": "uri", "uri": uri}, status
    if kind in (pymupdf.LINK_LAUNCH, pymupdf.LINK_GOTOR):
        return {
            "type": "remote" if kind == pymupdf.LINK_GOTOR else "file",
            "file": link.get("file"),
            "page": link.get("page"),
            "to": _point(link.get("to")),
            "zoom": link.get("zoom", 0),
        }, "external_unchecked"
    return {"type": "unsupported", "kind": kind}, "unverified"


def inventory(doc):
    """Include raw Link annotations that MuPDF omits for broken destinations."""
    rows = []
    for page in doc:
        raw = page.get_text("rawdict")
        links = page.get_links()
        link_indices = {link.get("xref"): index for index, link in enumerate(links)}
        visible = {link.get("xref"): link for link in links}
        for xref, subtype, _name in page.annot_xrefs():
            if subtype != pymupdf.PDF_ANNOT_LINK:
                continue
            link = visible.pop(xref, None)
            if link is None:
                typ, value = doc.xref_get_key(xref, "Rect")
                nums = re.findall(r"[-+]?(?:\d*\.\d+|\d+)", value)
                rect = (
                    pymupdf.Rect(*map(float, nums)) * page.transformation_matrix
                    if typ == "array" and len(nums) == 4
                    else pymupdf.Rect()
                )
                action, dest = (
                    doc.xref_get_key(xref, "A/S"),
                    doc.xref_get_key(xref, "Dest"),
                )
                internal = action[1] == "/GoTo" or dest[0] != "null"
                rows.append(
                    {
                        "page": page.number,
                        "xref": xref,
                        "rect": list(rect),
                        "text": covered_text(page, rect, raw),
                        "target": {
                            "type": "unreadable",
                            "action": action[1],
                            "destination": dest[1]
                            if dest[0] != "null"
                            else doc.xref_get_key(xref, "A/D")[1],
                        },
                        "target_status": "invalid" if internal else "unverified",
                        "logical_id": doc.xref_get_key(xref, "BabelDOCLink")[1],
                    }
                )
            else:
                target, status = _target(doc, link)
                before, after = rect_context(raw, link["from"])
                rows.append(
                    {
                        "page": page.number,
                        "link_index": link_indices.get(xref),
                        "reference_role": reference_role(link.get("nameddest")),
                        "before": before,
                        "after": after,
                        "xref": xref,
                        "rect": list(link["from"]),
                        "text": covered_text(page, link["from"], raw),
                        "target": target,
                        "target_status": status,
                        "logical_id": doc.xref_get_key(xref, "BabelDOCLink")[1],
                    }
                )
        # Some MuPDF versions cannot associate supported links with annotation xrefs.
        for xref, link in visible.items():
            if xref and xref > 0:
                continue
            target, status = _target(doc, link)
            rows.append(
                {
                    "page": page.number,
                    "xref": xref,
                    "rect": list(link["from"]),
                    "text": covered_text(page, link["from"], raw),
                    "target": target,
                    "target_status": status,
                    "logical_id": "null",
                }
            )
    return rows


def _signature(target):
    value = dict(target)
    if isinstance(value.get("to"), list):
        value["to"] = [round(float(v), 1) for v in value["to"]]
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _numbers(text):
    return re.findall(r"\d+(?:\.\d+)*", unicodedata.normalize("NFKC", text))


def audit_links(source_pdf, pdf, *, report_path=None):
    """Compare each source occurrence against a mono PDF, including internal targets.

    This deliberately does not certify prose-link semantic alignment from geometry.
    Numeric labels and unchanged literal text can be checked deterministically.
    """
    with pymupdf.open(source_pdf) as source, pymupdf.open(pdf) as output:
        if len(source) != len(output):
            raise ValueError(
                "audit_links expects a mono PDF with the source page sequence"
            )
        src, dst = inventory(source), inventory(output)
    candidates = defaultdict(list)
    for row in dst:
        candidates[(row["page"], _signature(row["target"]))].append(row)
    findings, used = [], set()
    identified_output = any(row.get("logical_id") not in (None, "null") for row in dst)
    for index, row in enumerate(src):
        pool = [
            c
            for c in candidates[(row["page"], _signature(row["target"]))]
            if (c["page"], c["xref"]) not in used
        ]
        logical_id = f"{row['page']}:{row.get('link_index')}"
        if identified_output and row.get("link_index") is not None:
            pool = [
                candidate
                for candidate in pool
                if candidate.get("logical_id") == logical_id
            ]
        numbers = _numbers(row["text"])
        # Prefer the same label when a destination has several source occurrences.
        pool.sort(
            key=lambda c: (
                _numbers(c["text"]) != numbers,
                abs(c["rect"][1] - row["rect"][1]),
            )
        )
        actual = pool[0] if pool else None
        finding = {
            "source_index": index,
            "page": row["page"],
            "source_xref": row["xref"],
            "logical_id": logical_id,
            "source_text": row["text"],
            "target": row["target"],
            "source_target_status": row["target_status"],
            "source_rect": row["rect"],
        }
        if actual is None:
            finding.update(preserved=False, anchor_status="missing", output_rects=[])
        else:
            group = [actual]
            logical_id = actual.get("logical_id")
            if logical_id and logical_id != "null":
                group = [c for c in pool if c.get("logical_id") == logical_id]
            for item in group:
                used.add((item["page"], item["xref"]))
            text = "".join(
                c["text"]
                for c in sorted(group, key=lambda c: (c["rect"][1], c["rect"][0]))
            )
            anchor = "unverified"
            if numbers:
                anchor = "verified" if _numbers(text) == numbers else "wrong_label"
            elif row["text"] and re.sub(r"\s", "", normalize_anchor(text)).rstrip(
                ".,;，。；"
            ) in [
                variant.rstrip(".,;，。；") for variant in anchor_variants(row["text"])
            ]:
                anchor = "verified"
            elif not row["text"]:
                anchor = "no_source_text"
            role = row.get("reference_role")
            if (
                anchor == "verified"
                and role
                and not role_matches(
                    role, text, group[0].get("before", ""), group[-1].get("after", "")
                )
            ):
                anchor = "wrong_role"
            finding.update(
                preserved=True,
                anchor_status=anchor,
                output_text=text,
                output_rects=[c["rect"] for c in group],
            )
        findings.append(finding)
    summary = dict(Counter(f["anchor_status"] for f in findings))
    summary.update(
        source_links=len(src),
        output_annotations=len(dst),
        preserved=sum(f["preserved"] for f in findings),
        source_invalid=sum(r["target_status"] == "invalid" for r in src),
        output_invalid=sum(r["target_status"] == "invalid" for r in dst),
        external_unchecked=sum(r["target_status"] == "external_unchecked" for r in dst),
    )
    report = {
        "source_pdf": str(source_pdf),
        "pdf": str(pdf),
        "summary": summary,
        "targets_preserved": summary["preserved"] == len(src),
        "anchors_verified": all(f["anchor_status"] == "verified" for f in findings),
        "findings": findings,
        "invalid_destinations": [r for r in dst if r["target_status"] == "invalid"],
    }
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report["report"] = str(path)
    return report
