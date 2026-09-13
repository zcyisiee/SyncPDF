# 工具 API 参考

本文列出工具链的**全部工具**：入参、出参、副作用（落盘）、典型调用、失败码。

工具有两套入口，能力重叠：

| 入口 | 调用方式 | 定位 |
|---|---|---|
| **legacy CLI**（本文示例以它为准） | `python -m babeldoc.tools.agent <cmd> …` | 名字空间固定，**与 cwd 无关**；可回放 MinerU 布局 |
| **工具层** | `python -m babeldoc_tools call <tool> --args-json '{…}'` | 稳定 JSON 契约、错误结构化、落盘报告 |

> ⚠ **同名包陷阱（实测）**：仓库里有两个 `babeldoc_tools`：
> 仓库根 `babeldoc_tools/`（MAS 版，`babeldoc_core.DocumentJob` 文本管线，
> **不含 MinerU 参数**）与 `skills/document-translate/tools/babeldoc_tools/`
> （agent 版，即本文描述的 MinerU/Markdown 管线）。Python 把 **cwd 排在
> `PYTHONPATH` 之前**，因此：
> - 在仓库根跑 `python -m babeldoc_tools call parse_document …` → 命中 **MAS 版**，
>   `mineru_json` 等参数报 `invalid_args: 未知参数`；
> - 在非仓库根目录（如 `/tmp`）跑 `skills/document-translate/tools/bin/bdt …`，
>   或 `cd skills/document-translate/tools` 后跑 `python -m babeldoc_tools` → 命中 **agent 版**。
>
> **建议**：需要 MinerU/回放/新产物时，用 legacy CLI
> （`python -m babeldoc.tools.agent`，见下）或 skills 的 `bdt` shim（在非仓库根 cwd 下）。

约定：stdout 恒为 `{"ok": true, "tool": …, "data": {…}}` 或
`{"ok": false, "error": {"code": …, "message": …}}`；退出码 0/1。

---

## 1. 工具总表

| 组 | 工具 | 一句话 |
|---|---|---|
| job | `job_create` / `job_status` / `job_resume` | 可恢复 job 的创建/查询/续跑 |
| parse | `parse_document` | PDF → 连续 Markdown（锚点）+ IR 状态 |
| parse | `dump_markdown` / `dump_ir` / `dump_text_layer` | 读产物 |
| parse | `inspect_selection` / `inspect_document` | 读翻译选择/段落决策报告 |
| translate | `translate_document` | 整篇翻译（默认 `agy` CLI），或导入外部译文 |
| translate | `retranslate_ids` / `repair_translation` | 按 id 重译（带 feedback） |
| translate | `apply_translation` | 校验收写回 IR |
| translate | `validate_translation` | 确定性协议门禁 |
| review | `review_document` / `review_protocol` / `review_fidelity` / `review_layout` | 结构/协议/语义/排版审查 |
| review | `backtranslate_check` | 高风险段回译 + 相似度 |
| layout | `reconstruct_pdf` | 应用排版覆盖重排 + dump 几何 |
| layout | `render_pages` | 页面 → PNG |
| layout | `layout_patch` / `layout_set` / `layout_lint` / `layout_locate` / `layout_rollback` / `inspect_layout` | 排版覆盖闭环 |
| version | `snapshot` / `restore` / `list_snapshots` / `job_snapshot` / `job_restore` | 快照与回滚 |
| report | `export_report` / `report` | 产出 `FINAL_REPORT.md` |

> 查单个工具的最新 schema：`python -m babeldoc_tools schema <tool>`
> （注意上面的同名包陷阱：该命令在仓库根会命中 MAS 版）。

---

## 2. 解析组

### `parse_document`

解析 PDF：MinerU 布局 → 段落 → 连续英文 Markdown（带行内锚点）。

**入参**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `pdf` | string | ✓ | 源 PDF 路径 |
| `workdir` | string | ✓ | 工作目录（产物写 `<workdir>/agent/`） |
| `layout` | string enum | | 只接受 `"mineru"`（默认）。**`native` 已移除** |
| `mineru_token` | string | | MinerU API token；缺省读 `MINERU_API_TOKEN` |
| `mineru_json` | string | | 回放缓存的 `layout.json`（不消耗 API） |
| `pages` | string | | 页范围，如 `1,2` 或 `1-3` |
| `lang_in` / `lang_out` | string | | 默认 `en` / `zh` |

**legacy CLI 额外旗标**（`python -m babeldoc.tools.agent md-extract`）：

| 旗标 | 说明 |
|---|---|
| `--mineru-cache-key <sha256>` | 按 PDF 内容哈希直接指定缓存 layout.json（`~/.cache/babeldoc/mineru-layout.v1/<key>.json`）；缓存未命中明确报错 |
| `--layout-coverage-threshold <float>` | 覆盖率门禁阈值，默认 `0.005` |

**出参（`data`）**

