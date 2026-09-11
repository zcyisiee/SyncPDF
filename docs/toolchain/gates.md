# 门禁参考

本文汇总工具链的**全部门禁**：阈值、触发条件、产物路径、修复建议，
以及**如何人为触发验证**。

判定分两类：

- **硬门禁（hard）**：失败即中止当前阶段并报错（退出码 1）——用于「宁可失败，
  不可静默交付」的场景（漏译、链接丢失、协议破损）。
- **软门禁（soft）**：只记警告并继续——用于「内容质量提示」的场景
  （目录置信度、单条链接未映射）。

聚合查看全部门禁：

```bash
python experiments/toolchain_gates.py <workdir> [--pdf <源pdf>] [--json]
```

退出码：任一硬门禁失败 → `1` + `"ok": false`。产物缺失的阶段降级为
`not_available` / `not_reconstructed`（**不算失败**），因此该脚本可在
「只解析未重建」的中间态安全运行。

---

## 1. `layout_coverage_gate`（硬）

| 项 | 值 |
|---|---|
| 阈值 | 未命中任何 `page_layout` 区域的原生字符占比 ≤ `--layout-coverage-threshold`（默认 **0.005 = 0.5%**） |
| 触发 | `LayoutParser.process` 末尾（`compute_layout_coverage` → `_enforce_layout_coverage`） |
| 报错 | `RuntimeError("layout_coverage_gate: 未命中任何 layout 区域的原生字符占比 X% 超过阈值 Y%（uncovered=a/b）。逐页明细…见 <path>")` |
| 产物 | `<workdir>/<pdf名>/layout_coverage.json`（**无论是否过门禁都落盘**） |
| 阻断效果 | 解析中止，**不产出** `document.md` / `anchors.json` / `sheet.jsonl` |

**统计口径**

- 分母 = 该页全部 `page.pdf_character`（必须在 `ParagraphFinder` 之前统计）；
- 命中 = 字符 `visual_bbox` 与任一 `page_layout` box 有正面积交；
- 无字符页（扫描件/OCR workaround）分母为 0 → `coverage = 1.0`，不参与门禁。

**意味着什么**：超阈值说明「有一部分原生文字不在任何布局区域内」——
这些文字既不会被翻译，也不在保护区域内（旧实现会用字符聚类 `fallback_line`
兜底，现已删除）。因此超阈值时**必须人工确认**：
是真的漏排（需修布局/加保护），还是扫描件/异形版式的正常现象。

**修复路径**

1. 打开 `layout_coverage.json`，看 `pages[*].uncovered_text_preview` 与
   `uncovered_samples[].text`，定位未覆盖文字的页与内容；
2. 若是 MinerU 漏检：用 `MINERU_API_TOKEN` 重新解析（清掉缓存）或在
   MinerU 侧确认模型版本；
3. 若是已知的版式边角（如装饰性文字流），调大阈值：
   `--layout-coverage-threshold 0.02`；
4. 若是特定页问题，用 `--pages` 缩小范围定位。

**如何人为触发验证**（本仓库实测）：

```bash
# 把阈值调到低于真实未覆盖率（DeepSeek 实测 0.0436%）→ 必定触发
python -m babeldoc.tools.agent md-extract DeepSeek_V41_Tech_Report.pdf \
  --workdir tmp/gate-coverage \
  --layout-coverage-threshold 0.0001 \
  --mineru-json ~/.cache/babeldoc/mineru-layout.v1/ba68e2e40408125ae6d2f63a9a241b61c73910691c74ec1a2a7023c851eac08d.json
# 实测输出（退出码 1）：
# layout_coverage_gate: 未命中任何 layout 区域的原生字符占比 0.0436% 超过阈值 0.0100%
#   （uncovered=59/135415）。逐页明细与未覆盖文本片段见
#   tmp/gate-coverage/DeepSeek_V41_Tech_Report/layout_coverage.json；…
```

---

## 2. `link_uri_set_mismatch`（硬）

