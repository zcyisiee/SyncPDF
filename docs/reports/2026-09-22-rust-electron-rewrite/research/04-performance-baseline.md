# 性能基线：现管线量化耗时与 xelatex 贴片路径固定开销

- 日期：2026-09-22
- 用途：为「Rust 自研排版直接改写 PDF 内容流」的重写决策提供量化证据与目标设定依据
- 证据口径分三级，表中每行注明：
  - **[文档]** 仓库内报告/计划/注释中已落盘的实测数字（附路径:行号）；
  - **[注释]** 代码注释中的实测数字（附路径:行号）；
  - **[取证]** 本次调研从本机 `~/.sp` 真实运行归档（`<workdir>/debug/runs/*/events.jsonl`、`app.db`）只读提取的数字（附归档路径与事件名）。取证环境：18 核 Apple Silicon（报告中的本机），MAX_PREVIEW_WORKERS=16。
- 找不到数字的地方明确写「无量化证据」，未做任何外推。

---

## 1. 各阶段耗时总表

### 1.1 端到端分阶段（真实运行取证，`bdt run` 七阶段）

来源：`~/.sp/<doc>/debug/runs/<run>/events.jsonl`，按各 stage 首末事件时间戳差计算（2026-09-22 只读取证）。

| 文档 | 阶段 | 耗时 | 出处 |
|---|---|---|---|
| up-vns（12 页 / 77,897 原生字符 / 翻译范围 169 块） | parse（MinerU 云，缓存未命中） | **63.8s**（其中 MinerU 上传 ~1s + 轮询 37s + 下载/解包 <1s，本地部分 ~26s） | `~/.sp/up-vns-20260921-022426/debug/runs/20260921T022439Z-00015b/events.jsonl`，span 事件 `mineru.request_upload_urls`/`mineru.upload`/`mineru.poll`（02:24:43→02:25:21） |
| 同文档重跑 | parse（MinerU 缓存命中） | **26.1s** | 同目录 `20260921T090434Z-000127/events.jsonl`，`mineru.cache` 命中事件 |
| 同文档 | translate | **218.9s / 241.8s**（两次 run） | 同上两个归档，translate 首末事件 |
| 同文档 | apply | 5.2s / 5.3s | 同上 |
| 同文档 | build（latex bbox 全量，106 段贴片，两遍：首遍 + 缩字段编译后扩框重排） | **54.6s / 52.0s** | 同上 |
| 同文档 | check | **157.6s / 160.5s**（无中间事件，黑盒） | 同上 |
| up-trc（27 页 / 113,424 字符） | parse / translate / apply / build / check | 20.5s / 540.7s / 6.3s / 57.7s / 54.9s | `~/.sp/up-trc-20260919-091110/debug/runs/20260919T143205Z-00018e/events.jsonl` |
| up-2602（58 页 / 103,302 字符 / 356 块） | parse（缓存命中） | **23.9s** | `~/.sp/up-2602-02908v2-20260920-155426/debug/runs/20260920T155429Z-000128/events.jsonl` |
| 同文档 | translate | **4118.9s（68.6 分钟）**——注意这是**失败 run**（job j_01M2ZRDAYWQVB8FF3E5TN4Z54H，总时长 5543s），含提供方失败/重试，不代表健康耗时 | 同上 |
| 同文档 | build | **1377.8s（23 分钟）**——2026-09-20 修复前的代码（批阶梯/页锁修复均在其后的 2026-09-21） | 同上 |

健康样本结论：**12 页文档端到端（parse 命中缓存 → report）约 7.5 分钟，其中 check 160s、build 52s、translate 219–242s 三座大山**。

### 1.2 parse 阶段内部细分（12 页、MinerU 缓存命中，26.1s）[取证]

来源同 `20260921T090434Z-000127/events.jsonl` 的 parse 段事件序列：

| 子步骤 | 耗时 | 事件 |
|---|---|---|
| pdf_prepared → page_frames → native_chars（prepare/页面框/原生字符抽取） | ~4s | `pdf_prepared` 09:04:35.9 → `native_chars` 09:04:39.1 |
| MinerU 布局（缓存命中，纯读盘） | <0.1s | `mineru.cache` span |
| layout_parsed → provider_artifacts / inline_math / ocr_backfill | **~7.4s** | 09:04:39.1 → 09:04:46.6 |
| toc / styles_formulas / paragraphs_found | ~3s | 09:04:46.6 → 09:04:49.7 |
| source_geometry | 0.3s | 09:04:49.7 → 09:04:50.0 |
| links_snapshot（链接快照） | **~9.9s** | 09:04:50.0 → 09:04:59.9 |
| selection → stage_finished | ~1.8s | 09:04:59.9 → 09:05:01.8 |

