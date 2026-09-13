# PP-DocLayoutV3 布局框查看器

用 **PP-DocLayoutV3**（PaddleOCR-VL 的布局分析模块）识别 PDF 每一页的版面区域，
把 bbox 画到 PDF 上，并提供一个前端页面交互式查看，方便判断 layout 效果。
推理走 **Apple Silicon GPU（CoreML / Metal）**，比 CPU 快约 3.2 倍。

```
paddle_layout_viewer/
├── scripts/
│   ├── prepare_model.py   下载官方 ONNX 权重 + 图手术（去 mask head、修 MaxPool）
│   ├── layout_engine.py   推理引擎：预处理 / 后处理 / 阅读顺序 / CoreML 选择与自检
│   ├── run_layout.py      主流程：PDF → 页面图 + layout.json + boxes_overlay.pdf
│   └── serve.py           静态服务器：/ 前端，/data 本次运行产物
├── viewer/                前端（原生 HTML/CSS/JS，无构建步骤）
├── model/                 权重（gitignore，由 prepare_model.py 生成）
└── requirements.txt
```

产物默认写在仓库的 `tmp/paddle-layout/<pdf文件名>/` 下（`tmp/` 已在 `.gitignore`）。
同一个输出根目录下的多份文档可以在一个前端里切换查看。

---

## 快速开始

```bash
cd paddle_layout_viewer

# 1. 下载权重 + 生成优化后的推理图（约 130 MB，只需一次）
python3 scripts/prepare_model.py --check

# 2. 跑一遍 PDF
python3 scripts/run_layout.py --pdf ../2512.08296v3.pdf
python3 scripts/run_layout.py --pdf ../DeepSeek_V41_Tech_Report.pdf

# 3. 打开查看器（会自动开浏览器）
#    指向【目录】就能在一个前端里切换所有文档：
python3 scripts/serve.py --data ../tmp/paddle-layout

#    也可以只指向单个文档：
python3 scripts/serve.py --data ../tmp/paddle-layout/2512.08296v3
```

常用参数：

```bash
python3 scripts/run_layout.py --pdf X.pdf \
    --pages 1-10            # 只跑前 10 页（1-based，支持 1-3,7,12-）
    --zoom 2.0              # 渲染倍率，检测结果会精确映射回 PDF 坐标
    --threshold 0.6         # 置信度阈值
    --device gpu|cpu|auto   # 默认 auto
    --no-annotate           # 跳过 boxes_overlay.pdf
```

在 `2512.08296v3.pdf`（44 页）与 `DeepSeek_V41_Tech_Report.pdf`（51 页）上的实测结果：

| | 2512.08296v3 | DeepSeek_V41 |
|---|---|---|
| 页数 | 44 | 51 |
| 检测耗时 | **4.6 s（95 ms/页）** | **6.0 s（99 ms/页）** |
| 检出框 | 644（19 类） | 624（19 类） |
| 设备 | `coreml/CPUAndGPU` | `coreml/CPUAndGPU` |

---

## 一个前端看多份文档

`serve.py --data` 指向**目录**时（目录下每个子文件夹各含一份 `layout.json`），
服务器会实时扫描并生成 `/data/index.json`，前端顶部就出现文档切换标签：

```
[ 2512.08296v3.pdf 44p ] [ DeepSeek_V41_Tech_Report.pdf 51p ]
```

- 选中状态存在 `localStorage`，下次打开还是上次那份。
- URL 带 hash，可以直接深链到具体文档和页码：
  `http://127.0.0.1:8770/#doc=DeepSeek_V41_Tech_Report&page=20`
- 切换文档会整体重建页面/覆盖层/图例，**阈值、框外扩、透明度等显示设置会保留**。
- 只有一份文档时标签栏自动隐藏，行为和以前完全一致。
- index.json 每次请求重新扫描，重跑 `run_layout.py` 后刷新页面即可，不用重启服务器。
- `/data/` 下的路径做了越界校验，`../` 出不去。

`data/index.json` 形如：

