# 流式预览「正文段被搬到下一页、原位留空白」修复报告

- 日期：2026-09-21
- 范围：`bdt serve` 局部编译的浮动阶梯（`block_compile._float_if_shrunk` → `layout_refine`）
- 现场：用户文档 `up-vns-20260921-022426`（12 页），段落 `P03-011`
- 前情：`docs/reports/2026-09-21-stream-preview-stale-target-and-title-float.md`（同一浮动阶梯的另一处缺门禁：标题上移）

## 一句话结论

预览第 3 页 `P03-011` 整块空白的根因是浮动阶梯的**第三级「跨页整框迁移」**：该段因缩字
（`min_font_size` 4.981 vs `src_font_size` 7.97）进入浮动阶梯，同页两级扩框都没找到净空，
第三级把**整段搬到了第 4 页页底**（落点 `y2 = 51.67pt` / 页高 793.7pt，页底 6.5% 处），
第 3 页原位什么都不留。该级缺省关闭并加上四道资格门禁后，`P03-011` 在原位渲染出译文，
`compile_float` 事件消失。

## 用户现象 → 根因对应

| 用户现象 | 根因 |
|---|---|
| 第 3 页 `P03-011` 位置空白 | 跨页整框迁移把贴片搬走、原位留空 |
| 点「编译此块」仍空白 | 同样的阶梯每次编译都走同一条路，结果可复现（`job_events` 里两条完全相同的 `next-page` 记录） |
| 空白处还有别的段落译文，看起来像「渲染丢了」 | 原位确实一点墨迹都没有（不是透明度/颜色问题） |

## 证据链（全部在 `~/.sp`，只读取证）

1. **数据齐全，不是缺译或缺几何**：`GET .../paragraphs?page=3` 里 `P03-011`
   `layout_label=text`、`layout_status=ok`、`target` 有译文，
   `geometry.layout_box = [35.754, 493.0, 290.541, 526.559]`、`n_lines=3`。
2. **CLI build 产物正确**：`output/source.no_watermark.zh.mono.pdf` 第 3 页该区域用
   `pymupdf` clip 取文本可见三行中文——说明原位渲染本身没有问题。
3. **serve 的流式预览产物把它搬到了第 4 页**，`job_events` 两条一模一样的记录：

   ```
   type=compile_float block_id=P03-011
   data={"paragraph_id":"P03-011","kind":"next-page","page":4,
         "box":[35.75,16.34,290.54,51.67]}
   ```

   落到 `box` 的 `y2 = 51.67`（页高 793.7）—— 下一页的**最底部**，不是阅读位置。

## 根因：跨页整框迁移的收益/代价完全不对等

`_float_if_shrunk` 的三级阶梯：

| 级 | 手段 | 判断 |
|---|---|---|
| 1 | 同栏向下/向上扩 | 保留。邻居译文常比源文短，框内确实空出地方，这是正收益。 |
| 2 | 跨栏横向扩 | 保留。同理，横着有净空就吃掉。 |
| 3 | **跨页整框迁移** | 把**正文段整块**搬到下一页 |

第三级要解决的问题只是「字被缩小了」，但代价是：

- **原位被掏空成一块空白**——界面上就是「这段没渲染出来」，用户观感等同于渲染失败；
- **阅读顺序被打断**：正文跑到下一页页底，读者在第 3 页读到一半就断了；
- 落点由纯几何规划给出（`plan_next_page_float` 在下一页 x 范围内自上而下找第一个够高的
  空闲区间），**不校验落点是不是阅读位置**——实测就落在页底 6.5% 处。

所以修法是「关掉默认 + 严格门禁」，而不是「调参」。

## 修复

1. `layout_refine.next_page_float_enabled()` —— 第三级总开关，**缺省关闭**，
   `BDT_NEXT_PAGE_FLOAT=1/true/yes/on` 才开。
2. `layout_refine.next_page_float_eligible()` —— 即便开启，四道门禁必须**同时**满足：

   | # | 门禁 | 判据 | 依据 |
   |---|---|---|---|
   | 1 | 同页确无净空 | 由调用方保证（前两级都失败才会走到这里） | 阶梯顺序即证明 |
   | 2 | 缩字严重 | `scale <= NEXT_PAGE_FLOAT_MAX_SCALE = 0.75` | 缩放阶梯每步 ×0.95（`renderer._SHRINK_FACTOR`），0.75 ≈ 5.6 步 |
   | 3 | 落点在页面上半部 | `landing[3] >= page_height * NEXT_PAGE_FLOAT_TOP_BAND_RATIO(0.5)` | 实测故障落点 51.67/793.7 |
   | 4 | **原位不留空白** | `home_occupied` | 段落版面框互不重叠，本段原位只可能有本段自己的贴片 |

   任一条不合格 → 返回原框、不浮动，**宁可缩字也不留空白**。

