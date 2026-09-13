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

---

## 快速开始

```bash
cd paddle_layout_viewer

# 1. 下载权重 + 生成优化后的推理图（约 130 MB，只需一次）
python3 scripts/prepare_model.py --check

# 2. 跑一遍 PDF
python3 scripts/run_layout.py --pdf ../2512.08296v3.pdf

# 3. 打开查看器（会自动开浏览器）
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

在 `2512.08296v3.pdf`（44 页，A4）上的实测结果：

| 指标 | 数值 |
|---|---|
| 检测耗时 | **4.8 s / 44 页（94 ms 每页中位数）** |
| 整个流程（含渲染、WebP、标注 PDF、模型加载） | 8.1 s |
| 检出框 | 644 个，19 个类别 |
| 设备 | `coreml/CPUAndGPU`（GPU 自检 Δ=2.7e-06） |

---

## 前端

- **连续滚动浏览**，页面图懒加载；bbox 覆盖层只对当前可视页构建，滚远了就销毁，
  44 页文档在 DOM 里始终只有两三个覆盖层。
- **鼠标悬停**：框从浅色变深色（填充 `0.07` → `0.28` 透明度，描边 `1px/62%` → `2px/100%`），
  同时**弱化其它框**（降到 38%），并在光标旁浮出信息条：
  类别、score、阅读顺序、框尺寸。
- **框与文字不重叠**：描边用 `outline` + **正 `outline-offset`**（默认 2px，可调 0–8px）
  画在文字 bbox **外面**的空白处，永远不压在字形上；填充用
  `mix-blend-mode: multiply`，深色字形乘完仍是深色，所以被框住的文字照样清晰可读。
- **类别图例**：右下角浮层，点击类别名即可隐藏/显示该类别，带颜色与计数。
  平时半透明（0.6），鼠标移上去才变实，可用 ▾ 收起。
- 底部工具条：置信度阈值、框外扩距离、填充/悬停透明度、
  阅读顺序徽标、常显标签、隐藏整页大框、下载带框 PDF。

快捷键：`←/→`、`PageUp/PageDown` 翻页；`+/-` 缩放；`o` 阅读顺序；`l` 常显标签。

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