| 项 | 值 |
|---|---|
| 阈值 | mono 输出的 URI 集合必须**等于**源快照的 URI 集合（集合相等，不看数量） |
| 触发 | `PDFCreater.write` 保存前（`_enforce_link_uri_set` → `link_remap.assert_uri_set_matches`） |
| 报错 | `RuntimeError("link_uri_set_mismatch (mono): 源 URI N 条 / 输出 M 条；缺失 […5 条]；新增 […5 条]")` |
| 产物 | `agent/reconstruct_report.json` 的 `link_uri_set_match`；`agent/source/links.json`（源集合） |
| 跳过条件 | `only_include_translated_page=True`（删页模式口径不同）；无 `link_remap_state`（旧调用方） |

**修复路径**

1. `python experiments/toolchain_gates.py <workdir>` 看 `link_integrity` 的
   `missing_uris` / `extra_uris`；
2. 缺失通常是链接矩形映射后落在页外/被删，或 dual 搬运遗漏；
3. 若为「源链接盖的是纯图形（无文字）」导致 `unresolved`，检查
   `link_unresolved` 明细；
4. 确认不是删页模式误用。

**如何人为触发验证**：修改快照使集合不一致（或断言函数）——

```bash
python - <<'EOF'
import sys; sys.path.insert(0, '.')
from babeldoc.format.pdf.document_il.backend.link_remap import assert_uri_set_matches
try:
    assert_uri_set_matches({"https://a.example"}, {"https://b.example"}, context="mono")
except RuntimeError as exc:
    print("OK 触发:", exc)
EOF
# 实测输出：
# OK 触发: link_uri_set_mismatch (mono): 源 URI 1 条 / 输出 1 条；缺失 ['https://a.example']；
#   新增 ['https://b.example']
```

---

## 3. `link_unresolved`（软）

| 项 | 值 |
|---|---|
| 触发 | 链接三级回退全部失败（既无存活源字符，也无对应段落 box） |
| 行为 | **保持原矩形，不删链接**；该条进 `link_unresolved[]` |
| 产物 | `agent/reconstruct_report.json` 的 `link_unresolved[]`（`{page, link_index, uri}`）；`workflow.reconstruct` 返回值 |
| 阈值 | 无硬阈值；建议 0（实测 DeepSeek 410 条中 2 条） |

**典型成因**：链接矩形盖在**纯图形**上（如 code 图内的细横线），没有覆盖任何文本字符，
也没有所属段落。这类链接保持原位是安全的（它本来就不指向文字）。

**定位**

```bash
python - <<'EOF'
import json
recon = json.load(open('<workdir>/agent/reconstruct_report.json'))
print('total', recon['link_total'], 'remapped', recon['link_remapped'],
      'fallback_paragraph', recon['link_fallback_paragraph'],
      'unresolved', len(recon['link_unresolved']))
for item in recon['link_unresolved']:
    print(' ', item)
EOF
```

> 若无 `reconstruct_report.json`（legacy CLI 不落盘），用
> `python experiments/toolchain_gates.py <workdir>`——它会改用
> `output/*.mono.pdf` + `links.json` 重算 URI 集合门禁。

---

## 4. `toc_low_confidence`（软）

| 项 | 值 |
|---|---|
| 阈值 | 条目切分置信度 = 已解析条目行 / 页内文本行 < **0.6** |
| 触发 | `TocDetector.process`（`detect_toc_page` 判为目录页但切分不可靠） |
| 行为 | **不改结构**（页面保持原 `text` 段落进入翻译），只记警告 |
| 产物 | `agent/source/toc.json` 的 `summary.low_confidence_pages` / `warnings`；`config.layout_warnings` |

**修复路径**

1. 看 `toc.json.pages[*].candidate_lines` / `total_lines` / `confidence`；
2. 若该页确实是目录页但没被条目化，常见原因是页码未右对齐（超出 8pt 容差）
   或条目行不含字母；
3. 若为误判（正文页被当目录页），低置信度机制已自动放弃条目化，无需处理。

**如何人为触发验证**：构造「3 行条目 + 5 行普通文字」的页面 fixture
（见 `tests/test_toc_detector.py::TestDetectTocPage::test_normal_text_page_not_toc`
与 `split_toc_line` 的拒绝用例）。

---

## 5. 翻译协议门禁（硬）