3. `block_compile._home_stays_occupied()` —— 第 4 道门禁的判据：按
   `rendered_box / layout_box / src_box`（与 `compose_page_asset` 要擦掉的脚印同口径）
   查原位是否有**别的**段落版面框或**别的**已定贴片覆盖；本段自己的贴片不算（它正在被搬走）。

   这一条在正常文档里几乎恒不成立——要它成立得是「别的贴片恰好压在本段原位上」这种版面
   异常，那时缩字反而是更安全的结果。所以缺省还叠了第 1 条总开关。

4. `layout_refine` 顶部 docstring 更正：横向扩与跨页迁移**都已接入** serve 编排，不再是
   「尚未接入编译编排（是后续任务）」。

**没有**按 `paragraph_id` / 页码硬编码：门禁全部是尺度与位置判据，对全部段落一致生效。

## 验证

### 单元守卫（改前全部失败）

把两份实现回退到 `6d408b32`（只留测试）→ **28 failed, 93 passed**：

- `tests/test_serve_block_compile.py::test_float_to_next_page_keeps_stamp_on_home_by_default`
  —— 缺省时即使规划给了落点也不搬：只渲染一次、贴片在主页、落点页从未合成、
  **不发 `compile_float` 事件**（这是 `P03-011` 的直接回归守卫）；
- `::test_next_page_float_keeps_home_when_home_would_be_blank` —— 开启开关后第 4 道单独不成立仍不搬；
- `::test_home_stays_occupied_detects_neighbour_footprint` —— 第 4 道判据的三类情形
  （只有本段自己的框/贴片 → 假；别的段落框或别的已定贴片压原位 → 真）；
- `::test_next_page_float_moves_stamp_when_gate_passes` —— 四道全过时迁移机制仍可用；
- `::test_float_obstacles_include_settled_sibling_stamps` /
  `::test_float_back_home_erases_old_foreign_stamp` —— 既有机制改显式开门禁后再断言；
- `tests/test_layout_refine.py::TestNextPageFloatGate` —— 门禁纯函数 19 例（开关取值、
  0.75 缩字门槛、**实测故障落点必须被拦**、原位空白、缩放比量不到）。

带修复后 `tests/test_serve_block_compile.py` + `test_layout_refine.py` +
`test_serve_stream_preview.py` **146 passed**；全量 `1456 passed, 1 failed`（唯一失败是
基线就有的 `test_bdt_console_script_is_installed`，环境性、与本任务无关）。

检测器一律是 stub，不依赖 PP-DocLayoutV3 可用。

### 端到端（副本库 `tmp/lib`，`~/.sp` 只读未动）

对 `P03-011` 跑一次单块编译（`tmp/repro_block.py`）：

| | 修复前 | 修复后 |
|---|---|---|
| `compile_float` 事件 | `{"kind":"next-page","page":4,"box":[35.75,16.34,290.54,51.67]}` | 无 |
| `stamp_page` | 4 | 3 |
| 第 3 页 `layout_box` 取文本 | `''`（空） | `'令…= {…, … , …max} 为一组算子，…'`（三行译文） |
| 第 4 页原落点取文本 | 三行译文 | `''` |
| 结论 | `MISSING-ON-HOME` | `PRESENT-ON-HOME` |

同结论在**完整 12 页预览**（`local_previews`）上复现：第 3 页原框有译文、第 4 页落点为空。

- **可重复**：同块再编一次结果一致（`stamp_page=3`、无事件）。
- **第 2 级未受影响**：`P04-010` 仍正常 `widen-right`（横向扩框是本轮明确保留的能力）。
- **整页批量**：第 3 页 25 个块逐个编译，10 个成功块**全部留在第 3 页**，
  `compile_float` 事件 0 条、`next-page` 计数 0。

## 未修的部分（有证据，属既有/内容级问题）

- 第 3 页 25 块里 9 块 `ToolError: 缺少译文或排版数据`（该块译文不在库里，非本轮范围）、
  6 块 `NotReplaced`（标题/单行，按设计保留基线原文）、若干 `单块编译失败`
  —— 与上一份报告的遗留一致，不属跨页浮动的范围。
- 浮动的**同页**两级本轮未动：若未来发现同页扩框也有「啃掉邻居」的问题，应另开一轮。