即缓存命中后 parse 的大头是**本地几何/链接处理（links_snapshot ~10s）与 IR 构建（~7s）**，不是 MinerU。MinerU 云往返只在缓存未命中时发生（12 页实测 ~38s，见上表）。

### 1.3 translate 阶段

| 场景 | 耗时 | 出处 |
|---|---|---|
| 12 页 / 169 块（生产翻译跨度） | 208s（÷~6s/块 ≈ 33 个 TTL 窗口） | `docs/reports/2026-09-21-stream-preview-stale-target-and-title-float.md:39-41` |
| 16 页 / 181 翻译段（agy gemini-3.8-flash-low，5 批 × 40 行） | **~106s** | `experiments/log/2026-09-09-mvp-e2e.md`（「批量：5 批 × 40 行，0 重试 0 回退，~106s」） |
| 58 页 / 356 块（生产节奏回放） | 438s | `docs/reports/2026-09-21-stream-preview-compile.md:37,84` |
| 单块提供方往返 | 无逐块量化证据（只有批量总数） | — |

### 1.4 build（latex 贴片全量编译）

| 场景 | 耗时 | 出处 |
|---|---|---|
| P3 单段渲染时代：三篇论文 LaTeX 额外耗时 | **DeepSeek(220 段) 350s / f1872(159 段) 284s / lawbench 177s** | `.plan/latex-justify/briefs/P4-batching.md:26-29`；`babeldoc/format/pdf/document_il/backend/latex_bbox/stamp_cache.py:4`（「P3 实测三篇 177–350s」）；`renderer_batch.py:2-4` |
| P4 批编译后同三篇 | **21.3s / 19.0s / 11.9s**（缓存二次命中 211/98 段） | `.plan/latex-justify/briefs/P5-regression.md:24-25` |
| 12 页全量 build（两遍，18 次 xelatex，106 段贴片） | 52.0s（xelatex 子进程 18 次 × ~0.9–1.1s，两遍贴片各 ~18s，其余为 typesetting_geometry 等） | [取证] `20260921T090434Z-000127/events.jsonl` build 段（`call_started/call_finished` 各 18 条） |
| 58 页全量 build（修复前代码） | **1377.8s 墙钟；xelatex 985 次调用、累计 1878.5 子进程秒、均值 1.91s/次**；缓存 437 miss / 59 hit | [取证] `20260920T155429Z-000128/events.jsonl`（`call_finished` 985 条）；miss/hit 数字与 `tests/test_latex_renderer_batch.py:637`、`stamp_cache.py:215-217` 注释一致 |
| 一次性全量编译（serve `action=compile` 老路径，W12 时代） | **~3 分钟** | `babeldoc_tools/serve/versions.py:92`（「比一次真 build（~3 分钟）不值一提」） |

### 1.5 流式预览编译（serve 路径）

| 场景 | 耗时 | 出处 |
|---|---|---|
| 58 页 / 356 块，修复前（8 worker、逐档起进程、整块串行） | **1164.9s**；xelatex 1755 次；版面检测 380 次；页合成 p50 36s / 最慢 127s | `docs/reports/2026-09-21-stream-preview-compile.md:45-49,80` |
| 修复后（16 worker、批阶梯、页锁） | **169.2s**；xelatex 692 次；页合成 p50 37s / 最慢 132s | 同上 :83 |
| 按生产节奏回放（翻译 438s） | 编译总墙钟 452.3s；**最后一块译文到达后 14.2s 全部编完**（目标 ≤30s）；55/55 页在翻译结束前发布 | 同上 :37,84,88-93 |
| 同页 21 块的旧串行长队 | 21 × 6s；单块 p50 **4.6s**；页合成最慢 89s / 均值 41s | `ARCHITECTURE.md:61`；`tests/test_serve_stream_preview.py:119` |
| 单块编译成本分解（预览资产膨胀时期，均值 7.9s / 中位 8.5s / 范围 3.5–15.5s；189 次编译） | xelatex **0.5s** + ILTranslator 重建 **0.59s**（其中 FontMapper 0.51s）+ state.pkl 冷读 **0.41s**（46MB）+ 预览合成 **1.31s**（177MB 基底）+ assets.put **0.10s** | `docs/issues/preview-asset-growth.md:140-150` |
| 「预览 `save(garbage=4)`」否决证据 | 体积 177MB→10.3MB 但耗时 1.3s→**30s** | `docs/issues/preview-asset-growth.md:152` |

