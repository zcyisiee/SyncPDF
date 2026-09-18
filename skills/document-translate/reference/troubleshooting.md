# 故障排查：症状 → 工具 → 参数

按"你看到什么"查表。公开命令用 `bdt`（= `python -m babeldoc_tools`，包位于仓库根）
执行，任意 cwd 均可；未暴露为子命令的确定性工具用内部 Python 函数调用。

---

## 一、翻译内容类

### 1. 某段漏译 / 只翻译了半句

症状：`bdt check` 报 `missing_ids` / `empty_target` / `intra_paragraph_truncated`，
或回译校验（`review.backtranslate_check`）相似度 < 0.55。

```bash
bdt check --workdir <wd>                                            # 拿 blockers/warnings
bdt translate --workdir <wd> --ids P07-012 \
    --feedback "原文后半句 additional task configurations… 未译出，请补全整段"
bdt apply --workdir <wd>                                            # 合并后写回
bdt check --workdir <wd>                                            # 复核
```

- `bdt translate --ids` 只重译合并 `translated.md`，需再 `bdt apply` 写回 IR。
- 想看提示词：整篇预演 `bdt translate --workdir <wd> --prompt-only` → `<wd>/agent/prompt.md`；
  重译运行后提示词落在 `<wd>/agent/prompt.retry.md`（回答写 `translated.retry.md`）。
- 没有 `agy` 时的替代：`bdt translate --workdir <wd> --markdown <外部译文.md>` 导入译文，
  或 `python -c "from babeldoc_tools import review;print(review.backtranslate_check(workdir='<wd>', backtranslation={'P07-012': '...英文回译...'}))"`。

### 2. 术语不统一

症状：同一英文术语出现多个中文译名。

```bash
python -c "import pymupdf,re;t=''.join(p.get_text() for p in pymupdf.open('<mono pdf>'));\
print(len(re.findall('注意力', t)), len(re.findall('自注意力', t)))"
bdt translate --workdir <wd> --ids P03-004,P08-002 \
    --feedback "术语表：attention 统一译作「注意力」，transformer 统一译作「Transformer」；请只改术语，其余保持"
bdt apply --workdir <wd>
```

程序化替换（不改语义、无需模型）：直接改 `agent/translated.jsonl` 的 `target` 再
`bdt apply --workdir <wd>`。

### 3. 占位符/样式锚点被模型改坏

症状：`apply_report.violations` 非空，`ok=false`。

- 看到 `anchor_multiset_mismatch` → 模型丢了或幻觉了锚点；`bdt apply` 会先做
  `proportional` 修复，仍失败才报错；多数错位只是 `anchor_reordered` 警告（合法语序），
  不阻断，但需抽样确认不是跨 span 搬运；
- 看到 `placeholder_lost/hallucinated` → 对相应 id `bdt translate --ids <id>`，feedback 里点名
  缺/多的 token；
- 输出 PDF 文本层出现 `{v1}` / `<style`（`bdt check` 的 `placeholder_leftover`）
  → 说明 target 里带了字面占位符：同样走 `bdt translate --ids <id>`。

### 4. 译文里混进文件头注释

症状：`markdown_comment_leak` blocker / PDF 末页出现 `babeldoc-markdown v1` 文本。

`bdt apply` 已程序化剔除（`markdown_view.strip_html_comments`），重跑一次即可：

```bash
bdt apply --workdir <wd> && bdt build --workdir <wd>
```

### 5. 译文"不完整/句子缺主语"但指标全绿

症状：`bdt check` 报 `formula_splice`（P2），或人工读起来句子不通（例如
"图 5 说明了改变如何影响…" 缺主语、"当设置得过高时" 缺变量）。

根因在**解析层**：公式被抽成 `{vN}` 后，原文文本流在占位符两侧直接相连
（`varying{v9}influences`），英文源文本身就已经破碎。核对方法：

```bash
python -c "import pickle;st=pickle.load(open('<wd>/agent/state.pkl','rb'));\
print(st['inputs']['P09-017'].unicode)"      # 看 source 是否本身就断
```

处理：
- 源文断裂 → 重译也无法修复，列入遗留项（建议在 IL 层合并同一公式的多个 fragment、
  补回词间空格）；
- 源文完整但译文断裂 → 正常走 `bdt translate --ids <id>`。

---

## 二、排版类

### 5. 某段太挤 / 溢出 / 与相邻段重叠

