"""PDF 注释快照：目录（书签）与超链接。

对应 ``.plan/minerU深度融合.md`` 板块 4/5 的解析侧契约：重建阶段不能依赖
「在译文里搜同名文字」来定位注释——必须在解析阶段就把源 PDF 的注释结构与
它覆盖的源字符/源页绑定下来，重建时按对象身份映射回去。

本模块当前实现**书签快照**（板块 4）。超链接快照（板块 5）会扩展同一文件。

产物：``<workdir>/agent/source/bookmarks.json``

```json
[
  {"level": 1, "title": "Introduction", "page": 4, "to": [70.87, 756.85],
   "nameddest": "section.1", "collapse": false}
]
```

``page`` 是 1-based 页码（``doc.get_toc()`` 口径）；``to`` 是目标点坐标
（``Point``，页面坐标），无显式目标时为 ``null``。``nameddest`` 保留源 PDF 的
命名目的地，便于重建时对照。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

BOOKMARKS_ARTIFACT = "bookmarks.json"
# 书签标题的最大保留长度（防御异常长标题）
MAX_TITLE_CHARS = 400


def snapshot_bookmarks(pdf_path: str | Path) -> list[dict]:
    """读取 PDF 书签（outline），返回可 JSON 序列化的列表。

    用 ``get_toc(simple=False)`` 拿完整目标信息（``to`` / ``nameddest``）；
    环境不支持时退回 ``get_toc()``（只有 level/title/page）。
    """
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        try:
            raw = doc.get_toc(simple=False)
        except Exception:  # noqa: BLE001 - 老版本/异常结构回退
            logger.debug("get_toc(simple=False) 失败，回退 simple", exc_info=True)
            raw = doc.get_toc()
    finally:
        doc.close()

    entries: list[dict] = []
    for item in raw:
        if not isinstance(item, list | tuple) or len(item) < 3:
            continue
        level, title, page = item[0], item[1], item[2]
        detail = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
        to_point = detail.get("to")
        entries.append(
            {
                "level": int(level),
                "title": str(title or "")[:MAX_TITLE_CHARS],
                "page": int(page),
                "to": (
                    [float(to_point.x), float(to_point.y)]
                    if to_point is not None
                    else None
                ),
                "nameddest": detail.get("nameddest"),
                "collapse": bool(detail.get("collapse") or False),
            }
        )
    return entries


def write_bookmarks(pdf_path: str | Path, out_path: str | Path) -> dict:
    """快照书签并落盘；返回 ``{"path", "count"}``。失败只 warning。"""
    out_path = Path(out_path)
    try:
        entries = snapshot_bookmarks(pdf_path)
    except Exception:  # noqa: BLE001 - 注释快照是审计产物，不阻断解析
        logger.warning("书签快照失败: %s", pdf_path, exc_info=True)
        entries = []
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(out_path)
    except OSError:
        logger.warning("写入 bookmarks.json 失败", exc_info=True)
        return {"path": str(out_path), "count": len(entries), "written": False}
    return {"path": str(out_path), "count": len(entries), "written": True}


def bookmarks_path(agent_dir: str | Path) -> Path:
    """``<agent>/source/bookmarks.json``。"""
    return Path(agent_dir) / "source" / BOOKMARKS_ARTIFACT