### 1.6 浮动重识别（本地 PP-DocLayoutV3，`block_compile._float_if_shrunk`）

| 项 | 数字 | 出处 |
|---|---|---|
| 一次检测（渲染位图 + ONNX 推理） | **~0.37s**；缩字块最多付 4 次（三级阶梯各自检测，PageLayoutCache 之前） | `babeldoc_tools/serve/block_compile.py:87-92`（`PageLayoutCache` docstring） |
| 热跑每页检测（ONNX+CoreML） | **~0.1s/页** | `babeldoc/docvision/paddle_layout_regions.py:9`；`docs/reference/pipeline.md:27` |
| ORT 线程夹到 4 的收益 | 16 进程负载下：线程 0（铺满）**0.78s/次** → 线程 4 **0.43s/次**；空载 0.33s | `block_compile.py:44-46`；`ARCHITECTURE.md:71` |
| CoreML 运行期失败降级重建 | 一次失败推理 + 重建引擎 **~6.5s**（并发首检曾各线程白付 5 次） | `paddle_layout_regions.py:136-141`；`tests/test_paddle_layout_regions.py:94` |
| 译文侧二次识别（MinerU 云，供前端识别框） | **无量化证据**（机制上复用上传/轮询链路再传整本译文 mono PDF，build 后可选、不阻断） | `babeldoc_tools/target_layout.py:46-48`；`babeldoc/docvision/mineru_doclayout.py:759-771` |

### 1.7 合成预览 / 导出

| 项 | 数字 | 出处 |
|---|---|---|
| 预览合成（读上一版 177MB + 换 1 页 + save） | 1.31s（历史缺陷期，随体积增长） | `docs/issues/preview-asset-growth.md:147` |
| 页资产（修复后每页独立合成） | 无单页合成计时数字 | — |
| 导出路径（从不可变 baseline 合成 19 页 / 15 页补丁） | **无计时数字**；体积 6.81MB → 23.20MB（14.5×），对象 2718 → 14514（Form XObject 矢量贴片） | `docs/issues/preview-asset-growth.md:94,101` |
| 前端预览刷新有效性 | 单次运行 19 次预览变更间隔：中位 4s / 均值 4.6s / 最长 13s；用户停在第 1 页时仅 **~6%** 的刷新能看到当前页变化 | `docs/issues/preview-asset-growth.md:62,78` |

---

## 2. xelatex 路径的固定开销（重写要消掉的正是这一层）

### 2.1 每块调用几次 xelatex

| 路径 | 次数 | 出处 |
|---|---|---|
| 阶梯候选数（顺序编译最坏值） | **≤15**：源字号+源行距 → 行距 ×1.1 / ×0.9 → 字号 ×0.95^k（≤12 步、下限 4pt、≈0.54×） | `renderer.py:85-98,919-929`（`_SHRINK_FACTOR`/`_MAX_SHRINK_STEPS`/`_MIN_FONT_SIZE`/阶梯构造） |
| 批阶梯（`build_ladder_tex`/`_try_ladder_batch`） | 最坏 **2 次进程**（首选档 1 次 + 其余候选压一次编译，一档一页、`\vsize`=6000bp 常量防分页截断、`@@S/@@E` 标记归属）；快路任一前提不成立（编译失败/标记数或页数不符）返回 None 退回顺序阶梯 | `renderer.py:654-736,738-780`；`ARCHITECTURE.md:69` |
| 全量整档批渲染（`BatchStampRenderer`） | 每轮一份 TeX（每段一页），状态机 源字号+源行距 → 行距±10% → 下扩 → ×0.95，≤3 轮，剩余段回退单段渲染；**编译开销从「段数×进程」降到「块数×进程」** | `renderer_batch.py:1-35` |
| 实测（12 页全量 build） | 18 次 xelatex 调用覆盖 106 段贴片（两遍 build 各约 3 批 + 少量单段回退） | [取证] `20260921T090434Z-000127/events.jsonl` |
| 实测（58 页，修复前） | **985 次**调用 / 437 段缓存 miss | [取证] `20260920T155429Z-000128/events.jsonl` |
| 实测（58 页流式，修复前 → 后） | 1755 次 → 692 次 | `docs/reports/2026-09-21-stream-preview-compile.md:80-84` |

