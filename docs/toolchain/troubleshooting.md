# 排查手册

按**症状**组织。每个症状给出：定位命令（产物路径 + grep 建议）→ 常见原因 → 修复动作。

前置：所有命令在仓库根目录执行。`<wd>` 是 workdir。
快速总览：`python experiments/toolchain_gates.py <wd> --json`。

---

## 0. 先看哪里

```bash
python experiments/toolchain_gates.py <wd> --json          # 五门禁聚合
ls <wd>/agent/source/                                      # 注释/目录/对齐审计
ls <wd>/agent/source/mineru/                               # provider IR + 对齐
python -c "import json;r=json.load(open('<wd>/agent/apply_report.json'));print(r['ok'],r['violations'][:5])"
```

产物地图见 [`pipeline-stages.md`](pipeline-stages.md#附产物一览)。

---

## 1. 目录乱序 / 条目丢失 / 目录页被整段翻译

**症状**：译文的目录页是一个大段落，点引导线与页码混进正文；或条目顺序错乱。

**定位**

```bash
python - <<'EOF'
import json
toc = json.load(open('<wd>/agent/source/toc.json'))
print('summary:', toc['summary'])
for p in toc['pages']:
    print('page', p['page_index'], 'conf', p['confidence'],
          'entries', len(p['entries']), 'low_conf', p['low_confidence'],
          'title', p['title_heading'])
EOF
grep -c 'label=toc_entry' <wd>/agent/document.md      # 应为目录条目数（DeepSeek 54）
```

**常见原因与修复**

| 原因 | 判据 | 修复 |
|---|---|---|
| 阈值未达（条目行占比 < 0.6 或条数 < 5） | `toc.json.pages[*].confidence < 0.6`、`low_confidence_pages` 非空 | 该页是边界情形（半页目录）；暂不支持条目化，接受或人工核对 |
| 页码未右对齐（超出 8pt 容差） | `candidate_lines` 明显小于目视条目数 | 检查源 PDF 排版；调试 `split_toc_line(chars, right_margin)` |
| 标题行不含字母（纯数字/符号页） | `split_toc_line` 返回 None | 正常拒绝，避免误伤 |
| 页面被判为非目录页 | `toc.json.pages` 里没有该页 | 页标题不在 `Contents/Table of Contents/目录` 且条目占比不足；确认是否真目录页 |

**注意**：目录条目化**只替换「整段所有行都是目录条目」的段落**；
混合内容段落不动并记入 `summary.skipped_pages`。

---

## 2. 链接丢失 / 矩形漂移

**症状**：mono PDF 里 URL/引用号链接点不到，或点击区域盖住无关文字。

**定位**

```bash
python - <<'EOF'
import json
r = json.load(open('<wd>/agent/reconstruct_report.json'))
print('total', r['link_total'], 'remapped', r['link_remapped'],
      'fallback_paragraph', r['link_fallback_paragraph'],
      'uri_match', r['link_uri_set_match'], 'unresolved', len(r['link_unresolved']))
for item in r['link_unresolved'][:5]: print(' ', item)
EOF
# 源快照里某条链接覆盖了哪些字符/段落：
python - <<'EOF'
import json
links = json.load(open('<wd>/agent/source/links.json'))
for entry in links.get('0', [])[:3]:
    print(entry['kind'], entry.get('uri'), 'chars', len(entry['char_indices']),
          'paras', entry['paragraph_ids'], 'ratio', entry.get('src_rect_ratio'))
EOF
```

**常见原因与修复**

| 原因 | 判据 | 修复 |
|---|---|---|
| 链接矩形盖的是纯图形（无文字） | `char_indices == []` 且 `paragraph_ids == []` | 计入 `unresolved`，**保持原矩形**——这是正确行为（它本来不指向文字） |
| 所在段被翻译重排 | `paragraph_ids` 非空但源字符已不在 composition | 走「段落 box + 段内相对投影」（已实现）；矩形会比字符并集粗 |
| URI 集合不一致（硬失败） | `link_uri_set_mismatch (mono): …` | 看 `missing_uris` / `extra_uris`；确认不是删页模式（`only_include_translated_page`） |
| dual 里链接全丢 | `_copy_page_links_to_dual` 需等所有页建完再插链接（GOTO 目标页存在性） | 已实现；若是自研调用，按该顺序 |

**核对文本层**（视觉结论必须回文本层复核）：

```bash
python - <<'EOF'
import pymupdf
doc = pymupdf.open('<wd>/output/<name>.mono.pdf')
for page in doc:
    for link in page.get_links():
        if link.get('uri'):
            print(page.number, link['uri'], '→', repr(page.get_text('text', clip=link['from'])[:40]))
            break
EOF
```

---

## 3. 漏译（覆盖率门禁触发）

**症状**：解析直接失败，报 `layout_coverage_gate: …`。

**定位**

```bash
python - <<'EOF'
import json, glob
path = glob.glob('<wd>/*/layout_coverage.json')[0]
cov = json.load(open(path))
print('global:', cov['global'], 'threshold', cov['threshold'], 'passed', cov['passed'])
for page in cov['pages']:
    if page['uncovered_chars']:
        print('page', page['page_index'], 'uncovered', page['uncovered_chars'],
              'preview', repr(page['uncovered_text_preview'][:120]))
        print('   samples', page['uncovered_samples'][:2])
EOF
```

**常见原因与修复**

| 原因 | 判据 | 修复 |
|---|---|---|
| MinerU 漏检区域 | 未覆盖文字是正常正文 | 清缓存重跑 MinerU（`MINERU_API_TOKEN`），或升级模型版本 |
| 装饰性文字流/水印 | 未覆盖文字重复、无意义 | 调大阈值 `--layout-coverage-threshold 0.02` |
| 扫描件/异形版式 | `total_chars` 很小或为 0 | 分母为 0 时本就不参与门禁；确认是否该走 OCR 路线 |
| 样本页范围裁剪错位 | `--pages` 与 `mineru_json` 不是同一文档 | 用同一 PDF 生成的缓存回放 |

> 覆盖率审计产物**永远落盘**（即使过门禁），所以「没失败但想确认」也能直接看。

---

## 4. 公式被翻译（inline_math 未对齐）

**症状**：译文里出现被翻译的数学记号，或公式位置错乱。

**定位**

```bash
python - <<'EOF'
import json
a = json.load(open('<wd>/agent/source/mineru/alignment.json'))
s = a['summary']
print('inline_equation_matched', s['inline_equation_matched'], '/', s['inline_equation_total'],
      'protected', s['protected_inline_math'], 'char_coverage', s['native_char_coverage'],
      'span_text_mismatch', s['span_text_mismatch'])
unmatched = [(p['page_index'], m['span_id'])
             for p in a['pages'] for m in p['inline_equation'] if not m['matched']]
print('unmatched inline spans:', len(unmatched), unmatched[:10])
EOF
# 该段是否已变成占位符：
grep -o "id=P09-006[^>]*>" <wd>/agent/document.md
sed -n "/id=P09-006/,+2p" <wd>/agent/document.md
```

**常见原因与修复**

| 原因 | 判据 | 修复 |
|---|---|---|
| span 落在 `code_body` 图内（无原生字符） | unmatched span 的 block 类型是 `code_body` | **正常**：代码块本就不翻译 |
| span bbox 与字符 bbox 无交（坐标/页高换算） | `native_char_coverage` 异常低 | 检查页高换算（`y'=H-y`，`H` 取 cropbox 高度） |
| `provider_ir.json` 缺失 | `alignment.json` 不存在 | 确认走的是 MinerU 布局路径（`--layout mineru`） |
| 未对齐 span 但公式仍被翻译 | 该段没有 `{vN}` | 该公式走的是**字体启发式**（非 MinerU span）；用 `span_text_samples` 对照 |

**注**：`inline_equation` 保护是**追加 formula 布局区域**实现的
（`InlineMathProtector`）；`ParagraphFinder` 会重写 `char.formula_layout_id`，
所以不能直接设该字段。

---

## 5. 标题字号偏小

**症状**：译文标题明显小于原文（如 `I. 引言` 变小字号）。

**定位**

```bash
python - <<'EOF'
import json
a = json.load(open('<wd>/agent/anchors.json'))
print([r['id'] for r in a['rows'] if r['layout_label'] in ('title','doc_title','paragraph_title')][:5])
EOF
grep -n "font_size" <wd>/agent/layout_geometry.json | head
```

**原因**：IEEE/ACM 标题用小型大写字母排版（首字母 ~10pt、其余 ~8pt 混排），
解析器按多数字符取段级 `pdf_style` 时会选中偏小字号。

**修复**：`workflow._bump_title_font_size` 已把标题段段级字号提到段落内最大 run 字号。
若仍偏小：用 `layout_patch` 调该段 `font_scale` →
`reconstruct_pdf` 复核（≤2 轮）。

---

## 6. 圈号退化 / 漂浮圆圈

**症状**：译文里圈号（①②③）变成普通数字，或页面上留着一堆漂浮小圆圈。

**定位**

```bash
python -c "
import json; g=json.load(open('<wd>/agent/layout_geometry.json'))
print('warnings:', g.get('warnings')[:10])"
# 文本层核对圈号是否保留：
python - <<'EOF'
import pymupdf
doc = pymupdf.open('<wd>/output/<name>.mono.pdf')
t = doc[1].get_text('text')
import re
print('circled chars:', re.findall(r'[\u2460-\u24ff\u2776-\u2793]', t)[:20])
EOF
```

**原因与边界**（`EnclosedMarkerFixer`）

| 边界 | 说明 |
|---|---|
| 只处理「单个字母/数字」 | 圈内多字符（⑩、㉑）暂不处理 |
| 只处理闭合曲线图形 | 下划线、方框、高亮等装饰性矢量仍会漂浮（影响面更广，未纳入） |
| 样式统一为空心圈号 | 不保留原实心/圆角样式 |
| NFKC 归一 | `layout_helper._nfkc_preserving_enclosed` 用私有区哨兵保护圈号，避免 `①→1` |

**修复**：确认 `config.fix_enclosed_markers` 未被关闭（默认 True）；
残余问题按边界说明单独评估。

---

## 7. 参考区/作者区被翻译（或该翻译的被跳过）

**定位**

```bash
python - <<'EOF'
import json
a = json.load(open('<wd>/agent/anchors.json'))
from collections import Counter
print('translated:', Counter(r['layout_label'] for r in a['rows']))
print('skipped reasons:', Counter(s['reason'] for s in a['skipped']))
EOF
```

| 症状 | 原因 | 修复 |
|---|---|---|
| 参考文献被翻译 | 参考文献区靠 `references_heading` 信号阻断；若标题未被识别为 heading，阻断不生效 | 用 `--skip-labels reference_content` 追加；或修 `_REFERENCE_HEADING_RE` |
| 作者区被翻译 | `author` 是首页启发式（标题与 Abstract 之间） | 用 `--skip-labels author`；或检查 `_first_page_author_band` 判据 |
| 图注被跳过（应翻译） | 图注落进受保护几何区域 | `CAPTION_LABELS` 已优先翻译；确认标签映射（`image_caption` → `figure_caption`） |

---

## 8. 协议违规（apply 失败）

见 [`gates.md`](gates.md#5-翻译协议门禁硬)。要点：

- `extra_ids`（模型伪造段落 id）→ 从译文里删掉该段；
- `anchor_multiset_mismatch` → 锚点数量/多重集与源文不一致（丢或幻觉锚点）；
  先由 `proportional` 自动修复，仍失败则
  `retranslate_ids --ids [...] --feedback "锚点必须与源文一致"`。锚点顺序与源文
  不同**不算违规**（只记 `anchor_reordered` 警告），因为中英语序调整是合法翻译。
- `missing_ids` **不阻断**（回退原文），但会进 `fallback_ids`——若大量出现，
  说明模型漏行，重译。

---

## 10. 工具层调用失败（`invalid_args: 未知参数`）

**症状**：在仓库根执行 `python -m babeldoc_tools call parse_document --args-json
'{"pdf": …, "mineru_json": …}'` 报 `invalid_args: 未知参数: mineru_json`。

**原因**：仓库里有两个同名包 `babeldoc_tools`：

| 包位置 | 定位 | 是否支持 MinerU |
|---|---|---|
| `babeldoc_tools/`（仓库根） | MAS 版（`babeldoc_core.DocumentJob` 文本管线） | ✗ |
| `skills/document-translate/tools/babeldoc_tools/` | agent 版（MinerU/Markdown 管线） | ✓ |

Python 把 **cwd 排在 `PYTHONPATH` 之前**，所以在仓库根跑 `python -m babeldoc_tools`
必然命中 MAS 版；`skills/.../bin/bdt` shim 虽预置了 `PYTHONPATH`，但 cwd 在仓库根时
仍会被遮蔽。

**验证**

```bash
# 在仓库根：命中 MAS 版（props 无 mineru_json）
python -m babeldoc_tools schema parse_document

# 在非仓库根 cwd：命中 agent 版（props 含 layout/mineru_json）
cd skills/document-translate/tools && python -m babeldoc_tools schema parse_document
```

**修复**

- 需要 MinerU/新产物时，用 **legacy CLI**（cwd 无关）：
  `python -m babeldoc.tools.agent md-extract …`；
- 或用 agent 版工具层：在非仓库根 cwd 下执行
  `skills/document-translate/tools/bin/bdt call …`（或 `cd skills/document-translate/tools`）。

---

## 11. 一页的完整排查流程（模板）

```bash
WD=<wd>; PDF=DeepSeek_V41_Tech_Report.pdf
# ① 门禁总览
python experiments/toolchain_gates.py $WD --pdf $PDF --json
# ② 覆盖率
python -c "import json,glob;c=json.load(open(glob.glob('$WD/*/layout_coverage.json')[0]));print(c['global'],c['passed'])"
# ③ 目录
python -c "import json;print(json.load(open('$WD/agent/source/toc.json'))['summary'])"
# ④ 对齐
python -c "import json;print(json.load(open('$WD/agent/source/mineru/alignment.json'))['summary'])"
# ⑤ 链接
python -c "import json;r=json.load(open('$WD/agent/reconstruct_report.json'));print(r['link_total'],r['link_remapped'],len(r['link_unresolved']),r['link_uri_set_match'])"
# ⑥ 文本层指纹（回归对比）
python experiments/pdf_fingerprint.py $WD/output/*.mono.pdf
```
