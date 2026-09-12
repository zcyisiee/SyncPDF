"""LaTeX bbox 排版（实验特性，默认关闭）。

在 generate 阶段对满足条件的段落用 XeLaTeX 在原 bbox 内重新排版并贴回，
修复现有逐字渲染的断行缺陷（行末异常留白、过早折行）。能力缺失或任何
失败都会安全回退现有渲染路径。

公共入口：

- :func:`record_source_texts` — 翻译之前记录源文（未翻译段判定）；
- :func:`capture_layout_sources` — Typesetting 之前捕获源几何 + 公式融合 body；
- :class:`LatexBboxOverlay` — PDFCreater.write 的 ``prepare``（内容流前预选+编译）
  与 ``stamp``（内容流后贴片）两步入口；
- :func:`apply_latex_bbox_overlay` — 直接跑完整 overlay（prepare + stamp）；
- :func:`probe_latex_capability` — XeLaTeX/宏包/字体能力探测。
"""

from babeldoc.format.pdf.document_il.backend.latex_bbox.capability import (
    LatexCapability,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.capability import (
    probe_latex_capability,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import FormulaLatexIndex
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import FuseResult
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import escape_latex
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import fuse_paragraph
from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import LatexBboxOverlay
from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import (
    apply_latex_bbox_overlay,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import (
    capture_layout_sources,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import (
    record_source_texts,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import write_report
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    BboxStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampRequest
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampResult

__all__ = [
    "LatexCapability",
    "probe_latex_capability",
    "FormulaLatexIndex",
    "FuseResult",
    "escape_latex",
    "fuse_paragraph",
    "LatexBboxOverlay",
    "apply_latex_bbox_overlay",
    "capture_layout_sources",
    "record_source_texts",
    "write_report",
    "BboxStampRenderer",
    "StampRequest",
    "StampResult",
]