### 2.2 单次 xelatex 的成本构成

| 项 | 数字 | 出处 |
|---|---|---|
| 单跑一次（进程启动 + 导言区加载） | **~0.85s**；排版 15 个候选与 1 个候选几乎等价（**0.83s vs 0.83s**） | `renderer.py:661-662`（`build_ladder_tex` docstring）；`docs/reports/2026-09-21-stream-preview-compile.md:45,102` |
| 16 并发下 | **~1.6s/次**（「剩余成本几乎全是进程启动；再往下只能减少进程数而不是加并发」） | `docs/reports/2026-09-21-stream-preview-compile.md:102` |
| 最小文档（含启动与字体加载） | 0.5s | `docs/issues/preview-asset-growth.md:144` |
| 导言区加载内容（每个贴片进程都要重付） | `fontspec` + `xeCJK` + `amsmath` + `amssymb` + `graphicx` + `babel` + `url` + `xcolor` + `hyperref`（+ `geometry` 纸张设置 + fontspec 按路径加载 Noto/Source Han 四体） | `renderer.py:41-76`（`TEX_COMMON`/`TEX_HEADER`）；`capability.py:27-36`（`REQUIRED_PACKAGES`） |
| 为什么压不掉下限 | **fontspec/xeCJK 不能 `\dump` 预编译格式** | `ARCHITECTURE.md:69` |
| 单位陷阱 | 模板必须用 `bp` 不是 `pt`，否则 455.67pt 页差 0.37% 被拉伸 | `.plan/latex-justify/PLAN.md:20`（根因 7）；`renderer.py:83-84` |

### 2.3 缓存（命中条件与失效面）

| 层 | 键 / 条件 | 出处 |
|---|---|---|
| 进程内 LRU（`BboxStampRenderer._cache`） | `cache_key` = (body 全文, 宽, 高, 字号, 行距, 首行缩进, ascent_top, serif, font_family)；上限 512，超限清空 | `renderer.py:110,312-324,629-632` |
| 持久 `StampCache`（跨文档共享 `<store_base>/cache/stamps`） | key = sha1(命名空间 \| cache_key)；命名空间 = `TEMPLATE_VERSION` + 字体签名（字体/模板一变自然失效）；值 = 单页贴片 PDF + json 元数据 | `stamp_cache.py:63-66,135-137`；`renderer.py:78-80,113-137`；`ARCHITECTURE.md:89` |
| 命中即零成本 | 命中时 `seconds=0`、`compile_attempts=0` | `stamp_cache.py:85-95` |
| 失效面（编辑场景） | 缓存键含 **body 全文**：译文改一个字整段重编；含行内公式的段因融合产物不稳定，「实测该文档 **49%** 的段落持久缓存全失效」 | `renderer.py:313-324`；`tests/test_latex_bbox.py:987` |
| 不共享的代价（历史） | build 阶段只看 workdir 私有缓存时「实测 437 次 cache_miss / 59 次命中」 | `stamp_cache.py:215-217`；`tests/test_latex_renderer_batch.py:637` |

### 2.4 超时与并发上限

| 项 | 值 | 出处 |
|---|---|---|
| 单段 xelatex 超时 | **45.0s**（构造默认，下限夹 5s），超时丢弃该段回退 | `renderer.py:531,541,1138-1140` |
| 批编译超时 | **45s + 0.2s × 段数**（超时按块二分重试隔离坏段） | `renderer_batch.py:27,76`；`.plan/latex-justify/briefs/P4-batching.md:58`；`tests/test_latex_renderer_batch.py:428` |
| `BboxStampRenderer` 默认 worker | 2（`max_workers: int = 2`）；配置侧 `latex_max_compile_workers` = min(cpu,16) | `renderer.py:532`；`.plan/latex-justify/PLAN.md:82`（P1-4） |
| 流式预览 `MAX_PREVIEW_WORKERS` | `max(1, min(16, cpu_count − 2))`（本机 18 核 → 16）；缺省取上限；xelatex 是独立子进程不受 GIL 约束、留 2 核给合成/IO，夹 16 防内存/文件 IO 铺过密 | `babeldoc_tools/serve/limits.py:13-17` |
| 批量块编译 | `MAX_BATCH_WORKERS = 8`，默认 4 | `block_compile.py:32-34` |
| 锁模型 | 共享线程池 + 每页一把锁：xelatex 渲染不持锁（同页可并行），只有浮动规划/占位与 patch 读-改-写提交持页锁 | `stream_preview.py:116-122`；`ARCHITECTURE.md:61` |

