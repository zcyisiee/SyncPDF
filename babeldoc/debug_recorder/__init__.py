"""``babeldoc.debug_recorder``：管线诊断采集核心（零业务依赖）。

公共 API：

- 写入方：``DebugRecorder`` / ``NullRecorder``
- 运行标识与事件读取：``new_run_id`` / ``read_events``
- 进程内上下文：``get_current`` / ``set_current``
- 数据契约：``Box`` / ``PageFrame`` / ``Entity`` / ``Relation``
  （``frame_from_pymupdf_page`` 由 pymupdf 页构造 ``PageFrame``）

约定：

- 关闭采集时 recorder 为 ``None``；深层模块用 ``get_current()`` 取当前
  recorder 并判 ``if recorder:``（见 :mod:`babeldoc.debug_recorder.recorder`）。
- 归档写入失败**不抛出**，记入 ``capture_status``，不替换管线结果。
"""

from __future__ import annotations

from .model import PDF_TOPLEFT
from .model import Box
from .model import CompileCandidate
from .model import Entity
from .model import PageFrame
from .model import Relation
from .model import frame_from_pymupdf_page
from .recorder import ARTIFACTS_DIR
from .recorder import EVENTS_FILE
from .recorder import MANIFEST_FILE
from .recorder import SCHEMA_VERSION
from .recorder import SNAPSHOTS_DIR
from .recorder import STATUS_ERROR
from .recorder import STATUS_FINISHED
from .recorder import STATUS_INTERRUPTED
from .recorder import STATUS_RUNNING
from .recorder import DebugRecorder
from .recorder import NullRecorder
from .recorder import get_current
from .recorder import new_run_id
from .recorder import read_events
from .recorder import set_current

__all__ = [
    "ARTIFACTS_DIR",
    "EVENTS_FILE",
    "MANIFEST_FILE",
    "PDF_TOPLEFT",
    "SCHEMA_VERSION",
    "SNAPSHOTS_DIR",
    "STATUS_ERROR",
    "STATUS_FINISHED",
    "STATUS_INTERRUPTED",
    "STATUS_RUNNING",
    "Box",
    "CompileCandidate",
    "DebugRecorder",
    "Entity",
    "NullRecorder",
    "PageFrame",
    "Relation",
    "frame_from_pymupdf_page",
    "get_current",
    "new_run_id",
    "read_events",
    "set_current",
]
