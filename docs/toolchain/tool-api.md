# 工具 API 参考

本文列出工具链的**全部工具**：入参、出参、副作用（落盘）、典型调用、失败码。

工具有两套入口，能力重叠：

| 入口 | 调用方式 | 定位 |
|---|---|---|
| **legacy CLI**（本文示例以它为准） | `python -m babeldoc.tools.agent <cmd> …` | 名字空间固定，**与 cwd 无关**；可回放 MinerU 布局 |
| **工具层 `bdt`** | `bdt <subcommand> [flags]`（= `python -m babeldoc_tools`） | 稳定 JSON 契约、错误结构化、落盘报告 |

> 仓库里只有一个 `babeldoc_tools` 包，位于仓库根 `babeldoc_tools/`（即本文描述的
> MinerU/Markdown agent 管线）。安装后 `bdt`（或 `python -m babeldoc_tools`）在任何
> cwd 下命中的都是这一份代码。

约定：stdout 恒为单行 `{"ok": true, "data": {…}}` 或
`{"ok": false, "error": {"code": …, "message": …}}`；日志/进度走 stderr；退出码 0/1。

---

## 1. 工具总表

`bdt` 子命令固定为 7 个（`bdt --help`）：

| 子命令 | 实现函数 | 一句话 |
|---|---|---|
| `parse` | `babeldoc_tools.parse.parse_document` | PDF → 连续 Markdown（锚点）+ IR 状态 |
| `translate` | `translate.translate_document`（`--ids` → `retranslate_ids`） | 整篇翻译（默认 `agy` CLI），或导入外部译文 |
| `apply` | `translate.apply_translation` | 校验译文写回 IR |
| `build` | `layout.build_pdf`（`reconstruct_pdf` + 可选 `render_pages`） | 应用排版覆盖重排 + dump 几何 |
| `check` | `review.review_document`（占位） | 结构/协议审查，`verdict=pass\|needs_fix` |
| `layout-set` | `layout.layout_set` | 写/清排版覆盖 |
| `report` | `report.report` | 产出 `FINAL_REPORT.md` |

内部 Python 函数（已从公开 CLI 移除，供 reviewer agent/脚本调用）：
`layout.layout_lint`、`layout.layout_locate`、`layout.render_pages`、
`layout.dump_text_layer`、`review.backtranslate_check`。

> 查子命令参数：`bdt <subcommand> --help`（U2 起不再有 `list` / `schema` 元命令）。

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

# ② 工具层 `bdt`（agent 版）：任意 cwd 均可。
bdt parse DeepSeek_V41_Tech_Report.pdf \
  --workdir tmp/docs-smoke \
  --layout mineru \
  --mineru-json "<缓存 layout.json 路径>"
```

> 实测两种写法的返回一致（`paragraphs=352`）。

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
# 工具层（agent 版）：
bdt translate --workdir tmp/docs-smoke
# 或导入已生成译文：
bdt translate --workdir tmp/docs-smoke --markdown /path/to/translated.md
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
# 工具层：
bdt build --workdir tmp/docs-smoke --output-dir tmp/docs-smoke/output --dual
```

### `render_pages`（内部函数）

**入参**：`workdir` 或 `pdf`、`pages`（`"2,3"` / `"1-3"`）、`dpi`（默认 110）、`out_dir`
**出参**：`{"images": ["…/render/page-02.png", …]}`
**副作用**：`render/page-NN.png`（或 `--out-dir`）

```bash
# 公开 CLI：bdt build --render 2,3（重建后渲染）
bdt build --workdir tmp/docs-smoke --render 2,3
# legacy CLI：
python -m babeldoc.tools.agent render tmp/docs-smoke/output/DeepSeek_V41_Tech_Report.no_watermark.zh.mono.pdf \
  --pages 2,3 --dpi 100 --out-dir tmp/docs-smoke/render
```

---

## 5. 排版微调组

| 入口 | 入参要点 | 副作用 |
|---|---|---|
| `bdt layout-set`（`layout.layout_set`） | `--patch '{"paragraphs":{id:{box/font_scale/…}},"pages":{…}}'`、`--reason`、`--clear` | `agent/layout_overrides.json`（写前备份） |
| `layout.layout_lint`（内部函数） | `workdir`、`page`、`code`、`min_sev` | 读 `layout_geometry.json`，写 `agent/layout_lint.json` |
| `layout.layout_locate`（内部函数） | `workdir`、`page`、`box`、`text` | 读 `agent/layout_geometry.json` |

**失败码**：`patch_missing`（既无 patch 又无 clear）、`invalid_box`、
`geometry_missing`（未先 `bdt build`）

---

## 6. 审查组

| 入口 | 出参 | 副作用 |
|---|---|---|
| `bdt check`（`review.review_document`，当前为转调占位） | `{"verdict": "pass\|needs_fix", "blockers": [...], "warnings": [...]}` | `agent/review_verdict.json` |
| `review.backtranslate_check`（内部函数） | `{"ids": [...], "per_id": [...], "needs_retranslate_ids": [...]}` | `agent/backtranslation_check.json` |
| `layout.dump_text_layer`（内部函数） | 文本层文件清单 | `output/text_layer/page-XX.txt` |

`review_document` 的检查项（`babeldoc_tools/review.py`）：
apply 报告、段内完整性、占位符残留、页数/目录/链接一致性、标题字号。

---

## 7. 报告

| 入口 | 出参 | 副作用 |
|---|---|---|
| `bdt report`（`report.report`） | `{"report": path}` | `<workdir>/FINAL_REPORT.md` |

> 快照（`snapshot` / `restore` / `list_snapshots`）与 `babeldoc_tools/version.py`
> 已在 U2 删除；回滚改用 `bdt layout-set --clear` 与重新 `bdt apply`。

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