---

## 3. 解析阶段开销

### 3.1 MinerU 云 API

| 项 | 值 | 出处 |
|---|---|---|
| 是否上传整个 PDF | 是：>chunk_pages(10) 页的文档切成 10 页分片，一个 batch 一次性提交（≤50 chunks）；每分片 PUT 全量字节 | `mineru_doclayout.py:63-66,72-75,351-405` |
| 轮询间隔 / 超时 | **5.0s / 900s**（`poll_interval_seconds`/`timeout_seconds`） | `mineru_doclayout.py:63,459` |
| 实测往返（12 页，缓存未命中） | 申请上传地址 <0.1s + 上传 ~1s + **轮询 37s** + 下载/解包 <1s ≈ **38s** | [取证] `20260921T022439Z-00015b/events.jsonl`（02:24:43→02:25:21） |
| 缓存 | 按 PDF sha256 键控 `~/.cache/babeldoc/mineru-layout.v1/`（15MB，实测）；命中后 parse 只剩本地 ~26s | `mineru_doclayout.py:738-758`；[取证] du |
| API 参数 | `model_version=vlm`、`enable_formula/table=True`、`language=en`（B-1 后） | `mineru_doclayout.py:351-368` |

### 3.2 本地 Paddle（`docvision/`）

| 项 | 值 | 出处 |
|---|---|---|
| PP-DocLayoutV3 layout 子模型（ONNX + CoreML EP） | **~0.1s/页**（热跑）；输入 144dpi 页面位图 | `paddle_layout_regions.py:9,35`；`docs/reference/pipeline.md:27` |
| 编排内的单次检测成本 | ~0.37s（渲染位图 + 推理），按页缓存（`PageLayoutCache`）后每页只付一次 | `block_compile.py:87-92` |
| ORT 线程 | `intra_op_num_threads=4`（`_LAYOUT_DETECT_THREADS`）；实测见 §1.6 | `block_compile.py:44-46`；`paddle_runtime.py:209` |
| CoreML/CPU 切换 | `BDT_PADDLE_DEVICE`（缺省 auto）；CoreML 运行期失败自动降级 CPU 重试一次并记住；降级重建 ~6.5s | `ARCHITECTURE.md:71`；`paddle_layout_regions.py:52-60,136-141` |
| PaddleOCR-VL 识别（0.9B VLM，MLX 路径） | 模型经 sha256 锁定（`MODEL_LOCK`）；逐 region 串行识别；**每页耗时无量化证据**（C-1 spike 的报告落在 gitignore 的 `tmp/`，未入库；计划里只有「CPU >30s/页 即切 mlx-vlm-server」的阈值） | `paddle_model_lock.py:22-61`；`.plan/parse-quality/briefs/C1-paddle-spike.md:30`；`.plan/parse-quality/PLAN.md:155` |
| ORT profile 无界增长 | `~/.cache/babeldoc/paddle-runtime/` 报告期 117 个 `ort-*.json` / **1.2GB**；[取证] 2026-09-22 已 145 个文件 | `docs/reports/2026-09-21-stream-preview-compile.md:101` |

---

## 4. 进程 / 内存模型

