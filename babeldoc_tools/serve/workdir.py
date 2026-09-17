"""workdir 产物读取：``agent/*`` 与 ``debug/runs/<run_id>/*`` 的容错只读层。

服务读文档产物只有两条路：:meth:`babeldoc_tools.serve.store.DocumentStore.resolve`
给出 workdir 绝对路径（安全边界），本模块把那个 workdir 里的产物读成 Python 值
（对外形状见 ``docs/frontend/api.md`` §3.1）。

产物是信任边界内的**本地产物**：缺失 / JSON 损坏 / 外形不符一律降级成 ``None``
（或空列表 + 一个 ``available`` 标志），由视图层如实上报 —— 单个产物坏掉不能让
整个端点 500，也不能把"没有数据"说成"数据是空的"（``None`` 与 ``{}`` 语义不同）。

只读：本模块不创建、不修改任何文件。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

__all__ = ["AGENT_DIR", "RUN_ID_RE", "WorkdirReader"]

#: 产物目录名（与 ``babeldoc_tools.common.AGENT_DIR`` 一致）。
AGENT_DIR = "agent"

#: run_id 目录名形态：``babeldoc.debug_recorder.new_run_id()`` 的产物
#: ``<UTC时间戳>Z-<6位十六进制>``。固定长度 → 目录名字典序即时间序，可直接取最大。
RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")

#: run 归档内的相对路径（统一用 ``/``，见 ``babeldoc.debug_recorder`` 的布局约定）。
_MANIFEST = "manifest.json"
_PARSE_SNAPSHOT = "snapshots/parse/paragraphs.json"


class WorkdirReader:
    """一个 workdir 的只读产物访问器。

    同一个 reader 实例内每份产物只读一次（一次 HTTP 请求创建一个 reader），
    **不跨请求复用缓存** —— 每次都重新读盘，避免读到上一轮的旧产物。
    返回的 dict/list 是同一对象，调用方不得修改。
    """

    def __init__(self, workdir: Path | str) -> None:
        self.workdir = Path(workdir)
        self._cache: dict[str, Any] = {}

    # ------------------------------------------------------------ 低层读取
    @property
    def agent_dir(self) -> Path:
        return self.workdir / AGENT_DIR

    def _read_json_at(self, path: Path) -> Any:
        """读 JSON；缺失 / 不可读 / 解析失败 → ``None``（非 dict 也算不可用）。"""
        key = f"json:{path}"
        if key not in self._cache:
            try:
                self._cache[key] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._cache[key] = None
        return self._cache[key]

    def _read_jsonl_at(self, path: Path) -> list[dict] | None:
        """读 JSONL；文件缺失 / 不可读 → ``None``，单行损坏只跳过该行。"""
        key = f"jsonl:{path}"
        if key not in self._cache:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                self._cache[key] = None
                return None
            rows: list[dict] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
            self._cache[key] = rows
        return self._cache[key]

    def _agent(self, relative: str) -> Path:
        return self.agent_dir / relative

    def _run_dir_with(self, relative: str) -> tuple[str, Path] | None:
        """从新到旧找第一个含 ``relative`` 的 run：``(run_id, run_dir)``。

        ``relative`` 为空串 = 只要求 run 目录存在。最新 run 可能刚创建、产物
        还没发布（归档不完整）：用次新的真实数据比谎报"没有"更准确，返回的
        ``run_id`` 始终是数据实际来源的那一个。
        """
        for run_id in self._run_ids():
            run_dir = self.workdir / "debug" / "runs" / run_id
            if not relative or (run_dir / relative).is_file():
                return run_id, run_dir
        return None

    def _run_ids(self) -> list[str]:
        """``debug/runs`` 下的 run_id，新→旧（目录名排序；非法名不参与）。"""
        key = "run_ids"
        if key not in self._cache:
            runs_dir = self.workdir / "debug" / "runs"
            try:
                names = [entry.name for entry in runs_dir.iterdir() if entry.is_dir()]
            except OSError:
                names = []
            self._cache[key] = sorted(
                (name for name in names if RUN_ID_RE.match(name)), reverse=True
            )
        return self._cache[key]

    # -------------------------------------------------------------- 产物读取
    def run_state(self) -> dict:
        """``agent/run_state.json``；缺失 / 损坏 → 空 dict。"""
        state = self._read_json_at(self._agent("run_state.json"))
        return state if isinstance(state, dict) else {}

    def anchors(self) -> dict | None:
        """``agent/anchors.json``（``{rows, skipped}``）；不可用 → ``None``。

        注意其中的 ``page`` 是 **0 基**页码（``markdown_view`` 直接写 IL 的
        ``page_number``），与几何 / 快照的 1 基页码不同 —— 视图层负责归一。
        """
        payload = self._read_json_at(self._agent("anchors.json"))
        return payload if isinstance(payload, dict) else None

    def translated_targets(self) -> dict[str, str] | None:
        """``agent/translated.jsonl`` → ``{id: target}``；产物不可用 → ``None``。

        ``None`` = 没有这个产物，``{}`` = 产物存在但没有可用行。
        """
        rows = self._read_jsonl_at(self._agent("translated.jsonl"))
        if rows is None:
            return None
        targets: dict[str, str] = {}
        for row in rows:
            paragraph_id = row.get("id")
            target = row.get("target")
            if isinstance(paragraph_id, str) and isinstance(target, str):
                targets[paragraph_id] = target
        return targets

    def layout_geometry(self) -> dict | None:
        """``agent/layout_geometry.json``；不可用 → ``None``。

        其中 box 是 **PDF 原生坐标（左下原点、y 向上）**，与 parse 快照的
        ``pdf_topleft`` 不同，端点用 ``coord_system`` 如实标注，不做静默转换。
        """
        payload = self._read_json_at(self._agent("layout_geometry.json"))
        return payload if isinstance(payload, dict) else None

    def review_verdict(self) -> dict | None:
        """``agent/review_verdict.json``（结构审查）；不可用 → ``None``。"""
        payload = self._read_json_at(self._agent("review_verdict.json"))
        return payload if isinstance(payload, dict) else None

    def layout_lint(self) -> dict | None:
        """``agent/layout_lint.json``（排版 lint）；不可用 → ``None``。"""
        payload = self._read_json_at(self._agent("layout_lint.json"))
        return payload if isinstance(payload, dict) else None

    def link_audit(self) -> dict | None:
        """``agent/link_audit.json``（链接审计）；不可用 → ``None``。"""
        payload = self._read_json_at(self._agent("link_audit.json"))
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------ run 归档
    def latest_run_id(self) -> str | None:
        """最新 run 的 id（目录名排序取最大）；没有 run 目录 → ``None``。"""
        found = self._run_dir_with("")
        return found[0] if found else None

    def latest_manifest(self) -> tuple[str, dict] | None:
        """最新含 manifest 的 run：``(run_id, manifest)``；没有 → ``None``。"""
        found = self._run_dir_with(_MANIFEST)
        if found is None:
            return None
        manifest = self._read_json_at(found[1] / _MANIFEST)
        return (found[0], manifest) if isinstance(manifest, dict) else None

    def parse_snapshot(self) -> tuple[str, dict] | None:
        """最新含 parse 段落快照的 run：``(run_id, snapshot)``；没有 → ``None``。

        快照内的 box 是 ``pdf_topleft``（``babeldoc/debug_recorder`` 的坐标系契约）。
        """
        found = self._run_dir_with(_PARSE_SNAPSHOT)
        if found is None:
            return None
        snapshot = self._read_json_at(found[1] / _PARSE_SNAPSHOT)
        return (found[0], snapshot) if isinstance(snapshot, dict) else None