```bash
python -c "from babeldoc_tools import layout;layout.layout_lint(workdir='<wd>', min_sev='P1')"   # out_of_page / paragraph_overlap
python -c "from babeldoc_tools import layout;print(layout.layout_locate(workdir='<wd>', page=5, text='（1）'))"   # 拿 id
bdt layout-set --workdir <wd> \
    --patch '{"paragraphs":{"P05-012":{"scale_cap":0.9}}}' \
    --reason "P1 重叠：与 P05-013 IoU 0.23" \
&& bdt build --workdir <wd> \
&& python -c "from babeldoc_tools import layout;layout.layout_lint(workdir='<wd>', min_sev='P1')"   # 复核
```

### 6. 字号明显小于原文（`font_shrink`）

先看 evidence 的 `font_scale`：

- `font_scale = 1.0` → Typesetting 自动缩放（`optimal_scale` < 1）。**放宽框**通常比继续
  降字号更有效：`{"box_scale": 1.15}`（框锚定左上、向右下生长，不会顶到上一段）；
- `font_scale ≠ 1.0` → 是上一次覆盖造成的，回退该字段：`{"font_scale": null}`。

### 7. 行断得难看（公式列表挤在一行、单字成行）

```bash
bdt layout-set --workdir <wd> \
    --patch '{"paragraphs":{"P02-010":{"force_break_after_text":["。我们假设"]}}}'
```

- 文本锚点优先（抗译文改动）；同一子串多次出现时取**最后一次**匹配；
- 需要精确位置时用 `force_break_after_offset`（偏移 = 新行起始字符位置）；
- 锚点没命中不会报错，只在 `layout_lint` 里出现 `force_break_unresolved`（P2）——
  先在 `layout_geometry.json` 的 `text` 字段确认子串真的存在。

### 8. 段落位置整体不对

```bash
python -c "import json;g=json.load(open('<wd>/agent/layout_geometry.json'));\
print([p for p in g['paragraphs'] if p['id']=='P02-010'][0])"
bdt layout-set --workdir <wd> --patch '{"paragraphs":{"P02-010":{"box":[318,600,562,700]}}}'
```

`box` 是 PDF 坐标（y 向上，原点左下角），超界会被自动裁剪到 cropbox 并在
`ir_overrides.clamped` 计数。

### 9. 文本层出现兼容表意文字（复制/检索乱码）

`text_layer_compat_ideograph`（P2）：字体缺该字形时回退到 CJK 兼容区码位
（如 `了` → U+FA0A），视觉正常但复制会得到兼容码位。不阻断交付；
可用 `unicodedata.normalize("NFKC", ch)` 还原，或用
`python -c "from babeldoc_tools import layout;print(layout.dump_text_layer(pdf='<mono.pdf>'))"`
导出的页首注释查看每页的兼容字清单。需要根治请改 `pdf_creater` 的 ToUnicode 生成
（见 `reference/pipeline.md` 5.2 第 9 条）。

### 10. 链接点不准

`link_misaligned`（P2）：链接矩形下方没有文本块，多为译文重排后引用位置漂移。
记录即可；根治需要按段落新旧 box 仿射映射重定位链接。

---

## 三、流程/环境类