来源：`apply_markdown`（`markdown_view.py`）+ `workflow.apply`
（`protocol.check_placeholders`）。

| 违规 | 触发 | 是否阻断 | 说明 |
|---|---|---|---|
| `extra_ids` | 译文出现源码没有的段落 id | ✓ | 模型伪造/复制了段落标记 |
| `anchor_order_mismatch` | 确定性修复后锚点顺序仍与源文不一致 | ✓ | 防跨 span 搬运 |
| `missing_ids` | 源码有、译文无 | ✗ | **回退原文**（`fallback_ids`），不阻断 |
| `empty_translation` | 标记在但正文为空 | ✗ | 回退原文 + 警告 |
| `label_mismatch` | 译文回写的 label 与 anchors 不一致 | ✗ | 警告（`label_mismatches[]`） |
| `empty_style_span` | 译文里出现空样式 span | ✗ | 警告（重建时按空文本处理） |
| `placeholder_lost` / `placeholder_hallucinated` | 公式占位符丢失/新增 | ✓ | agent 链路：`babeldoc/tools/agent/protocol.py::check_placeholders`（占位符多重集） |
| `formula_changed` | 公式内容被改动 | ✓ | MAS 链路：`babeldoc_core/protocol.py`（按 `formula_id` 校验；agent 链路以占位符多重集等价校验） |

**产物**：`agent/apply_report.json`（工具层）或 `agent/protocol_report.json`；
无报告时脚本用 `translated.jsonl` 与 `anchors.json` 做等价 id 覆盖校验。

**定位**

```bash
python -c "
import json; r=json.load(open('<workdir>/agent/apply_report.json'))
print('ok', r['ok'], 'violations', r['violations'][:5],
      'fallback', len(r.get('fallback_ids') or []),
      'repaired', len(r.get('repaired') or []))"
```

**修复路径**：`violations` 里的 id → `retranslate_ids --ids [...] --feedback "…"`
（≤2 轮）。

**如何人为触发验证**：

```bash
# 复制 workdir，往译文塞一个不存在的 id → extra_ids → ok=false
cp -r <workdir> tmp/gate-protocol
printf '\n<!-- id=P99-999 label=text -->\n伪造段落\n' >> tmp/gate-protocol/agent/translated.md
python -m babeldoc.tools.agent md-apply tmp/gate-protocol tmp/gate-protocol/agent/translated.md
# 预期：{"ok": false, "extra_ids": ["P99-999"], ...}，退出码 1
```

---

## 6. `layout_lint`（软）

来源：`layout_lint` / `review_layout` 工具；读 `agent/layout_lint.json`。

| 项 | 值 |
|---|---|
| 严重级别 | `P0` / `P1` / `P2` / `P3`（`SEV_ORDER`） |
| 产物 | `agent/layout_lint.json`（`{findings: [...], summary: {total}}`） |
| 触发 | 排版几何异常（框越界、缩放过大、行距过密等） |
| 修复 | `layout_patch`（写 `layout_overrides.json`）→ `reconstruct_pdf`（≤2 轮） |

---

## 7. 门禁速查表

| 门禁 | 硬/软 | 阈值 | 产物 | 阻断点 |
|---|---|---|---|---|
| `layout_coverage_gate` | 硬 | 未覆盖占比 ≤ 0.5% | `<workdir>/<pdf名>/layout_coverage.json` | `LayoutParser.process` |
| `link_uri_set_mismatch` | 硬 | 集合相等 | `reconstruct_report.json`（`link_uri_set_match`） | `PDFCreater.write` |
| `link_unresolved` | 软 | 记数 | `reconstruct_report.json`（`link_unresolved[]`） | — |
| `toc_low_confidence` | 软 | 置信度 ≥ 0.6 | `agent/source/toc.json` | — |
| protocol（anchors/占位符） | 硬 | 0 违规 | `apply_report.json` / `protocol_report.json` | `apply_markdown` |
| `layout_lint` | 软 | 记发现 | `agent/layout_lint.json` | — |

```bash
# 一次跑完全部（推荐）
python experiments/toolchain_gates.py <workdir> --pdf <源pdf> --json
```
