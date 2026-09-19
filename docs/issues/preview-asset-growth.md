# 预览资产无界增长（流式编译的贴图累加）

- 发现日期：2026-09-19
- 严重级别：高（单文档可占数十 GB；用户可感知的页面反复重载）
- 状态：**未修复**，仅有分析与候选方案
- 证据来源：本机 `tmp/app.db`、`tmp/up-alns-20260919-092753/`、`tmp/up-trc-20260919-054344/` 的真实运行数据，以及 `pymupdf` 复刻实验

## 一句话结论

流式翻译预览每编译一个块，都会**从上一版预览 PDF 出发**、替换其中一页的内容流、再另存为一个全新的预览 PDF。由于保存时不做垃圾回收，每一轮都留下上一版的对象残骸，于是：

- 预览资产体积**随块数单调递增**（实测 6.9 MB → 177 MB），
- 磁盘上**累积数百个**几乎相同的百兆文件（实测 376 个 / 30.3 GB），
- 前端因为 URL 变化而**反复重新下载整个 PDF**。

**但用户最终下载的导出件不在此列**：导出路径每次都从**不可变 baseline** 出发，体积与块数无关，只与总页数成正比。详见下方「两条路径的差别」。

## 现象与数据

### 预览资产按块数递增

`tmp/up-alns-20260919-092753` 一次运行产生的预览资产（按生成顺序）：

| 位置 | 体积 |
|---|---|
| 首个 | 6.9 MB |
| 第 8 个 | 7–9 MB |
| 末尾 8 个 | 172–177 MB |
| 合计（376 个） | **30.3 GB** |

同一时期 `assets` 表按类型（`tmp/app.db`）：

| kind | 数量 | 合计 | 平均 | 最大 |
|---|---|---|---|---|
| `preview` | 376 | 30 308.8 MB | 80.6 MB | 177.2 MB |
| `page` | 376 | 416.3 MB | 1.1 MB | 5.4 MB |
| `baseline` | 4 | 18.1 MB | 4.5 MB | 6.9 MB |
| `stamp` | 367 | 16.7 MB | 0.05 MB | 2.7 MB |
| `export` | 1 | 12.3 MB | 12.3 MB | 12.3 MB |

原文 `source.pdf` 只有 1.6 MB，而单个预览能到 177 MB（**约 110 倍**）。预览类资产的合计比其余所有类型加起来还多两个数量级。

### 累加的是垃圾对象，不是内容嵌套

复刻 `block_compile.py` 的预览合成循环（每轮从上一版出发、只替换一页），对比两种保存方式：

| 轮次 | 普通 `save()` 对象数 | `save(garbage=4)` 对象数 |
|---|---|---|
| v1 | 28 | 21 |
| v8 | 70 | 23 |
| v16 | 118 | 23 |
| v24 | 166 | **23（恒定）** |

结论：膨胀来自**未回收的孤儿对象**，不是把旧内容真的嵌进了新内容。加垃圾回收即可让体积回到常数级——这决定了修复成本很低。

### 触发前端反复重载

`preview_asset` 每编译完一个块就变一次（实测 19 个间隔：中位 4 秒、平均 4.6 秒、最长 13 秒；并行编译时有 0 秒的相邻间隔）。前端把它直接拼进 PDF 的 URL：

```js
// web/src/components/preview/PreviewArea.tsx
const targetUrl = localPreview ? `/api/v1/documents/${did}/assets/${localPreview}` : ...
```

而 `PdfCanvas` 以 URL 作为重挂载 key：