| 层 | 事实 | 出处 |
|---|---|---|
| 进程层级 | `bdt serve`（uvicorn/FastAPI）→ job 子进程 `sys.executable -m babeldoc_tools run`（serve 注入 PYTHONPATH 保证同源码树）→ 每贴片一个 **xelatex 子进程**（16 并发上限）+ job 进程内 ONNX（PP-DocLayoutV3） | `ARCHITECTURE.md:94`；`runner.py:141,156,281-305` |
| GIL 相关 | xelatex 是 CPU 密集独立子进程不受 GIL 约束——这是 worker 上限随核数走的依据；Python 侧共享状态用线程池+页锁 | `limits.py:13-17`；`stream_preview.py:116-122` |
| `state.pkl` 大小 | 实测 12 页 **25MB** / 27 页 **34MB** / 58 页 **40MB**（[取证] `ls -lh ~/.sp/*/agent/state.pkl`）；注释口径 46MB | [取证]；`block_compile.py:22` |
| `state.pkl` 反序列化成本 | 冷读 **~0.6s**（46MB，注释）；实测 0.41s（issue 文档）；按 workdir+mtime 进程内缓存，命中 ≈0.0001s | `block_compile.py:22-26`；`docs/issues/preview-asset-growth.md:146` |
| `ILTranslator` 构造成本 | **~0.5s/次**（FontMapper 扫字体 0.51s）；每块重建 = 356 块白付 **~178 核秒**；并发不持锁时 16 worker 各建一份 **16 × 2.2s** | `block_compile.py:51-55,63-69` |
| `hydrate_parse` 查询 | 每次新建 MetadataDB 0.21s × 512 次 ≈ **109 核秒**（加 5s TTL 缓存前） | `tests/test_serve_stream_preview.py:811`；`block_compile.py:365` |
| 行缓存 TTL | `_ROWS_CACHE_TTL_S = 5.0`（曾造成流式饿死，见 §6） | `block_compile.py:38` |
| 存储 | SQLite WAL（app.db）+ 按内容寻址 assets/；[取证] `~/.sp` 整库 **16GB**（含历史预览资产） | `ARCHITECTURE.md:86-92`；[取证] du |

---

## 5. 依赖体积与外部软件

### 5.1 Python 依赖（`pyproject.toml`）

- 直接依赖 36 项（`pyproject.toml:19-54`）。重量级：`pymupdf>=1.26.7`、`onnxruntime`（+可选 directml/cuda）、`opencv-python-headless`、`scikit-image`、`scikit-learn`、`scipy`、`numpy`、`hyperscan`、`uharfbuzz`、`tiktoken`、`xsdata[lxml,soap]`。
- **torch 未引入**（[取证] `pip list | grep -c ^torch` = 0）。**paddlex 不在主依赖**：手动装入共享 conda env（`pip install -e '.[web]' 'paddlex==3.7.2'`，`docs/guide/cli.md:7,16`）；[取证] env 内有 `paddlex 3.7.2`、无 `paddlepaddle`/`paddleocr`/`mlx`（layout-only ONNX 路径不需要它们）。
- web extra：fastapi/uvicorn/python-multipart（`pyproject.toml:60-65`）。

### 5.2 实测体积（2026-09-22，`du -sh`）

| 项 | 体积 | 备注 |
|---|---|---|
| conda env `bdt` | **1.1GB** | `~/miniconda3/envs/bdt` |
| TeX 发行版 `/usr/local/texlive` | **10GB** | xelatex + 全部宏包；`/Library/TeX` 另有 40K 壳 |
| `~/.cache/babeldoc` 合计 | **2.9GB** | working 1.2G + paddle-runtime **1.2G（ORT profile 膨胀，非模型本体）** + fonts **254M** + paddle-models 124M + models 72M + cache.v1.db 41M + paddle-layout.v2 19M + mineru-layout.v1 15M |
| `~/.sp` 文档库 | **16GB** | 历史资产回收未闭环（`docs/issues/index.md`） |

### 5.3 需要用户安装的外部软件

| 项 | 内容 | 出处 |
|---|---|---|
| TeX 发行版 | xelatex（探测路径 `/Library/TeX/texbin/xelatex`、`/usr/local/bin/xelatex`、`/usr/bin/xelatex`） | `capability.py:39-43,159-168` |
| 宏包 | xeCJK、geometry、fontspec、amsmath、amssymb、graphicx、babel、url（kpsewhich 逐个探测，15s 超时） | `capability.py:27-36,248-266` |
| 字体 | `~/.cache/babeldoc/fonts/`：Source Han Serif/Sans CN（regular+bold）、Noto Serif/Sans 四体（缺拉丁整组退 Latin Modern）；可选族（GoNoto/KleeOne/LXGW 等） | `capability.py:46-94`；[取证] ls |
| MinerU | 云 API token（`MINERU_API_TOKEN`） | `docs/guide/cli.md:77` |
| 模型 | PP-DocLayoutV3（onnx + paddle 三套 repo，sha256 锁定）、PaddleOCR-VL-1.6（识别路径） | `paddle_model_lock.py:6-61` |

---

## 6. 贴片覆盖模型的固有缺陷清单

判定口径：**「自然消失」** = 内容流重写模型下该机制不复存在；**「以新形式出现」** = 问题本质属于排版/PDF 域，换引擎后仍需解决；**「与贴片无关」** = 编排/存储层缺陷，重写时另行处理。