```jsonc
{ "paragraphs": 352, "chars": 139657,
  "label_counts": {"text": 220, "title": 59, "toc_entry": 54,
                   "figure_caption": 14, "table_caption": 5},
  "skipped_label_counts": {"reference": 100, "page_number": 50, …},
  "document_md": "<workdir>/agent/document.md",
  "anchors_json": "<workdir>/agent/anchors.json",
  "sheet": "<workdir>/agent/sheet.jsonl",
  "skipped_rows": [{"id": "P02-003", "page": 1,
                    "layout_label": "toc_entry_page",
                    "source": "4", "reason": "toc_entry_page"}],
  "workdir": "…", "layout": "mineru" }
```

**副作用（落盘）**

```text
agent/document.md  agent/anchors.json  agent/sheet.jsonl  agent/state.pkl
agent/source/bookmarks.json  agent/source/links.json  agent/source/toc.json
agent/source/mineru/provider_ir.json  agent/source/mineru/alignment.json
<workdir>/<pdf名>/layout_coverage.json
```

**典型调用**

```bash
# ① legacy CLI（推荐：cwd 无关，支持 MinerU 回放与所有新旗标）
python -m babeldoc.tools.agent md-extract DeepSeek_V41_Tech_Report.pdf \
  --workdir tmp/docs-smoke \
  --mineru-json ~/.cache/babeldoc/mineru-layout.v1/ba68e2e40408125ae6d2f63a9a241b61c73910691c74ec1a2a7023c851eac08d.json

# ② 工具层（agent 版）：必须在非仓库根 cwd 下调用 bdt shim，
#    否则会被仓库根的 MAS 版 babeldoc_tools 遮蔽（见开头同名包陷阱）。
skills/document-translate/tools/bin/bdt call parse_document --args-json '{
  "pdf": "DeepSeek_V41_Tech_Report.pdf",
  "workdir": "tmp/docs-smoke",
  "layout": "mineru",
  "mineru_json": "<缓存 layout.json 路径>"
}'
```

> 实测两种写法的返回一致（`paragraphs=352`）。若在仓库根直接用
> `python -m babeldoc_tools call parse_document`，`layout`/`mineru_json`
> 会报 `invalid_args: 未知参数`。

**失败码**