```json
{ "multi": true, "docs": [
  { "id": "2512.08296v3", "name": "2512.08296v3.pdf", "base": "data/2512.08296v3/",
    "pages": 44, "boxes": 644, "ms_per_page": 95.0, "device": "coreml/CPUAndGPU" }
] }
```
前端只依赖 `base` 拼接 `layout.json` / `pages/*.webp` / `boxes_overlay.pdf`，
所以把产物放到任何静态服务器上都能直接用。

---

## 前端

- **多文档切换**：`--data` 指向输出根目录时，顶部出现文档标签（见上一节）。
- **连续滚动浏览**，页面图懒加载；bbox 覆盖层只对当前可视页构建，滚远了就销毁，
  51 页文档在 DOM 里始终只有两三个覆盖层。
- **鼠标悬停**：框从浅色变深色（填充 `0.07` → `0.28` 透明度，描边 `1px/62%` → `2px/100%`），
  同时**弱化其它框**（降到 38%），并在光标旁浮出信息条：
  类别、score、阅读顺序、框尺寸。
- **框与文字不重叠**：描边用 `outline` + **正 `outline-offset`**（默认 2px，可调 0–8px）
  画在文字 bbox **外面**的空白处，永远不压在字形上；填充用
  `mix-blend-mode: multiply`，深色字形乘完仍是深色，所以被框住的文字照样清晰可读。
- **类别图例**：右下角浮层，同时显示**中文名 + 英文名 + 计数**（`目录 content 2`），
  点击即可隐藏/显示该类别。平时半透明（0.6），鼠标移上去才变实，可用 ▾ 收起。
- 悬停信息条同样中英并列：`目录 content | score 0.972 · 阅读序 #2 · 926×1262 px`；
  “常显标签”开关打出的标签也用中文名。
- 底部工具条：置信度阈值、框外扩距离、填充/悬停透明度、
  阅读顺序徽标、常显标签、隐藏整页大框、下载带框 PDF。

快捷键：`←/→`、`PageUp/PageDown` 翻页；`+/-` 缩放；`o` 阅读顺序；`l` 常显标签。

---

## 类别说明（25 类）

官方类别名有几个用英文看很容易误读，最坑的就是 **`content` = 目录（Table of Contents）**，
**不是**“正文内容”（正文是 `text`）。所以一份报告的目录页被识别成 `content` 是**正确**的，
不是模型错。前端图例和悬停信息条现在都同时显示中文名，就是为了避免这个歧义。

| 英文名 | 中文含义 | | 英文名 | 中文含义 |
|---|---|---|---|---|
| `abstract` | 摘要 | | `header` | 页眉 |
| `algorithm` | 算法 | | `header_image` | 页眉图像 |
| `aside_text` | 侧栏文本 | | `image` | 图像 |
| `chart` | 图表 | | `inline_formula` | 行内公式 |
| **`content`** | **目录** | | `number` | 页码 |
| `display_formula` | 行间公式 | | `paragraph_title` | 段落标题 |
| `doc_title` | 文档标题 | | `reference` | 参考文献 |
| `figure_title` | 图表标题 | | `reference_content` | 参考文献内容 |
| `footer` | 页脚 | | `seal` | 印章 |
| `footer_image` | 页脚图像 | | `table` | 表格 |
| `footnote` | 脚注 | | `text` | 文本（正文） |
| `formula_number` | 公式编号 | | `vertical_text` | 竖版文字 |
| | | | `vision_footnote` | 图注 |

配色按**语义分组**（同类共享色系），`content`（目录）单独用中性石板灰 `#475569`：
它是导航元素而非正文，必须和正文的蓝色、页眉页脚的玫红都区分开。