| 症状 | 处理 |
|---|---|
| `workdir_missing` | 先 `bdt parse`（`agent/` 目录必须存在） |
| `mineru_token_missing` | 设 `MINERU_API_TOKEN`，或 `--mineru-json <缓存 layout.json>` / `--mineru-cache-key <sha256>`（本地 ONNX 布局 `layout=native` 已移除） |
| `layout_unsupported` | 布局后端只支持 `mineru` / `paddle` |
| `layout_coverage_gate` | 未命中 layout 区域的原生字符超阈值（默认 0.5%）：看 `<workdir>/<pdf名>/layout_coverage.json` 的逐页明细与 `uncovered_text_preview`，`--layout-coverage-threshold` 调大阈值或重新解析 MinerU 布局 |
| `link_uri_set_mismatch` | mono 输出的 URI 集合 ≠ 源快照（硬阻断）：看 `reconstruct_report.json` 的 `link_uri_set_match`，或 `python experiments/toolchain_gates.py <wd>` 的 `link_integrity.missing_uris` |
| `toc_low_confidence`（警告） | 目录页条目切分置信度 < 0.6：不改结构，只记警告；看 `agent/source/toc.json` 的 `confidence` / `candidate_lines` |
| 目录页被整段翻译 / 条目丢失 | 看 `agent/source/toc.json` 的 `summary.entries` 与 `anchors.json` 的 `toc_entry` 行数；无编号/页码未右对齐导致未切分 |
| 链接矩形漂移 / 点不到 | 看 `reconstruct_report.json` 的 `link_remapped` / `link_fallback_paragraph` / `link_unresolved`；三级回退：字符并集 → 段落投影 → 保持原矩形 |
| `translator_missing` | 未给 `--translator`/`BDT_TRANSLATOR`：用 `--markdown <文件>` 导入译文，或 `--prompt-only` 只取提示词 |
| `translator_failed: Agent execution terminated due to error.` | 被调命令自身失败：翻译命令里指定了当前环境不可用的模型；先 `agy models` 列可用模型，再改 `AGY_MODEL`（如 `claude-sonnet-4-6` 需 `AGY_EFFORT=none`） |
| `translator_empty` | 命令退出码 0 但 stdout 为空：检查 wrapper 的解包（如 agy 的 `response` 字段）是否取到内容 |
| `geometry_missing` | 先 `bdt build`（geometry 由重排阶段 dump） |
| `unmatched_ids` 非空 | 覆盖里的 id 拼错或来自另一次 parse（段落 id 与解析绑定） |
| 想整体回滚 | `bdt layout-set --workdir <wd> --clear`；内容回滚改 `agent/translated.md` 后重新 `bdt apply`（早前的 `snapshot` / `restore` 已随 U2 删除） |
| 重建结果和上次不一致 | 检查是否残留 `layout_overrides.json`；用 `experiments/pdf_fingerprint.py` 比对文本层哈希 |
| 脚本里调 `layout.build_pdf` 报 multiprocessing `bootstrapping phase` 错 | PDF 字体子集化用 spawn 起子进程：脚本入口必须有 `if __name__ == "__main__":` 保护（`python -m babeldoc_tools` 与已有 experiments 脚本都已满足） |
| 审查 agent 需要 grep 文本层 | `python -c "from babeldoc_tools import layout;print(layout.dump_text_layer(pdf='<mono.pdf>', with_spans=True))"` → `output/text_layer/page-XX.txt`（页首注释列出该页兼容表意文字） |
| `invalid_args: 未知参数: mineru_json` | 历史上源于仓库里两个同名 `babeldoc_tools` 包互相遮蔽；现已只剩仓库根一个包（U1 迁移完成）。若仍命中，说明解释器加载了旧安装产物：执行 `uv sync` 刷新 |

## 四、新增门禁与排查入口（板块 1–5）

**一次跑完全部门禁**：

```bash
python experiments/toolchain_gates.py <workdir> --pdf <源pdf> --json
```

五项：`layout_coverage`（硬）、`link_integrity`（URI 集合硬 / unresolved 软）、
`toc_integrity`（硬）、`protected_tokens`（软）、`protocol`（硬）。
任一硬门禁失败 → 退出码 1 + `"ok": false`。

新审计产物速查：

| 文件 | 看什么 |
|---|---|
| `agent/source/mineru/provider_ir.json` | MinerU 结构树；`unknown_types` 非空说明有未识别类型 |
| `agent/source/mineru/alignment.json` | `inline_equation_matched/total`（公式保护覆盖率）、`native_char_coverage` |
| `agent/source/toc.json` | `summary.entries`（条目数）、`confidence`、`low_confidence_pages` |
| `agent/source/bookmarks.json` | 书签条目数（应与源 PDF `get_toc()` 一致） |
| `agent/source/links.json` | 每条链接覆盖的 `char_indices` / `paragraph_ids` |
| `<workdir>/<pdf名>/layout_coverage.json` | `global.uncovered_ratio`、逐页 `uncovered_text_preview` |
| `agent/reconstruct_report.json` | `link_total/remapped/fallback_paragraph/unresolved/uri_set_match` |

**排查入口**见 [运行与验证](../../../docs/guide/cli.md)，阶段产物见 [管线参考](../../../docs/reference/pipeline.md)。

---

## 五、回归自检（改动 Typesetting/重建后必跑）

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests -q \
    --basetemp="tmp/pytest-$(date +%Y%m%d-%H%M%S)"  # 保留每轮证据
bdt build --workdir tmp/md-ccs3764                                   # 空覆盖重建
.venv/bin/python experiments/pdf_fingerprint.py \
    tmp/md-ccs3764/output/<new>.mono.pdf --compare <baseline.pdf>    # 文本层哈希必须一致
python -c "from babeldoc_tools import layout;layout.layout_lint(workdir='tmp/md-ccs3764')"   # 与基线 counts 对比
```