```jsx
// web/src/components/preview/PdfCanvas.tsx
key={`${url}#${pageNumber}#${attempt}`}
```

于是：**块编译完成 → `preview_asset` 变化 → URL 变化 → pdf.js 销毁整个文档并重新拉取 100+ MB**。用户看到的就是「页面疯狂刷新，十几次才有一次内容真的变了」——因为只有正在看的那一页被编译时才看得出差别，其余刷新纯粹是重下同一个大文件。

**量化**：单次运行（job `j_01M2W8XRFYQ1GJFRVX99315P89`，202 个翻译块 / 189 次成功编译）按页统计：第 1 页 11 次、第 2 页 8 次、第 4 页 14 次……用户停在第 1 页时，**只有约 6% 的刷新能看到当前页变化**，其余 94% 是别的页在编译、当前页画布内容其实没变。「十几次才有一次」与这个比例吻合。

补充说明口径：`preview_ready` 在数据库里累计有 376 次，其中约 370 次属于少数几次完整运行，另有若干次是单块手动编译；**单次运行内每个块只编译一次，没有重复编译**（已核实）。

## 两条路径的差别（重要）

项目中存在两个相似但行为不同的合成循环，**不要把两者的结论混为一谈**：

| | 预览路径 `BlockCompiler.compile` | 导出路径 `BlockCompiler.export` |
|---|---|---|
| 起点 | **上一版预览**（`local_previews` 的最新资产） | **不可变 baseline**（`local_pages[page].base` / `output/*.mono.pdf`） |
| 每轮产物 | 新预览 PDF | 新导出 PDF |
| 体积随块数 | **单调递增（无界）** | 有界（∝ 页数） |
| 保留旧对象 | 每轮都留残骸 | 每次从干净 baseline 重建 |
| 用户下载的是 | 否（仅供界面预览） | **是** |

实测导出路径复刻（19 页，贴入 15 页补丁，从 6.81 MB 的 baseline 出发）：导出件 23.20 MB、19 页、14514 对象，体积**不随编译次数增长**。

因此：

- **用户下载的 PDF 不会因为反复编译而无界膨胀**：它会大于输入（该文档 23.2 MB vs 输入 1.6 MB，约 14.5x），但**有上界，与块数/编译次数无关**；
- **磁盘占用与前端重载才是被这个缺陷直接击中的地方**。

关于「导出件为什么仍比输入大」——这是**另一个独立现象**，不属于本缺陷：贴入的页补丁（`page` 类资产）本身合计 28.79 MB，已超过导出件体积；同时对象数从输入的 2718 涨到 14514（内容流被替换为对补丁的引用、每页各自带一份资源）。需要说明的是贴入的是 **Form XObject（矢量）而非位图**：导出件里文字仍可提取、图片对象数为 0，所以它不损失可选中/可搜索性。是否可接受这个倍率需要单独评估，不在本文结论内。

## 根因

`babeldoc_tools/serve/block_compile.py` 的 `compile()` 预览段（`preview_base` 起）：

```python
preview_base = assets.resolve(latest[0]) if latest else outputs[0]
with pymupdf.open(preview_base) as preview_pdf:
    changed_page = preview_pdf[index]
    empty = preview_pdf.get_new_xref()
    preview_pdf.update_object(empty, "<<>>")
    preview_pdf.update_stream(empty, b"")
    changed_page.set_contents(empty)
    changed_page.show_pdf_page(changed_page.rect, composed, 0)
    preview_path = temporary / "preview.pdf"
    preview_pdf.save(preview_path)          # ← 无 garbage / deflate
preview_asset = assets.put(preview_path, kind="preview")
```

三个叠加因素：

1. **起点是上一个预览**：每轮的输入已经含有历史残骸，误差逐轮累积。
2. **`save()` 未做垃圾回收**：孤儿对象全部写入新文件（本仓库多处 `save()` 同款写法，但只有这里因为「输入是上一次输出」而累积）。
3. **每轮都产出完整新资产**：不覆盖、不淘汰，`assets/` 只增不减。

前两个因素让单个文件越来越大，第三个让文件数量与块数同阶。

同文件中的 `export()`（`pdf.save(path)`）与 `composed.save(result_path)` 也没带垃圾回收，但它们**每次从干净 baseline 重建**，所以不累积；`overlay.py`、`renderer_batch.py` 的 `save()` 同理是单次写出。本缺陷的特殊性在于「输入是上一次的输出」这个自反结构。

## 影响面

- **磁盘**：单文档实测 30.3 GB，且随每次重跑继续增长；`bdt serve --cleanup` 只清 `tmp/`、`cache/` 下过期文件，**不碰 `assets/`**，因此这些预览永远不会被自动回收。
- **前端**：每个块触发一次整文档重载与百兆下载（见上）。
- **服务**：每个块要读入上一版百兆预览并写出一份新的，I/O 随运行时长线性变重——这会与「并行编译提速」的目标直接冲突（并行度越高，同时写大文件越多）。
- **导出件**：**不受影响**（有界）。

## 附带发现：为什么单块编译要 8 秒

排查本缺陷时实测了单块编译的成本构成（`render_request` 路径，单次运行 `j_01M2W8XRFYQ1GJFRVX99315P89` 的 189 次编译：均值 7.9s、中位 8.5s、范围 3.5–15.5s；纯串行累加 25.0 分钟）：

| 环节 | 实测 | 说明 |
|---|---|---|
| XeLaTeX 进程一次编译 | **0.5s** | 最小文档实测（含进程启动与字体加载） |
| `ILTranslator` 初始化 | **0.59s** | `render_request` 里**每块都新建**，含 `fontmap` 字体加载（0.51s） |
| `state.pkl` 冷读（46 MB） | 0.41s | 已加缓存，缓存命中后 ≈0.0001s |
| 预览合成（读 177 MB 上一版 + 替换 1 页 + `save`） | **1.31s** | 本缺陷造成，随体积增长 |
| `assets.put`（sha256 + 复制 177 MB） | 0.10s | 本缺陷造成 |

可见 8 秒里 XeLaTeX 本身只占约 0.5 秒，**大部分是每块重复付的固定成本**（`ILTranslator`/字体加载）与本缺陷带来的大文件读写。

**注意**：给预览 `save()` 加 `garbage=4` 虽然能把体积从 177 MB 降到 10.3 MB，但实测**耗时 30 秒**（垃圾回收要遍历全部对象），**不能直接用在流式热路径上**——这条否定了「顺手加个参数」的最小改动方案，方案 A 需要重新评估（例如改用 `deflate=True` 或只在非流式路径回收）。

## 候选修复（未实施，需决策）

按代价从低到高：

**A. 预览保存减小体积（不可直接照搬垃圾回收）**

单纯 `garbage=4` 实测让单块耗时从 1.3s 涨到 30s，**反而更慢**，不能用于流式路径。可行变体：只加 `deflate=True`（压缩内容流，不遍历回收）、或把回收放到翻译结束后的最后一次整理。

**B. 预览改为按页资产（治前端重载）**

服务端 `pages` 表已存有每页的 `page_asset`（平均 1.1 MB）。前端不再订阅会变的整体 `preview_asset`，改为只取当前页；`PdfCanvas` 只在当前页资产变化时重载。这样单块编译只影响一页画布，不再重下整本。

**C. 流式期间不产出整本预览（治本）**

翻译进行中只维护 `page_asset`，完整预览 PDF 留到翻译结束后生成一次。可同时解决膨胀、数量与重载。

**D. 资产淘汰**

为 `preview` 类资产加保留策略（例如只保留最新 N 版），并把 `assets/` 纳入 `--cleanup` 范围。已有 376 个 / 30.3 GB 存量需要一次性清理。

## 复现方式

```bash
# 观察预览资产随块数增长（需先有一次带流式预览的运行）
sqlite3 -readonly tmp/app.db \
  "SELECT kind,COUNT(*),SUM(byte_size) FROM assets GROUP BY kind ORDER BY 3 DESC;"

# 确认导出件来自不可变 baseline（对象数有界）
sqlite3 -readonly tmp/app.db \
  "SELECT p.page,a.byte_size FROM pages p JOIN assets a ON a.sha256=p.page_asset
   WHERE p.document_id='<did>' ORDER BY p.page;"
```

机制验证（不依赖真实运行）：

```python
# 从上一版出发逐轮合成 → 对象数单调增长
# 同一循环加 garbage=4 → 对象数稳定
# 详见本文「累加的是垃圾对象」小节的数据
```

## 相关位置

- `babeldoc_tools/serve/block_compile.py`：`compile()` 预览段（无界增长源）、`export()`（有界参照）
- `babeldoc_tools/serve/asset_store.py`：`put()` 按内容哈希寻址，同内容不重复，但**不同内容照单全收**
- `babeldoc_tools/serve/cleanup.py`：清理范围不含 `assets/`
- `web/src/components/preview/PreviewArea.tsx`、`PdfCanvas.tsx`：URL → 重挂载链路
- `web/src/lib/queries.ts`：`preview_asset` 的取数与失效