| code | 触发 | 修复 |
|---|---|---|
| `pdf_missing` | PDF 不存在 | 检查路径 |
| `layout_unsupported` | `layout != "mineru"` | 改 `mineru`（ONNX 后端已移除） |
| `mineru_token_missing` | 既无 token 也无 `mineru_json` | 设 `MINERU_API_TOKEN`，或用 `mineru_json` 回放 |
| — （`RuntimeError`） | `layout_coverage_gate: …` | 见 [`gates.md`](gates.md#1-layout_coverage_gate硬) |

---

## 3. 翻译组

### `translate_document`

**入参**

| 字段 | 说明 |
|---|---|
| `workdir` | ✓ |
| `translated_md` | 导入外部译文（无 `agy` CLI 时的替代路径） |
| `provider` | Python provider 对象（进程内调用；省略则回退源文） |
| `model` / `effort` | 传给翻译 CLI 的参数（legacy 写法：`--model` / `--effort`） |

**出参**：`{"state": "translated"}` + `warnings`（若发生回退）
**副作用**：`agent/translated.md`（+ `usage.json`）
**失败码**：`document_missing`（未先 parse）、`model_cli_missing`、
`model_failed: …`、`translated_md_missing`

```bash
# 工具层（agent 版；非仓库根 cwd 下用 bdt shim）
skills/document-translate/tools/bin/bdt call translate_document --args-json '{"workdir":"tmp/docs-smoke"}'
# 或导入已生成译文：
skills/document-translate/tools/bin/bdt call translate_document --args-json \
  '{"workdir":"tmp/docs-smoke","translated_md":"/path/to/translated.md"}'
```

### `retranslate_ids`

按 id 重译（供 review 发现 blocker 后修补）。

**入参**：`workdir`、`ids[]`、`translated{id: 文本}`、`feedback`（legacy）
**出参**：`{"ids": [...], "translated_md": …}`
**失败码**：`ids_unknown`（id 不在解析产物中）

### `apply_translation`

**入参**：`workdir`、`translated_md`（默认 `agent/translated.md`）
**出参**

```jsonc
{ "ok": true, "applied": 352, "violations": [], "repaired": [{"id":"P09-006","mode":"accepted"}],
  "warnings": ["empty_style_span: id P12-002 style 1", "anchor_reordered: id P09-006"],
  "fallback_ids": [], "label_mismatches": [], "empty_ids": [],
  "markdown_sheet": "<workdir>/agent/translated.jsonl" }
```

**副作用**：`agent/translated.jsonl`、`agent/il_translated.applied.json`、
`agent/apply_report.json`
**失败码**：`ok=false` + `violations[]`（`anchor_multiset_mismatch`、`extra_ids` 等），
退出码 1

### `validate_translation` / `review_protocol`

**出参**：`{"ok": bool, "violations": [...]}` → 落盘 `agent/protocol_report.json`

---

## 4. 重建与渲染

### `reconstruct_pdf`

**入参**：`workdir`、`output_dir`（默认 `<workdir>/output`）、`dual`（默认 true）、
`watermark`、`stats`（默认 true，附页数/目录/链接统计）
**出参**

```jsonc
{ "mono_pdf": "…/output/….zh.mono.pdf", "dual_pdf": "…/….zh.dual.pdf",
  "layout_geometry": "<workdir>/agent/layout_geometry.json",
  "layout_override_stats": {"font_scaled": 0, "box_changed": 0, "clamped": 0,
                            "unmatched_ids": [], "warnings": []},
  "layout_warnings": [],
  "link_total": 410, "link_remapped": 408, "link_fallback_paragraph": 350,
  "link_unresolved": [{"page": 15, "link_index": 0, "uri": null}],
  "link_uri_set_match": true,
  "stats": {"mono": {"pages": 51, "toc_entries": 54, "links": 410}} }
```

**副作用**：`output/*.pdf`、`agent/layout_geometry.json`、
`agent/reconstruct_report.json`
**失败码**：`link_uri_set_mismatch (mono): …`（硬）、
`FileNotFoundError`（未先 parse，`input.pdf` 缺失）

```bash
python -m babeldoc.tools.agent reconstruct tmp/docs-smoke --output-dir tmp/docs-smoke/output --dual
# 工具层（agent 版，非仓库根 cwd）：
skills/document-translate/tools/bin/bdt call reconstruct_pdf --args-json '{"workdir":"tmp/docs-smoke","dual":true}'
```

### `render_pages`

**入参**：`workdir` 或 `pdf`、`pages`（`"2,3"` / `"1-3"`）、`dpi`（默认 110）、`out_dir`
**出参**：`{"images": ["…/render/page-02.png", …]}`
**副作用**：`render/page-NN.png`（或 `--out-dir`）

```bash
python -m babeldoc.tools.agent render tmp/docs-smoke/output/DeepSeek_V41_Tech_Report.no_watermark.zh.mono.pdf \
  --pages 2,3 --dpi 100 --out-dir tmp/docs-smoke/render
```

---

## 5. 排版微调组

| 工具 | 入参要点 | 副作用 |
|---|---|---|
| `layout_patch`（别名 `layout_set`） | `patch`（`paragraphs{id:{box/font_scale/…}}` / `pages{page:{font_scale}}`）、`reason`、`finding_id`、`round`、`render_verified`、`clear` | `agent/layout_overrides.json`（写前备份） |
| `layout_lint` | `workdir` | 读 `agent/layout_lint.json` |
| `layout_locate` | `workdir`、`page`、`box` | 读 `agent/layout_geometry.json` |
| `layout_rollback` | `workdir` | 删 `layout_overrides.json`，返回备份 |
| `inspect_layout` | `workdir` | 读 `layout_geometry.json` |

**失败码**：`patch_missing`（既无 patch 又无 clear）、`invalid_box`、
`geometry_missing`（未先 reconstruct）

---

## 6. 审查组

| 工具 | 出参 | 副作用 |
|---|---|---|
| `review_document`（别名 `review_fidelity`） | `{"verdict": "pass\|needs_fix", "findings": [...]}` | `agent/review_verdict.json` |
| `backtranslate_check` | `{"checked": bool, …}` | — |
| `dump_text_layer` | 文本层 | — |

`review_document` 的检查项（`skills/document-translate/tools/babeldoc_tools/review.py`）：
apply 报告、段内完整性、占位符残留、页数/目录/链接一致性、标题字号。

---

## 7. 报告与版本

| 工具 | 出参 | 副作用 |
|---|---|---|
| `export_report`（别名 `report`） | `{"report": path}` | `agent/FINAL_REPORT.md` + `<workdir>/FINAL_REPORT.md` |
| `snapshot` / `list_snapshots` / `restore` | 快照名与路径 | `agent/snapshots/<name>/` |
| `job_create` / `job_status` / `job_resume` | manifest 状态 | `agent/manifest.json` |

---

## 8. legacy CLI 速查

```bash
python -m babeldoc.tools.agent --help
# extract / apply / reconstruct / render / md-extract / md-apply

# 关键新旗标（md-extract 与 extract 都支持）
--mineru-cache-key <sha256>            # 按内容哈希指定缓存 layout.json
--mineru-json <path>                   # 直接给 layout.json 路径
--layout-coverage-threshold <float>    # 默认 0.005
```

---

## 9. 门禁聚合脚本

```bash
python experiments/toolchain_gates.py <workdir> [--pdf <源pdf>] [--json]
```

五项门禁聚合为 JSON；任一硬门禁失败 → 退出码 1。详见 [`gates.md`](gates.md)。
