# Task: A2 — 删除 reorder 强制回贴，尊重模型语序重排

## Objective

`markdown_view.repair_target` 的 reorder 模式把源文锚点顺序强行贴回译文，破坏模型合法的中文语序调整（实测 P01-017："coordinates T rounds across S sub-agents" → "在 S 个子智能体间协调 T 轮"被回贴成语义颠倒的"在 T 个子智能体间协调 S 轮"）。删除 reorder 分支；锚点多重集校验（丢/幻觉）与 proportional 兜底保留。

## Context

- 分支 `feature/latex-bbox-layout`。
- 坏例数据：`tmp/md-test/agent/apply_report.json`（`repaired: [{"id": "P01-017", "mode": "reorder"}]`）；`tmp/md-test/agent/translated.jsonl` P01-017 的 target（{v5}=T 轮数、{v6}=S 智能体数被互换）；最终 PDF `tmp/md-test/output/测试.no_watermark.zh.mono.pdf` 含"在 𝑇 个子智能体间协调 𝑆 轮"。
- **下游兼容证据（删除 reorder 安全）**：
  1. `il_translator.py:777 parse_translate_output` 用 `re.finditer(combined_pattern)` 按出现顺序逐个匹配占位符 → 与锚点顺序无关，任意顺序都能回填到正确 composition；
  2. `protocol.check_placeholders`（`workflow.apply` 调用）校验多重集（Counter 差集），顺序无关；
  3. 唯一依赖顺序的就是 `apply_markdown:907-912` 的 `src_seq != tgt_seq` 判等和 `repair_target` 本身。
- `_fix_empty_spans`（`[[SN]][[/SN]]X → [[SN]]X[[/SN]]`）与顺序无关，保留。
- 必读：`babeldoc/tools/agent/markdown_view.py:230-282`（repair_target 全文）、`:895-915`（apply_markdown 调用点）、`tests/test_markdown_format.py`（现有测试风格）。

## Deliverables

1. `markdown_view.py` `repair_target` 重构：
   - 删除"多重集一致且顺序不同 → 按模型位置回贴源文锚点序列"的 reorder 主分支；
   - 保留 proportional 模式（多重集不一致时按源文各文本段长度占比等比投放）；
   - 多重集一致且顺序相同 → 原样返回（现有行为）；
   - 多重集一致但顺序不同 → **接受模型顺序**，只跑 `_fix_empty_spans`，返回 mode 用新值（如 `"accepted"` 或保持函数签名的字符串约定）。
2. `apply_markdown` 调用点（:895-915）：
   - `src_seq != tgt_seq` 且多重集一致 → 不修复，`warnings.append(f"anchor_reordered: id {pid}")`（不阻断，供观察真实发生率）；
   - `has_empty`（`[[SN]][[/SN]]` 空对）→ 仍跑 `_fix_empty_spans` 修复；
   - 多重集不一致 → 仍走 proportional（现有）；
   - 修复后二次校验 `anchor_sequence(body) != src_seq` 的 violations 判定改为**多重集校验**（顺序不再视为违规）。
3. `tests/test_markdown_format.py` 追加测试：
   - reorder 场景：源文 `A [[F1]] rounds across [[F2]] agents`、译文 `在 [[F2]] 个智能体间协调 [[F1]] 轮` → apply 后 target 保持模型顺序（`{v2}` 在前、`{v1}` 在后）；
   - 丢锚点场景：译文缺 `[[F2]]` → 仍触发 proportional 修复；
   - 幻觉锚点：译文多出 `[[F9]]` → proportional 修复或 violations（维持现行为）。
4. `experiments/protocol_report.py` / 文档中若提及 reorder 模式，同步更新措辞（grep `reorder` 全仓确认）。

## Constraints

- 不改 `protocol.py`（多重集校验已正确）；
- 不改 `il_translator.parse_translate_output`；
- `apply_report.json` 的 `repaired[]` 结构保持兼容（mode 值变化是允许的，下游只有人工查看）；
- 单测基线 308 passed / 7 failed 不变（新增测试全过）。

## Validation

```bash
python3 -m pytest tests/test_markdown_format.py -q
python3 -m pytest tests/ -q
# 2512 p7 好例回放（译文已有）：
python3 -m babeldoc.tools.agent md-apply tmp/md-2512-p7 tmp/md-2512-p7/agent/translated.md
# 测试.pdf 全链路验证语义：
python3 -m babeldoc.tools.agent md-apply tmp/md-test tmp/md-test/agent/translated.md
# 然后检查 apply 报告：P01-017 不再出现在 repaired[]，warnings 含 anchor_reordered: id P01-017
```

## Report back

- 修改/新增文件清单与每处改动目的；
- 验证命令完整输出摘要（P01-017 的 target 前后对照）；
- 发现并修复的缺陷（若有）；
- 任何偏离 brief 的决定及理由。
