# babeldoc_tools：文档翻译工具层

把 BabelDOC 的 agent 工作流（解析 → 翻译 → 审查 → 重建 → 排版微调）包成
**稳定、机读、可组合**的工具。设计给两类调用者：

1. 编排者（主 Agent）：按 JSON 参数调工具，读 JSON 结果决定下一步；
2. 后续的 MCP wrapper：直接复用 `registry.dispatch`，无需重写逻辑。

## 调用方式

```bash
# 方式 A：不安装，直接跑（推荐在仓库根目录）
PYTHONPATH=skills/document-translate/tools python -m babeldoc_tools list
skills/document-translate/tools/bin/bdt list          # 等价 shim（自动找 .venv）

# 方式 B：可编辑安装
pip install -e skills/document-translate/tools        # 提供 bdt 命令
```

三个子命令：

```bash
python -m babeldoc_tools list                          # 工具清单（含 JSON Schema）
python -m babeldoc_tools schema layout_set             # 单个工具的入参 schema
python -m babeldoc_tools call layout_set \
    --args-json '{"workdir": "tmp/wd", "patch": {"paragraphs": {"P05-012": {"scale_cap": 0.9}}}}'
python -m babeldoc_tools call layout_lint --workdir tmp/wd --arg min_sev='"P1"'
```

约定：

- **stdout 永远是 JSON**：`{"ok": true, "tool": ..., "data": {...}}` 或
  `{"ok": false, "tool": ..., "error": {"code", "message", ...}}`；
- 退出码：`0` = ok，`1` = 工具失败（含 `invalid_args` / `unknown_tool`），`2` = 用法错误；
- **错误不抛栈**：工具内部异常被包成 `tool_exception` 并附 traceback 尾部；
  可预期的失败用 `ToolError(code, message, **extra)` 抛出，错误码直接透传。

## 工具清单

| 组 | 工具 | 作用 |
|---|---|---|
| parse | `parse_document` | PDF → 连续 Markdown（带锚点）+ `state.pkl` / `sheet.jsonl` |
| translate | `translate_document` | 整篇翻译（默认调 `agy` CLI），自动补译漏行 |
| translate | `retranslate_ids` | 按 id 补译/重译（可带 feedback），合并回 `translated.md` 并 apply |
| translate | `apply_translation` | 校验译文 Markdown 并写回 IR（确定性修复锚点/双标点/注释残留） |
| review | `review_document` | 确定性审查 → `verdict: pass \| needs_fix` + blockers/warnings |
| review | `backtranslate_check` | 高风险段落回译 + Levenshtein 相似度判定 |
| layout | `reconstruct_pdf` | 应用排版覆盖重排 mono/dual PDF + dump `layout_geometry.json` |
| layout | `render_pages` | PDF 页 → PNG（视觉审查） |
| layout | `layout_set` | 写入/合并段落级排版覆盖（可回滚） |
| layout | `layout_lint` | 排版缺陷清单（越界/重叠/字号塌缩/压图 + P2 信息项） |
| layout | `layout_locate` | 按页 + box/text 定位段落 id |
| version | `snapshot` / `restore` / `list_snapshots` | 小文件快照与回滚（不含 55MB `state.pkl`） |
| report | `report` | 汇总 token/apply/verdict/lint 前后对比 → `FINAL_REPORT.md` |

## 排版覆盖（`agent/layout_overrides.json`）

`layout_set` 写入、`reconstruct_pdf` 读取；不写 `state.pkl`，因此删 key / 删文件 /
`restore` 都能回滚。

```json
{
  "version": 1,
  "paragraphs": {
    "P05-012": {
      "scale_cap": 0.92,
      "font_scale": 0.95,
      "line_skip": 1.35,
      "box_scale": 1.05,
      "box": [44.0, 500.0, 300.0, 620.0],
      "force_break_after_text": ["（1）"],
      "force_break_after_offset": [23]
    }
  },
  "pages": {"5": {"font_scale": 0.98}},
  "history": [{"ts": "...", "reason": "...", "patch": {...}}]
}
```

| key | 语义 | 注意 |
|---|---|---|
| `scale_cap` | 缩放**上限**：`optimal_scale := min(optimal_scale, cap)` | 只降不升（Typesetting 只从 1.0 递减） |
| `font_scale` | 字号乘数（段落级 × 页级叠加） | 对译文 unicode run 会同时改变字宽 → 触发重排 |
| `line_skip` | 覆盖行距系数（默认 CJK 1.50 / 其它 1.3） | 段落级优先于文档级 |
| `box_scale` | 布局框等比扩缩，锚定**左上角**向右下生长 | 值 > 1 变宽松（可缓解字号塌缩） |
| `box` | 显式 `[x, y, x2, y2]`（PDF 坐标，y 向上） | 会裁剪到页面 cropbox |
| `force_break_after_text` | 在指定子串后强制换行（取最后一次匹配） | 抗译文改动；未命中只记 warning |
| `force_break_after_offset` | 在指定字符偏移处强制换行 | 同上（偏移 = 新行起始位置） |

字段值写 `null` 表示删除该字段。

## 数据文件（都在 `<workdir>/agent/`）

| 文件 | 内容 |
|---|---|
| `document.md` / `anchors.json` / `sheet.jsonl` | 解析产物（锚点协议的真源） |
| `state.pkl` | IR 状态（约 MB 级，勿手改） |
| `translated.md` / `translated.jsonl` | 模型译文 / canonical 译文 |
| `apply_report.json` | 写回报告（violations / repaired / fallback_ids） |
| `review_verdict.json` | 审查结论（blockers / warnings / metrics） |
| `layout_overrides.json` | 排版覆盖（唯一真源，可回滚） |
| `layout_geometry.json` | 每个段落的源框/渲染框/缩放/字号/行数（lint 与 locate 的数据源） |
| `layout_lint.json` / `lint_history.json` | 最近一次 lint 结果 / 历史摘要（报告用） |
| `snapshots/<name>/` | 小文件快照 |
| `usage.json` | 各阶段 token 用量 |

## 二次开发

```python
from babeldoc_tools import registry
registry.load_builtin_tools()
print(registry.get_schema("layout_set"))
registry.dispatch("layout_lint", {"workdir": "tmp/wd", "min_sev": "P1"})
```

新增工具：在 `layout.py` / `translate.py` 等模块里用 `@register("name", ...)` 装饰
`fn(args) -> dict` 即可；注册表、schema 校验、CLI、错误包装都会自动生效。
