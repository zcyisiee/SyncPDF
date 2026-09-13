# Task: A1 — 修复 PdfCharacter.advance 的 text/device 空间单位不一致

## Objective

`il_creater_active.py` 存入 `PdfCharacter.advance` 的值是 text-space 宽度，而 `box` 是 device-space 坐标；当 PDF 内容流用 `Tf 1` + 大 `Tm` 矩阵（如 `11.12728 0 0 10.9091 Tm`）排版时两者差 ~11 倍，`layout_helper._has_word_gap` 据此在每个字符间假阳性插入空格（`S i n g l e - A g e n t`）。修复后 advance 换算到 device space，逐字符空格消失。

## Context

- 分支 `feature/latex-bbox-layout`。
- 触发条件实测：`测试.pdf` 全部 308 个 `Tf` 都是 `1 Tf`（缩放烘进 Tm 的 a 分量）；`2512.08296v3.pdf` 用正常字号（`10.9091 Tf` + Tm a=1.02）。同内容、不同 PDF 生成器设置 → 一个全坏一个好。
- 坏例数据：`tmp/md-test2/agent/state.pkl`（P01-002 段 'S' 的 box 宽 6.32、advance 0.568，比值 11.127 = |Tm a|）。
- **IL 层约定 advance ∈ device space 的证据**：`typesetting.py:533` 重建字符时 `advance=self.char.advance * scale` 与 `box` 宽 `width*scale` 同乘；`typesetting.py:844` 纯 unicode 单元 `advance=char_width`（与 box 同空间）。
- 消费 advance 的路径：`layout_helper.py:225 _has_word_gap`（`extra = next.box.x - (prev.box.x + prev.advance)`）、`layout_helper.py:269 get_char_unicode_string`、`paragraph_finder.py` 的空格 dummy（用 box 距离，不受影响）。
- 必读：`babeldoc/format/pdf/document_il/frontend/il_creater_active.py:1293-1400`（`project_native_char`）、`babeldoc/format/pdf/new_parser/text_positioning.py`（AWLTChar 构造，`adv = textwidth * fontsize * scaling`，matrix 含 Tm+CTM）、`babeldoc/format/pdf/awlt_char.py:41`。
- 复用：`il_creater_active_support.py:561 get_rotation_angle` 展示了 matrix 的解包方式。

## Deliverables

1. `tests/test_char_advance.py` 新增（测试先行，提交时同一 commit）：
   - 构造水平文本场景：matrix = `(11.12728, 0, 0, 10.9091, x, y)`、`char.adv = 0.568` → 断言存入 IL 的 `advance ≈ 6.32`（0.568 × 11.12728）。
   - 常规场景：matrix = `(1.02, 0, 0, 1, x, y)`、`adv = 6.07` → advance ≈ 6.19。
   - `_has_word_gap` 端到端断言（可选，若构造 PdfCharacter 成本高则用单测直接测换算函数）。
2. `il_creater_active.py` `project_native_char` 修复（约 :1341）：
   ```python
   # advance 换算到 device space：AWLTChar.adv 是 text-space 宽度，
   # char.matrix（Tm×CTM）的水平缩放因子把它映射到与 box 同一空间。
   # Tf 1 型 PDF（缩放烘进 Tm）此前会差 ~11 倍，导致 _has_word_gap 假阳性。
   if char.adv and not char.vertical:
       _a, _b = char.matrix[0], char.matrix[1]
       _sx = math.hypot(_a, _b) if (_a or _b) else 1.0
       advance = char.adv * _sx
   else:
       advance = char.adv
   ```
   （`vertical` 分支：AWLTChar 的 vertical 判定见 `awlt_char.py:64`；竖排文本保持原值即可——`_has_word_gap` 对 `prev.vertical` 直接返回 False，不消费。）
3. `il_creater.py:1026`（旧 frontend，同样 `advance = char.adv`）同步修复，保持两 frontend 一致。

## Constraints

- **不改** `_has_word_gap` / `get_char_unicode_string` 的判定逻辑——它们在 advance 语义正确后自然恢复。
- **不改** `text_positioning.py` 的 `pos_x += item.adv`（text space 内部推进，语义正确）。
- `char.adv` 为 None/0（Type3/CID 缺宽度等）时保持原值，下游已有 fallback。
- 不碰 MinerU/布局相关代码。

## Validation

```bash
# 1. 新测试（修复前应 FAIL，修复后 PASS）
python3 -m pytest tests/test_char_advance.py -q
# 2. 单测基线不变（308 passed / 7 failed 既有）
python3 -m pytest tests/ -q
# 3. 坏例重放（MinerU 缓存命中，无需 token）
python3 -m babeldoc.tools.agent md-extract "测试.pdf" --workdir tmp/a1-verify
#    断言：document.md 无逐字符空格 —— grep -E "([A-Za-z] ){3,}" 应无命中（标题/正常空格除外）
# 4. 好例不变（2512 p7）
python3 -m babeldoc.tools.agent md-extract 2512.08296v3.pdf --workdir tmp/a1-verify-2512 --pages 7
#    断言：document.md 与 tmp/md-2512-p7/agent/document.md 逐字节一致（diff 为空）
```

## Report back

- 修改/新增文件清单与每处改动目的；
- 验证命令完整输出摘要（尤其 3、4 的 diff 结论）；
- 发现并修复的缺陷（若有）；
- 任何偏离 brief 的决定及理由。