| # | 缺陷 | 证据 | 内容流重写下的命运 |
|---|---|---|---|
| 1 | **缩字不保真**：放不下时字号 ×0.95 阶梯最多 12 步（≈0.54×，下限 4pt），译文比源文小是常态（故障段 4.981 vs 源 7.97pt） | `renderer.py:85-87`；`docs/reports/2026-09-21-next-page-float-blank-home.md:12` | 部分消失：自研排版可真正重排（增行、调行距、跨栏流动），但「空间不够放什么代价」的决策仍要在新引擎里做（缩字号仍会作为兜底存在） |
| 2 | **原位空白**：跨页整框迁移把正文段整块搬走、原位什么都不留（P03-011 落到下一页底 6.5% 处）；四道门禁 + 缺省关闭是补丁 | `docs/reports/2026-09-21-next-page-float-blank-home.md:10-14,41-56` | **自然消失**：整页/整段重排不存在「搬走留空」的补丁几何 |
| 3 | **贴片压字/重叠**：并发浮动互相不可见时 81 对重叠；跨页迁移全部顶对齐到同一位置 | `docs/reports/2026-09-21-stream-preview-compile.md:53-54,80` | **自然消失**（无贴片矩形叠加）；但新排版引擎仍需元素碰撞/净空检测（现在由 PP-DocLayoutV3 + 墨迹兜底承担） |
| 4 | **标题上移 26.6pt**：资格门禁缺失使单行标题被当正文编译，浮动阶梯向上吃净空 | `docs/reports/2026-09-21-stream-preview-stale-target-and-title-float.md:56-59` | **自然消失**（无浮动阶梯）；标题/单行的资格判定仍需保留语义 |
| 5 | **跨页贴片丢失**（20/66）：外来贴片落到已发布页未重合成 | `docs/reports/2026-09-21-stream-preview-compile.md:54`；`tests/test_serve_stream_preview.py:153`（实测 16 处） | **自然消失**（无「已发布页」与外来贴片的概念）；但流式增量更新页面时同样要处理「后到内容改已发布页」的一致性 |
| 6 | **双层文本/选中复制混乱风险**：现靠构造保证（已贴片段落字符不进内容流）+ redaction 物理擦除兜底；历史路径曾靠白矩形假擦除 | `overlay.py:6-18`；`.plan/latex-justify/PLAN.md:138`（P1-5） | **自然消失于单语输出**（内容流重写天然单层）；双语对照 PDF 仍要两层文本，「复制到哪层」的交互问题以新形式出现 |
| 7 | **字体整体替换**：贴片字体恒为 Source Han + Noto，与原文档字体无关 | `.plan/latex-justify/PLAN.md:18`（根因 4）；`renderer.py:199-228` | 以新形式出现：自研排版要么嵌入原字体子集（体积/版权），要么继续替换（一致性问题不变，只是从「贴片突兀」变成「全文统一」——后者其实是改善） |
| 8 | **pt/bp 单位陷阱**：TeX pt ≠ PDF bp，差 0.37% 拉伸 | `.plan/latex-justify/PLAN.md:20`（根因 7）；`renderer.py:83-84` | **自然消失**（自控坐标系）；但新引擎必须一开始就冻结单位约定 |
| 9 | **导出体积膨胀**：Form XObject 贴片使导出 23.2MB vs 输入 1.6MB（14.5×），对象 2718→14514；每页带一份资源 | `docs/issues/preview-asset-growth.md:94,101` | 大幅缓解：直接改写内容流只写真正变化的算子；裁剪公式图片片段若保留仍会引入图像对象 |
| 10 | **预览资产无界增长**：整本预览逐块重存（6.9MB→177MB，376 个/30.3GB），前端 94% 重载无效 | `docs/issues/preview-asset-growth.md:16-18,78` | **可根除**：内容流级增量更新（只写变化页的内容流对象）取代整本重存；但资产去重/回收策略仍需设计 |
| 11 | **缓存按 body 全文键控**：改一字整段重编；含行内公式段 49% 缓存全失效 | `renderer.py:313-324`；`tests/test_latex_bbox.py:987` | 以新形式出现：任何排版缓存都有失效粒度问题；但 ms 级排版使缓存重要性大幅下降 |
| 12 | **text-mismatch 不可编译段**：xeCJK 字符类 + ToUnicode 码点映射差异，7 段 5 种字体组合无解，应用率上限 95.33% | `.plan/latex-justify/briefs/P4-batching.md:37-39`；`P5-regression.md:27-28` | **自然消失**（不再依赖 xelatex 的 ToUnicode）；自研引擎自己写文本编码，但要自己保证「写的字 == 译文文本」（同源校验仍需要） |
| 13 | **两端对齐质量受 TeX 限制**：fill 达标线 0.98、公式密集段 fill 0.069/0.221 | `.plan/latex-justify/PLAN.md:30`（DONE）；`P4-batching.md:41` | 以新形式出现：Rust 排版要自实现 justification/hyphenation/CJK 断行（`XeTeXlinebreakskip` 等语义），这是重写的核心工作量 |
| 14 | **流式/全量双路径漂移**：5s 快照饿死（151 块只成 33）、资格门禁两套（修复后逐块对齐 119/50） | `docs/reports/2026-09-21-stream-preview-stale-target-and-title-float.md:11-13,90-99` | 与贴片无关（编排层缺陷），重写仍会有流式与全量两条路径，必须单一门禁/译文读取源 |
| 15 | **ORT profile 无界增长 1.2GB** | `docs/reports/2026-09-21-stream-preview-compile.md:101` | 与贴片无关；若 Rust 版仍调 ONNX/模型推理需沿用「关 profiling」教训 |
| 16 | **34 个内容级失败**：无法安全生成 LaTeX（16）、单块编译失败（16）、bbox 与相邻块重叠（2） | `docs/reports/2026-09-21-stream-preview-compile.md:99` | 部分消失（LaTeX 转义类失败没有源头）；几何重叠类以新形式出现 |
| 17 | **导出/预览双重合成路径**：preview 从上一版出发（曾无界增长）、export 从不可变 baseline（有界），两套并存易误判 | `docs/issues/preview-asset-growth.md:84-101` | 结构性消失机会：重写可统一为「baseline + 页内容流补丁」一条路径 |