| 分组 | 颜色系 | 成员 |
|---|---|---|
| 正文类 | 蓝 | `text` `vertical_text` `aside_text` `abstract` `footnote` `vision_footnote` |
| 标题类 | 紫 | `doc_title` `paragraph_title` `figure_title` |
| 目录 | 石板灰 | `content` |
| 页眉页脚页码 | 玫红 | `header` `footer` `number` |
| 参考文献 | 靖蓝 | `reference` `reference_content` |
| 图表媒体 | 绿/青绿 | `image` `chart` `table` `header_image` `footer_image` `seal` |
| 公式与算法 | 橙/棕 | `display_formula` `inline_formula` `formula_number` `algorithm` |

实测：`DeepSeek_V41_Tech_Report.pdf` 第 2/3 页（就是目录页）被正确识别为 `content`
（score 0.972 / 0.974），`Contents` 标题是 `paragraph_title`，页码是 `number`；
而 `2512.08296v3.pdf`（无目录页的 arXiv 论文）检出 0 个 `content`，也是一致的。

---

## 推理管线的三个关键点（都是实测得出，不是猜的）

官方 ONNX 导出有三个不显眼的约定，任何一处弄错都会让结果静默变废：

1. **输入要先除以 255。** deploy 配置里的 `is_scale` 隐含了 `scale=1/255`，
   归一化只写了 `mean=0, std=1, norm_type=none` 容易漏掉。不除的话最高分从
   **0.96 掉到 0.05**，等于一个框都检不出来。
2. **`scale_factor` 是 `[h_scale, w_scale] = [800/H, 800/W]`。** PaddleX 内部存
   `[w, h]`，喂给模型前做了 `[::-1]` 反转。顺序搞反的话框会转 90° 且全部错位。
3. **`fetch_name_0` 是 `(300, 7)`**，每行 = `[class_id, score, x1, y1, x2, y2, order_rank]`，
   一行对应一个 decoder query。第 7 列是模型预测的**阅读顺序**，按它排序即得阅读顺序
   （这 300 行本身不是有序的）。

预处理用 BGR + `INTER_CUBIC` 缩放成 800×800 方形（deploy 配置声明 `keep_ratio: false`，
即不保持长宽比，反向映射由 `scale_factor` 承担）。

正确性校验：把检出的框换算回 PDF 点坐标后，与 PyMuPDF 的文字块逐一对齐，
例如 `header` 框 `(219.3, 54.6, 375.7, 64.8)` vs 真实文字块 `(221.3, 55.7, 373.8, 63.7)`，
页码 `number` 框 `(523.2, 779.3, 534.3, 788.3)` vs `(524.1, 780.4, 532.9, 788.3)`。

---

## Apple Silicon 性能优化

在 M5 Pro、800×800 输入、batch=1 下实测：

| 执行后端 | 每页耗时 | 说明 |
|---|---|---|
| `CPUExecutionProvider`（全核） | ~305 ms | 基线 |
| **`CoreML / MLProgram / CPUAndGPU`** | **~94 ms** | **默认，3.2×** |
| `CoreML / MLProgram / ALL`（含 ANE） | ~99 ms | ANE 反而更慢 |
| `CoreML / MLProgram / CPUOnly` | ~302 ms | 证明前两行确实是 GPU 在算 |
| `CoreML` 默认选项 / `NeuralNetwork` 格式 | — | **数值错误**（Δ≈1400 px），已排除 |

> 绝对数字是**空载机器**上测的；机器有负载时两边都会变慢（实测 load≈11.7 时
> CPU 609 ms / GPU 158 ms）。真正稳定的是**倍率 3.2–3.8×**——而且 CPU 被争抢时
> GPU 的优势反而更大（3.84×）。

做了这几件事才拿到这个速度：

1. **去掉用不到的 mask head。** 原图第三个输出 `fetch_name_2` 是 300×200×200 的实例分割
   mask，由 18 个 `GridSample` 拼出来，既费算力又是 MLProgram 编译不了的算子。
   只保留 bbox 两个输出即可砍掉整支子图。