---

## 7. 重写目标设定（从上述数字推导）

| 目标项 | 现状基线 | Rust 自研排版可期目标 | 依据 |
|---|---|---|---|
| 单段排版延迟 | xelatex 单进程 ~0.85s（并发 1.6s）；批编译摊薄后 21.3s/220 段 ≈ 97ms/段（含两遍） | **<1ms/段**（进程内断行，无进程启动/导言区/字体重加载） | §2.2：0.85s 几乎全是启动+导言区；排版本体 15 候选≈1 候选 |
| 12 页全量 build | 52s（两遍、18 次 xelatex） | **<2s** | 去掉 985→18 次进程的极限已在批编译证明（350s→21s），进程归零后剩余为几何/IO |
| 流式单块编译 | p50 4.6s（含 xelatex 0.5–1.6s + 固定成本） | **<100ms**（缓存命中 <1ms） | §1.5 |
| parse 本地部分 | 20–26s（links_snapshot ~10s、IR 构建 ~7s） | 需单独 profile 后定目标；Rust 化 PDF 解析/链接快照通常可 10× | §1.2 |
| MinerU 云往返 | 38s（12 页，轮询 37s @5s 间隔）+ 译文侧再传一次 | 无法靠重写消除（除非换本地模型）；本地 Paddle layout 0.1s/页是替代证据 | §3.1, §3.2 |
| 磁盘/安装面 | conda 1.1GB + TeX 10GB + 缓存 2.9GB | 单二进制 + 嵌入字体子集（百 MB 级）；TeX 依赖完全消失 | §5 |
| 导出体积 | 14.5× 输入（Form XObject） | 目标 <2× 输入 | §6#9 |

### 最慢环节排序（健康 12 页样本，§1.1）

1. check 160s（黑盒，无细分证据——重写前应先插桩弄清）
2. translate 219–242s（外部模型，非本仓库可控）
3. build 52s（其中 xelatex 进程开销约占 2/3）
4. parse 26s（缓存命中后；未命中 +38s 云往返）
5. apply 5s

> 翻译本身（219s）是端到端最大项且不受排版重写影响；重写能直接拿下的是 build（52s→~2s）与流式编译尾延（14.2s→亚秒），并顺带消除 §6 中 1/2/3/4/5/8/12 号缺陷。check 阶段 160s 在调研中没有找到任何细分证据，建议在重写决策前先补一次插桩测量。