2. **修 `MaxPool` 的 `ceil_mode`。** 主干第一个 `MaxPool` 同时带 `ceil_mode=1` 和
   `auto_pad=SAME_UPPER`，MLProgram 直接编译失败：*"ceil_mode must be False when pad_type
   is equal to same"*。**编译失败时 onnxruntime 会静默退回 CPU**，模型照样出框，只是完全没有
   GPU 加速——很容易以为"已经在用 GPU 了"。该 pooling 是 stride=1、kernel=2 作用在 400×400
   上，`ceil_mode` 不可能改变结果，所以清零是**可证明无损**的（已比对为逐位一致）。
3. **删掉 Paddle2ONNX 留下的脏 `value_info`。** 约 2800 条中间张量形状里有的首维是错的
   （256）。batch 维是动态时无害，但一旦把 batch 固定成 1 就会触发 shape 合并冲突而加载失败。
4. **用 free-dimension override 把形状钉成静态。** CoreML 遇到无界维度会直接报
   `has unbounded dimension which is not supported`；固定后既避免重复编译也省掉 shape 推断开销。
5. **缓存编译产物**（`ModelCacheDirectory`）：会话加载 9.5 s → **2.2 s**。
6. **GPU 数值自检。** 上面表格里已经出现过"能加载、能出框、但坐标是错的"的配置，
   所以引擎会拿一张合成图比一次 GPU/CPU 的输出，Δ<0.05 才启用 GPU，结论缓存在
   `model/coreml_verify.json`。默认用 CPU 会话兜底。
7. **渲染与推理重叠。** 页面渲染 + WebP 编码在 `ThreadPoolExecutor` 里预取，
   主线程同时跑 GPU 推理。batch 化实测无收益（per-page 耗时不变），故不 batch。

> 注意：本仓库路径含中文（`博0/杂项`），而 CoreML EP 无法从非 ASCII 路径建 model URL
> （报 `Failed to create model URL from path`）。引擎会自动把图复制一份到
> `~/.cache/paddle-layout-viewer/` 再加载，无需手工处理。

---

## 输出格式

`layout.json`：

```json
{
  "meta":  { "pdf": "...", "device": "coreml/CPUAndGPU", "zoom": 2.0,
             "threshold": 0.5, "ms_per_page_median": 94.4, "annotated_pdf": "boxes_overlay.pdf" },
  "labels": [...], "palette": { "text": "#2563eb", ... }, "class_counts": { "text": 202, ... },
  "pages": [ { "index": 0, "page": 1, "width": 1191, "height": 1684, "image": "pages/page-0001.webp",
               "pdf_width": 595.28, "pdf_height": 841.89, "infer_ms": 95.8,
               "boxes": [ { "label": "header", "score": 0.872,
                            "x0": 438.6, "y0": 109.2, "x1": 751.4, "y1": 130.5,
                            "order": 1, "query_rank": 0 } ] } ]
}
```

`boxes` 的坐标是**渲染图像像素**（`width`/`height` 为同一坐标系），除以 `zoom` 即得
PDF 点坐标——`boxes_overlay.pdf` 就是这么画的。`order` 是页面内 1-based 阅读顺序，
`query_rank` 是模型原始输出值。

---

## 已知限制

- **只输出矩形框。** 原模型的第 3 个输出是实例分割 mask，能还原多点多边形（PP-DocLayoutV3
  的卖点之一是斜拍/弯曲文档）。这里为了性能把它们剪掉了，所以对平面电子版 PDF（本工具的目标
  场景）完全够用，但对拍照/弯曲的文档只能给外接矩形。需要多边形的话，把
  `prepare_model.py` 里 `KEEP_OUTPUTS` 加回 `fetch_name_2` 并自行解码 mask 即可。
- **不做 NMS。** RT-DETR 本身 NMS-free，输出是每 query 一个框，实测无重复框。
- 类别名 `LABELS` 硬编码在 `layout_engine.py`，与模型自带的 `inference.yml` 中
  `label_list` 一致（保留该 yml 作溯源用）。
- 标注 PDF 的像素→PDF 坐标映射对 `/Rotate` 非 0 的页面走
  `Matrix(zoom) * page.rotation_matrix` 求逆，未在旋转页面上单独验证过。
