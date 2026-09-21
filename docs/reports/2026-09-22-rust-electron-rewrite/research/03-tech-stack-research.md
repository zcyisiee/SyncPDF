# PDF 保版式翻译桌面应用重写：Rust + Electron 技术栈调研报告

- **调研日期**：2026-09-22。除特别注明外，所有版本号、发布日期、许可证、维护状态均于 **2026-09-22 当日**经 crates.io API / GitHub API / npm / 官方文档直接核实，每条附 URL。
- **目标架构**：Rust 引擎（解析 PDF → ONNX 布局识别 → 段落 → LLM 翻译 → 自研排版 → 改写 PDF 内容流输出）+ Electron 前端（VSCode 整体布局与样式）。
- **结构**：A. Rust PDF 读写栈；B. Rust ML 推理；C. Rust 服务/基础设施；D. Electron 前端；E. 同类开源项目参考；文末为「推荐技术栈一览表」。

---

## A. Rust PDF 读写栈

### A1. 解析 / 渲染库对比（核实日期：2026-09-22）

| crate | 最新版本（发布日期） | 许可证 | 维护状态 | 字形级几何（bbox/字体/字号/矩阵） | 渲染位图 | 加密/损坏 PDF | 结论 |
|---|---|---|---|---|---|---|---|
| **pdfium-render** | 0.9.4（2026-09-06） | MIT OR Apache-2.0 | 活跃：0.9.0→0.9.4 半年 5 版，总下载 243 万 | **能**：`PdfPageTextChar` 字符级 bbox/字体/字号；`PdfPageTextObject` 文本矩阵与 scaled/unscaled 字号；`PdfFontGlyphs` 字形几何；0.8.31 重写 `chars_for_object()` 解决了字符边界问题 | **能**：`render_with_config()` / `render_into_bitmap()` → RGBA | PDFium（Chrome 同源内核）：带密码加载、容错业界最强 | **推荐（主力）** |
| **hayro**（+hayro-syntax 0.7.2 / hayro-interpret） | 0.7.1（2026-06-05；2025-07 首发，至今 8 版） | Apache-2.0 OR MIT | 活跃但**官方自述 experimental / WIP**，性能未优化，MSRV 1.92；1400+ 回归测试（抓自 PDFBox/pdf.js 套件），自称最完整的纯 Rust 渲染器 | **能（接口最干净）**：`hayro-interpret` 的 `Device` trait 按**每字形**回调 `draw_glyph(&Glyph, transform, glyph_transform, paint, mode)`，另有 path/image/clip/透明组/marked-content 回调 | **能**：渲到 PNG 位图；hayro-svg 出 SVG | README 未提加密；**不支持 knockout groups 与非嵌入 CID 字体的渲染** | 备选（二线/对照实现） |
| **mupdf**（原 mupdf-rs，crates.io 名 `mupdf`） | 0.8.0（2026-06-22，+mupdf-sys 0.8.0） | **AGPL-3.0**（GitHub API 核实） | 活跃（2026-09-14 仍有 push） | 能（MuPDF `stext` 字符 bbox/字体） | 能（`pixmap`） | 能，强 | **否决**：AGPL 传染，闭源商用需 Artifex 商业许可 |
| **pdf**（pdf-rs） | 0.10.0（2026-03-02） | MIT | 活跃（851 commits / ~1.7k stars；注意其 PR 政策拒绝 AI 代写代码） | **部分**：`pdf::content` 的 `Op` 枚举 + `parse_ops`/`serialize_ops` 逐算子解析（含 `TextMode`/`TextDrawAdjusted`、`FormXObject`、`Matrix`）；字体度量需自己从字体程序算，文档率仅 8.38% | **否**（渲染在单独 pdf_render 仓库，基于 Pathfinder，不成熟） | `crypt` 模块支持解密；损坏文件容错一般 | 不单独作主力；**其 `Op` 枚举是内容流过滤器的现成参考** |
| **lopdf** | 0.45.0（2026-09-08） | MIT | **已复活且活跃**：0.20→0.34 曾多年断更，2024-09 起新维护者（williamdes 等）接手，2026 年 6/7/9 月连发 0.43/0.44/0.45（0.45 含约 30 个 PR），2026-09-12 仍有 push | 否（DOM 层，不解码文本语义） | 否 | 对象层可用；加密支持有限、损坏文件脆弱 | 备选（对象级原地改） |
| **pdf-extract** | 0.12.1（2026-09-16） | MIT | 维护中（2025-2026 持续小版本） | 弱（纯文本抽取为主，非布局引擎） | 否 | 一般 | **否决**：无几何/渲染 |

URL：
- pdfium-render：https://crates.io/crates/pdfium-render 、https://github.com/ajrcarey/pdfium-render 、https://docs.rs/pdfium-render/latest/pdfium_render/
- hayro：https://crates.io/crates/hayro 、https://crates.io/crates/hayro-syntax 、https://github.com/LaurenzV/hayro 、https://docs.rs/hayro-interpret/latest/hayro_interpret/trait.Device.html
- mupdf：https://crates.io/crates/mupdf 、https://github.com/messense/mupdf-rs （license=AGPL-3.0，GitHub API 核实）
- pdf：https://crates.io/crates/pdf 、https://github.com/pdf-rs/pdf 、https://docs.rs/pdf/latest/pdf/content/index.html
- lopdf：https://crates.io/crates/lopdf 、https://api.github.com/repos/J-F-Liu/lopdf 、https://github.com/J-F-Liu/lopdf/releases
- pdf-extract：https://crates.io/crates/pdf-extract

**pdfium-render 两个必须注意的点**：
1. **不随 crate 分发 PDFium dylib**：需自带 bblanchon/pdfium-binaries（https://github.com/bblanchon/pdfium-binaries ），或用 `static` feature 静态链接（`PDFIUM_STATIC_LIB_PATH`）。
2. **线程安全**：`thread_safe` feature 用全局互斥锁串行化全部调用——页级并行要么接受串行、要么自管多实例/多进程。与 C11 的并发设计要协调（pdfium 调用段收敛到专用队列，推理/排版段照常并行）。

补充观察（E 组发现）：纯 Rust 新引擎 `pdf_oxide` 0.3.78（2026-09-08，1042 stars，增长极快，MIT/Apache，自称 3830 个 PDF 100% 解析通过率、20 语言绑定），偏读取/提取，编辑能力深度待验证——值得列入观察名单。https://github.com/yfedoseev/pdf_oxide

### A2. 写入 / 改写库与「内容流改写」实现路径（核实日期：2026-09-22）

| crate | 最新版本（发布日期） | 定位 | 关键能力 | 结论 |
|---|---|---|---|---|
| **pdf-writer** | 0.15.0（2026-05-27） | typst org 低层增量 writer | `writers` 模块确认含 **Type0Font、CidFont（含 CIDFontType2）、Widths（W 数组）、FontDescriptor、Cmap（ToUnicode）**、Encoding/Differences、WMode；0.15 加 `Settings` 与 xref-stream 输出 | **推荐（回写主力）** |
| **lopdf** | 0.45.0（2026-09-08） | 对象级 DOM 增删改 | 直接改既有文档对象/内容流/保存；写新内容的排版辅助少 | 备选（原地小改对象字典时用） |
| **krilla** | 0.8.2（2026-06-04） | 高层「创建」库（基于 pdf-writer）；作者 Laurenz Stampfl（typst 生态 / pdf-writer 贡献者），crates.io repository 指向 LaurenzV/krilla（444 stars，2026-09-20 仍有 push，Apache-2.0） | **只创建、不改已有 PDF**（README 明说改 PDF 请用 pdf-rs）；CFF/TTF 字体子集化极佳；v0.5 起支持**嵌入已有 PDF 页面**；PDF/A-1..A-4；210+ 视觉回归测试 | 对「原地改写」**否决**；但其子集化与「嵌入 PDF 页」代码是重要参考 |

URL：https://crates.io/crates/pdf-writer 、https://github.com/typst/pdf-writer/releases 、https://docs.rs/pdf-writer/latest/pdf_writer/writers/index.html ；https://crates.io/crates/krilla 、https://github.com/LaurenzV/krilla/releases

typst 现状佐证：typst 0.15.1（2026-07-17）的 `typst-pdf` 自 0.13（2025-03）起底层改用 krilla（krilla 又基于 pdf-writer），弃用直接拼 pdf-writer 的旧路径——即 **typst 官方输出栈 = krilla + pdf-writer**，本项目的「新建内容流 + 子集嵌字」直接对齐该栈。https://github.com/typst/typst/releases

**「保留非文字内容 + 删指定文本 + 加新文字 + 嵌子集字体」推荐路径（最省事组合）**：

```text
pdfium-render（读：字符级几何 / 页面位图 / 解密）
  → 自写内容流 Op 过滤器（参考 pdf-rs 的 Op 枚举；递归进 Form XObject，累积 CTM）
      · 命中待译文本的 Tf/Tj/TJ/'/" 等文字算子丢弃，其余算子（图像/路径/gs/cm…）原样重放
  → 自研排版（skrifa 度量 + harfrust 塑形 + icu_segmenter 断行禁则）
  → pdf-writer 追加输出：
      Type0Font + CIDFontType2 + Identity-H + W 数组 + ToUnicode CMap
  → subsetter 子集化嵌入
  → 对象级合并：新字体对象挂进页面 Resources（lopdf 或 pdf-writer 保存）
```

- **Form XObject 递归（所有路线共有的坑）**：文本算子可能藏在任意深度的 Form XObject 里，过滤器必须**递归遍历 XObject 树并累积 CTM**，只处理顶层页面流会漏文本。Python 侧 pdf2zh 的做法是递归解释后把 XObject **拍平内联**进页面流（`q {ops_base} Q {a b c d e f} cm {ops_new}`），并把新字体键写进 Resources/Font 与 XObject 资源字典——Rust 版引擎应将此行为作为对齐基准（详见 E16-2 源码分析）。
- **降级模式**（更简单但保真度低）：krilla v0.5+ 的「嵌入已有 PDF 页面」= 整页原样 + 上面叠译文（**无法删原文，只能遮盖**）；hayro 仓库的内部 crate hayro-write（"convert PDF pages into XObjects or a new page via pdf-writer"，https://github.com/LaurenzV/hayro/tree/main/hayro-write ）就是这个思路的现成参考。

引用：https://github.com/funstory-ai/BabelDOC 、https://github.com/PDFMathTranslate/PDFMathTranslate （2026-09-22 核实）

### A3. 字体栈（CJK + 拉丁混排：度量 / 塑形 / 子集化）（核实日期：2026-09-22）

| crate | 最新版本（日期） | 状态 | 在本项目中的角色 |
|---|---|---|---|
| **skrifa**（read-fonts 0.44.0） | 0.47.0（2026-09-08，同日更新） | Google fontations，活跃（总下载 2750 万） | **推荐：度量 / glyph id / advance** |
| **harfrust** | 0.13.3（2026-08-25） | **HarfBuzz 官方 Rust 移植**（harfbuzz org，发布者含 Behdad Esfahbod 等核心成员）：2025-06 首发至今 30 版，MIT，总下载 880 万 | **推荐：复杂塑形**（rustybuzz 的正统继任者） |
| **subsetter** | 0.2.6（2026-06-04） | typst 出品，专为 PDF 嵌入子集化（TrueType/CFF），0 issue | **推荐：子集化**（typst 同款；README 提到未来通用替代是 fontations 系） |
| fontdb | 0.24.0（2026-07-29） | resvg 系（RazrFalcon），活跃 | 系统字体发现/枚举 |
| ttf-parser | 0.25.1（2024-11-29） | 维护模式（harfbuzz org） | **否决**：被 skrifa 取代 |
| rustybuzz | 0.20.1（2024-11-12） | **已停更**（2024-11 后无发布），harfbuzz org 重心已转 harfrust | **否决** |
| cosmic-text | 0.19.0（2026-04-22） | System76 整栈排版（shaping+断行+双向） | 备选：整引擎过重（本项目只要度量+塑形，断行自研） |
| parley | 0.11.1（2026-08-16） | Linebender 富文本布局，活跃 | 备选：同上 |

URL：https://crates.io/crates/skrifa 、https://crates.io/crates/read-fonts 、https://crates.io/crates/harfrust （仓库 https://github.com/harfbuzz/harfrust ）、https://crates.io/crates/subsetter 、https://github.com/typst/subsetter 、https://crates.io/crates/fontdb 、https://crates.io/crates/ttf-parser 、https://crates.io/crates/rustybuzz 、https://crates.io/crates/cosmic-text 、https://crates.io/crates/parley

**推荐组合**：`skrifa`（度量）+ `harfrust`（塑形）+ `subsetter`（子集化）+ `fontdb`（字体发现）。风险提示：harfrust 2025-06 才首发，属年轻 crate，但背靠 HarfBuzz 官方、发布节奏密集（2026-08-25 已至 0.13.3），风险可控。

### A4. 断行与 CJK 标点禁则（核实日期：2026-09-22）

| crate | 最新版本（日期） | 说明 | 结论 |
|---|---|---|---|
| **icu_segmenter**（ICU4X） | 2.3.0（2026-08-13） | UAX #14/#29；`LineSegmenter` + `LineBreakOptions { LineBreakStrictness: Strict/Normal/Loose/Anywhere, .. }`，与 CSS `line-break` 语义对齐 | **推荐** |
| hypher | 0.1.8（2026-09-17） | typst 的西文连字断词 | 推荐（西文补充） |
| unicode-linebreak | 0.1.5（2023-07-25） | UAX #14 参考实现，只给断点机会，无严格度区分 | **否决**：被 icu_segmenter 严格超集覆盖 |

URL：https://crates.io/crates/icu_segmenter 、https://docs.rs/icu_segmenter/latest/icu_segmenter/options/enum.LineBreakStrictness.html 、https://crates.io/crates/hypher 、https://crates.io/crates/unicode-linebreak

**CJK 禁则结论：有现成实现**。`icu_segmenter` 的 `LineBreakStrictness::Strict` 文档明确「class CJ 解析为 NS（non-starter，LB1 规则）」——行首禁则（。，」、）等不出现在行首）与行尾禁则（开括号/引号类不收尾，LB13/LB14 等）均由 UAX #14 机制覆盖，选 **Strict** 即得标准禁则基线；JIS X 4051 风格的标点悬挂/标点压缩仍需少量自研补充。

### A5. 位图 / 图像与栅格 inpainting（核实日期：2026-09-22）

| crate | 最新版本（日期） | 定位 | 结论 |
|---|---|---|---|
| image | 0.25.10（2026-03-10） | 编解码/基础处理事实标准 | 推荐（推理输入位图、调试导出） |
| tiny-skia | 0.12.0（2026-02-02） | Skia 子集 2D 栅格化 | 推荐（叠层/调试绘制） |
| resvg | 0.48.1（2026-08-02） | SVG 渲染（linebender org） | 仅间接需要 |

URL：https://crates.io/crates/image 、https://crates.io/crates/tiny-skia 、https://crates.io/crates/resvg

**栅格 inpainting：不值得首发做**。Rust 无成熟开箱方案——crates.io 仅 `inpaint` 0.1.7（2025-11-13，Telea/NS 经典算法）等小项目，社区主流是 ort 跑 LaMa 类 ONNX 或绑 OpenCV（https://crates.io/search?q=inpaint ）。本项目主路径是**内容流级删除文本**（根治，无需修补位图）；inpainting 仅在「文字与背景图形粘连 / 扫描件」场景有价值，留作后期 ort + LaMa 的可选增强。

---

## B. Rust ML 推理

### B6. ONNX 推理框架（核实日期：2026-09-22）

**`ort`（pykeio/ort）——推荐**

| 项目 | 核实结果 |
|---|---|
| 最新版本 | **2.0.0-rc.13**（2026-07-28 发布）；**至今没有 2.0 stable**（crates.io `max_stable_version = null`），已连发 13 个 rc |
| 捆绑 ONNX Runtime | **1.28.0**（官方 version-mapping：v2.0.0+ → ORT 1.28.0；主分支徽章显示已支持到 1.30.0） |
| 许可证 | MIT OR Apache-2.0 |
| 活跃度 | 总下载 1900 万+，43 个版本；被 HF Text Embeddings Inference、Google Magika 使用 |
| 链接/分发 | 默认**静态链接**（官方文档推荐）；`load-dynamic` feature 可运行时加载 dylib（`ort::init_from(path)` 或 `ORT_DYLIB_PATH`）——桌面 App 分发最灵活；ort 自带预编译二进制下载 |
| macOS CoreML / Apple Silicon | `coreml` Cargo feature + `Session::builder().with_execution_providers([ep::CoreML::default().build()])`；可指定计算单元（`ComputeUnits::CPUAndNeuralEngine` 可走 ANE）；含控制流算子的模型需 `.with_subgraphs()`；**EP 注册失败会静默回退 CPU**（需 `.error_on_failure()` 显式暴露） |
| Windows | 完整支持（CUDA/TensorRT/DirectML feature；DirectML dylib 由 `copy-dylibs` feature 自动拷入 target） |
| opset 支持 | ONNX opset 24 / ML opset 4 |

结论：**推荐**。「长期 rc」是唯一心理负担，但自 rc.9（2024-11）以来已稳定近两年，是 Rust ONNX 生态的事实标准；建议 `ort = "=2.0.0-rc.13"` 钉死版本。
URL：https://crates.io/crates/ort 、https://github.com/pykeio/ort 、https://ort.pyke.io/setup/linking 、https://ort.pyke.io/perf/execution-providers 、https://ort.pyke.io/misc/version-mapping

**`pp_layout` / `oar-ocr` 存在性核实**

| crate | 是否存在 | 细节 |
|---|---|---|
| `pp_layout` | **不存在**（crates.io API 404、docs.rs 404；GitHub 无任何 Rust 语言实现；搜到的均为 Python/TS 的 PP-DocLayout 封装） | — |
| `oar-ocr` | **存在且活跃** | v0.9.2（2026-08-18），Apache-2.0，维护者 GreatV（个人，非组织），29 万总下载，最新提交 2026-08-20。功能：OCR 检测/识别 + 版面分析 + 表格/公式 + VLM 文档理解（oar-ocr-vl 走 candle）。模型覆盖：PP-OCRv4/v5/**v6** 检测与识别（含多语言）、PicoDet/RT-DETR-H、PP-DocLayout S/M/L/Plus/**V2/V3**/DocBlockLayout、SLANet 表格、PP-FormulaNet 公式。后端即 `ort`，feature 含 **CoreML**/CUDA/DirectML/OpenVINO/TensorRT/WebGPU；`auto-download` 从 **ModelScope** 拉模型（国内网络友好），也支持内嵌 ONNX。风险：单一维护者、0.x API 不稳——可只用其前后处理代码 + 自管 ort session 规避 |

URL：https://crates.io/crates/oar-ocr 、https://github.com/greatv/oar-ocr

**PP 系模型 ONNX 导出途径与大小**

| 模型 | 状态 | 导出途径 | 大小 |
|---|---|---|---|
| PP-OCRv6 | PaddleOCR v3.7.0（2026-06-11）发布，三档：tiny 1.5M / small 7.7M / medium 34.5M 参数；单模型 50 语言；medium 对 v5_server 检测 +4.6%、识别 +5.1% | PaddleX paddle2onnx 插件：`paddlex --install paddle2onnx` → `paddlex --paddle2onnx --paddle_model_dir ... --onnx_model_dir ...`（默认 opset 7，transformer 结构需更高）；oar-ocr 已内置 v6 ONNX 自动下载 | 官方未给 MB 数（tiny/small 档应为个位数 MB 量级） |
| PP-OCRv5 | v3.0（2025-05）发布，5 种文字类型单模型，精度较 v4 +13% | 同上 | — |
| PP-DocLayoutV3 | PaddleOCR-VL-1.5 的版面模型，底层 **RT-DocLayout（ECCV 2026 论文）**，输入 800×800，**单次前向**同时输出 25 类布局框（300 queries）+ bbox + 200×200 实例分割 mask + **阅读顺序 logits** | **无官方 ONNX**（官方 HF 仓库只发 paddle-inference + safetensors）。三条现成路：(1) PaddleX paddle2onnx 插件转换（官方路径）；(2) HF 第三方 ONNX 导出（Bei0001、alex-dinh、beclab、aoiandroid，含 FP16/CoreML/OpenVINO 变体）；(3) oar-ocr 内置（ModelScope 分发） | 第三方实测：ONNX 图 ~5MB + 权重侧车 ~137MB ≈ **142MB FP32**，CPU ~480ms/页 |

- ⚠️ 注意：搜索结果中「PP-DocLayoutV3 ONNX 仅 7.5MB」的说法来自被垃圾站污染的结果页，与实测 142MB 严重不符，**不可信**。若嫌 V3 大：官方有 `PP-DocLayoutV2_onnx`、`PP-DocLayout_plus-L_onnx`（官方 ONNX 发布，体积更小），oar-ocr 也支持。
- URL：https://github.com/PaddlePaddle/PaddleOCR/releases 、https://github.com/PaddlePaddle/PaddleOCR/releases/tag/v3.7.0 、https://paddlepaddle.github.io/PaddleX/latest/en/pipeline_deploy/paddle2onnx.html 、https://huggingface.co/PaddlePaddle/PP-DocLayoutV3 、https://huggingface.co/Bei0001/PP-DocLayoutV3-ONNX 、https://huggingface.co/models?search=PP-DocLayout

**备选推理框架对比**

| 框架 | 最新版（日期） | 许可 | 对检测模型实用性 | 结论 |
|---|---|---|---|---|
| ort | 2.0.0-rc.13（2026-07-28） | MIT/Apache | Paddle 导出的检测模型即开即用 + CoreML/DirectML 加速 | **推荐** |
| candle | 0.11.0（2026-06-26；HuggingFace，维护者已从 Laurent Mazare 移交 ivarflakstad） | MIT/Apache | 无 ONNX 前端，需手写模型结构；适合跑 VLM（oar-ocr 的 VLM 侧即用 candle） | 备选（VLM 场景） |
| tract | 0.23.8（2026-09-21，sonos，极活跃） | MIT/Apache | 纯 Rust ONNX 推理，但 RT-DETR 系 transformer 算子（deformable attention 等）支持不全，需实测 | **否决**（兼容性风险 + 无硬件加速收益） |
| burn | 0.21.0（2026-05-07；另有 0.22.0-pre.3） | MIT/Apache | burn-import 可转 ONNX，但为训练框架设计，推理嵌入偏重 | **否决**（过重，兼容层不如 ort 直接） |

### B7. 布局模型对比（替换 MinerU 云 API + 本地 Paddle）（核实日期：2026-09-22）

| 模型 | 结构/精度 | 许可证 | 分发 | 结论 |
|---|---|---|---|---|
| **PP-DocLayoutV2 / plus-L** | RT-DETR 系，类别少于 V3 | Apache-2.0 | **官方直接发布 ONNX**（`PP-DocLayoutV2_onnx`、`PP-DocLayout_plus-L_onnx`），体积更小 | **推荐起步**：小而稳、零转换成本；MinerU 4.0 自己也退回用 V2 |
| **PP-DocLayoutV3** | RT-DocLayout（ECCV 2026），25 类 + 阅读顺序 + 分割 mask | Apache-2.0 | ONNX ~142MB FP32（可 FP16/量化）；oar-ocr 内置 | **推荐升级项**：更强（阅读顺序、歪斜版面），代价是 142MB 分发 |
| DocLayout-YOLO | YOLO-v10 + DocSynth300K 预训练；D4LA mAP 70.3 / DocLayNet 79.7 | **AGPL-3.0** | 无官方 releases；News 止于 **2024-10**；ONNX 导出官方未文档化（仅社区导出） | **否决**：AGPL 传染 + 停更（38 commits 即弃更迹象） |
| MinerU 本地 | 4.0.5（2026-09-20）；4.0 改 **VLM 路线**（MinerU2.5 1.2B VLM + llama.cpp），四档质量；4.0 的 ONNX 模型包 = PP-DocLayoutV2 + PP-OCRv6 tiny det + small rec + PP-FormulaNet plus-M；2.x 时代曾用 DocLayout-YOLO | Apache-2.0 **+ 附加条件**（非纯 Apache，需审 LICENSE.md） | 模型下载 0.8GB（basic）～3GB（full） | **否决内嵌**：VLM 路线依赖 llama.cpp、模型 ≥0.8GB，桌面分发过重；但其 4.0 ONNX 包恰好证明「PaddleX 模型 + ONNX」是业界收敛路线 |
| docling DSPE（IBM） | RT-DETR 基座版面模型 | 代码 MIT / 权重 IBM 自定义许可（允许商用），HF `ds4sd/docling-models` | HF 分发 | 英文向备选；类别体系偏 IBM 文档场景，中文不如 PP 系；Rust 侧无现成绑定 |

**推荐**：`ort`（coreml，静态或 load-dynamic）+ `oar-ocr`（或仅参考其前后处理）+ **PP-DocLayoutV2 起步 → PP-DocLayoutV3 升级** + PP-OCRv6 small 档做 OCR。

URL：https://github.com/opendatalab/DocLayout-YOLO 、https://github.com/opendatalab/MinerU 、https://github.com/opendatalab/MinerU/releases 、https://github.com/DS4SD/docling

---

## C. Rust 服务 / 基础设施

### C8. 异步与 HTTP / LLM 客户端（核实日期：2026-09-22）

基础：tokio 1.53.1（2026-07-20，https://crates.io/crates/tokio ）；reqwest 0.13.5（2026-09-08，确认有 `rustls` / `rustls-no-provider` feature，https://crates.io/crates/reqwest ）。

LLM 调用 crate：

| crate | 最新版（日期） | 关键事实 | 结论 |
|---|---|---|---|
| **genai** | 0.6.5（2026-09-08；0.7.0-beta.23 流水线中，2026-09-08） | 定位「native-protocol 多 provider」：开箱 26+ provider——`openai`（chat/completions）、`openai_resp`（Responses）、**`anthropic`（原生 Messages 协议，含 prompt caching、SSE heartbeat、thinking block）**；自定义 OpenAI 兼容端点走 `genai_n` 适配器（`GENAI_1_ENDPOINT` 环境变量）；流式 `ChatFrameSink` 支持原始帧检查 | **推荐** |
| async-openai | 0.42.0（2026-09-09；MIT；2014 stars） | **已完整支持 Responses API**：仓库含 `src/responses/` 模块（api/stream/websocket）+ examples（responses-stream / multi-turn-reasoning / structured-outputs 等）；v0.31.0（2025-11-28）起一等公民，可按 feature `responses`/`chat-completion` 裁剪；v0.36.0 升 reqwest 0.13 | OpenAI 生态专用，对 Anthropic 无帮助 → **单独使用否决** |
| rig-core | 0.42.0（2026-08-17） | opinionated LLM 应用框架：agent/ECS/向量库集成/MCP，远超 HTTP client 范畴；0.x 每 2 周一版 | **否决** |
| Anthropic 官方 Rust SDK | — | `anthropics/anthropic-sdk-rust` GitHub API 404，**不存在**；社区 crate 全部偏弱（anthropic-sdk-rust 0.1.1（2025-06-11）、async-anthropic 0.6.0（2025-05-03，16 个月未更）、anthropic 0.0.8 已弃） | **否决** |

URL：https://github.com/jeremychone/rust-genai 、https://github.com/64bit/async-openai/tree/main/async-openai/src/responses 、https://github.com/64bit/async-openai/releases 、https://github.com/0xPlaygrounds/rig 、https://api.github.com/repos/anthropics/anthropic-sdk-rust 、https://crates.io/crates/anthropic-sdk-rust

**结论**：推荐 **genai 0.6.5**（正好覆盖全部需求：Anthropic Messages 原生协议 + OpenAI chat/completions + Responses + 自定义兼容端点 + SSE 流式，API 面比 rig 小一个数量级）。**备选**：reqwest(rustls) + eventsource-stream 0.2.3（https://crates.io/crates/eventsource-stream ）手写——翻译场景每 provider 只需 chat + stream 两个端点，两 provider 合计几百行，零框架锁定；genai 0.7 API 变动造成摩擦即回退此路线。

### C9. 持久化与序列化（核实日期：2026-09-22）

| crate | 最新版（日期） | 结论 |
|---|---|---|
| **rusqlite** | 0.40.2（2026-08-08） | **推荐**：`bundled` feature 自带 SQLite 源码编译，免系统依赖（docs.rs features 列表核实） |
| sqlx | 0.9.0（2026-05-21；上一稳定版 0.8.6 还是 2025-05，节奏偏慢） | **否决**：异步 + 编译期查询检查为服务端多连接设计；桌面单用户单连接用不上，反而引入 build.rs 连库编译、tokio 耦合 |
| **ciborium** | 0.2.2（2024-01-24，冻结但稳定，2.4 亿下载） | **推荐**：serde 原生派生，serde_json 可直接替换 |
| minicbor | 2.3.0（2026-07-23） | 备选：no_std 优势在桌面无用，且不兼容 serde（自有 derive） |
| **zstd** | 0.14.0（2026-09-04） | **推荐** |

组合建议：**rusqlite(bundled) + serde + ciborium + zstd**——SQLite 存任务/会话元数据；布局识别结果序列化 CBOR → zstd 后按 `(文件哈希, 页号)` 存 BLOB 列做缓存（同 key 直接 `zstd::decode_all` 反序列化）。

URL：https://crates.io/crates/rusqlite （features：https://docs.rs/crate/rusqlite/latest/features ）、https://crates.io/crates/sqlx 、https://crates.io/crates/ciborium 、https://crates.io/crates/minicbor 、https://crates.io/crates/zstd

### C10. 进程模型（重点）（核实日期：2026-09-22）

**决定性的核实事实**：

1. Electron **`utilityProcess.fork()` 只接受 JS 入口**（`modulePath` = "Path to the script"），**不能直接跑原生二进制**；且文档明确「Configuring stdin to any property other than ignore is not supported and will result in an error」——**utilityProcess 无法与子进程做 stdin 管道通信**。https://www.electronjs.org/docs/latest/api/utility-process
2. **napi-rs 官方文档对 Electron 只字未提**（napi.rs 与 website 仓库的 support-compatibility/FAQ 全文 grep "electron" 为空）；Electron 官方 native modules 文档笼统要求原生模块重编译。napi 3.12.7 / napi-derive 3.6.8（2026-09-20）本身活跃。https://napi.rs/docs/ 、https://www.electronjs.org/docs/latest/tutorial/using-native-node-modules
3. Tauri sidecar 是成熟先例（参考样板）：`externalBin` + `-$TARGET_TRIPLE` 后缀打包，shell 插件 `spawn()` 长驻进程、stdout 逐行接收转发。https://v2.tauri.app/develop/sidecar/

四方案对比：

| 维度 | (a) sidecar 子进程 stdin/stdout JSONL | (b) 本地 HTTP/WS（axum 0.8.9，2026-04-14） | (c) napi-rs 原生模块 | (d) Electron utilityProcess 包 JS 壳 |
|---|---|---|---|---|
| 取消语义 | **最优**：协议层 cancel + kill 即取消，任务状态全丢弃，冷重启子进程即可 | HTTP abort 只是通知，引擎内部要自建取消令牌 | 最差：panic 传染或需 catch_unwind 全覆盖 | 需再从 JS 壳 spawn 二进制，双层 |
| 进度流 | stdout 逐行推送，天然 | WS/SSE 均可 | ThreadsafeFunction 回调 | MessagePort，但要经 JS 中转 |
| 崩溃隔离 | **进程级隔离**，引擎 panic 不影响 UI | 进程级隔离 | **同进程，无隔离**；OOM/panic 可拖垮整个 app | 进程级（但只隔离 JS 壳） |
| 打包/签名 | extraResources 放二进制；macOS 需单独签二进制（electron-builder `binaries`） | 同 (a)，还要管端口占用/防火墙弹窗（Windows） | .node 跟随 asar/unpacked，3 OS × 2 arch 构建矩阵 | 打包 JS 壳 + 仍需放二进制 |
| 调试 | **引擎可独立 CLI 运行/单测/管道测试** | 需先起服务（curl 可测） | 只能在 Node 内跑，原生崩溃栈最难调 | 双层启动 |

**推荐：(a) sidecar 子进程 + stdin/stdout JSONL**，由 Electron 主进程 `child_process.spawn` 直接拉起。协议建议：请求带 `id`，响应/进度事件带相同 `id`；取消 = `{"method":"cancel","id":...}` 优雅 + 超时后 kill 兜底。
- 备选 (b) axum（https://crates.io/crates/axum ）：若未来引擎要多客户端/浏览器扩展复用再升级；当前多付端口/loopback 鉴权复杂度。
- **否决** (c) napi-rs（无官方 Electron 支持承诺 + 无崩溃隔离）；**否决** (d) utilityProcess（已核实不能跑原生二进制、stdin 只能 ignore——对本用例是死胡同）。

### C11. 并发模型（核实日期：2026-09-22）

- rayon 1.12.0（2026-04-14）：https://crates.io/crates/rayon
- 社区/tokio 文档共识：短 CPU 工作内联；中等阻塞 `tokio::task::spawn_blocking`；**长 CPU 密集用 rayon 自有线程池 + `tokio::sync::oneshot` 桥接回 async**；绝不在 tokio worker 线程直接跑 `par_iter`（会饿死同线程所有任务）；`spawn_blocking` 池（上限约 512 线程）为阻塞 IO 设计，不适合持续 CPU 工作；混合部署给 rayon `ThreadPoolBuilder::num_threads(n)` 限核。https://docs.rs/tokio/latest/tokio/task/fn.spawn_blocking.html
- 管线设计：`producer(pages) → rayon CPU 池（ONNX 推理 + 排版 + 回写）→ tokio LLM 并发（Semaphore 限流）→ rayon 回写`，两端用 channel/oneshot 解耦。

**per-page 并行注意点**：
1. **内存峰值**：每页持有页面位图 + 布局输出 + 字体数据；页级并行度应可配（默认 ≈ 物理核数一半起测），rayon `num_threads` 限核。
2. **ONNX 线程叠加**：ort Session 跨线程共享安全，但 N 路页级并行 × Session intra-op 线程 M 会超订阅——并行推理时 `intra_op_num_threads` 设 1–2。
3. **顺序保持**：rayon `par_iter().map(...).collect()` 保序（indexed 并行迭代器保证）；或 `enumerate` + 结果槽位聚合。
4. **LLM 段（IO）与 CPU 段并发度解耦**：LLM 用 tokio Semaphore（8–16 起步、按 provider 限流）。
5. **pdfium 全局锁**（A1）：`thread_safe` 是全局互斥锁——pdfium 调用段收敛到专用单线程队列（或解析阶段串行），推理/排版/LLM 段照常并行。

---

## D. Electron 前端「基于 VSCode」

### D12. 三条实现路线对比（核实日期：2026-09-22）

| 维度 | (a) Fork Code-OSS | (b) Eclipse Theia 壳 | (c) 自建 Electron + React 复用 VSCode 视觉 |
|---|---|---|---|
| 最新版本 | microsoft/vscode 持续滚动（无传统版本号） | Theia 1.75.0（2026-08-27） | Electron 44.4.3 + React |
| 许可 | MIT（但**禁用微软 Marketplace/品牌**，需换 Open VSX） | EPL-2.0 + 二次依赖 | 自选（MIT 系） |
| 「保留布局样式」程度 | 100%（就是 VSCode） | ~85%（视觉近似，非 VSCode 本体；官方自述「不是 VSCode fork」） | ~70–85%（取决于还原投入） |
| 首次工作量 | 数周起步（裁剪 + 构建管线；完整构建数十分钟，`product.json` 刻意不进仓库需自动生成） | 1–2 周（模板起项目） | 2–4 周（自搭 workbench 骨架） |
| 长期维护成本 | **极高**（月度 upstream merge 冲突；Cursor/Windsurf 走此路但有全职团队，VSCodium 是最成熟先例） | 低–中（跟 Theia 版本） | **完全自控（零升级税）** |
| 结论 | **否决** | 备选 | **推荐** |

URL：https://github.com/microsoft/vscode/wiki/How-to-Contribute 、https://github.com/VSCodium/vscodium 、https://open-vsx.org/ 、https://theia-ide.org/ 、https://github.com/eclipse-theia/theia/releases

否决理由展开：对「PDF 翻译工作台」而言，fork Code-OSS 会继承整个编辑器内核（LSP/扩展系统/多编辑器组）——巨量无用负担 + 每月 upstream rebase 长期税；Theia 框架重（InversifyJS DI、自有组件体系），逆着框架裁剪成 PDF 工具不划算，唯一强理由是将来做通用 IDE。

**路线 (c) 的物料清单（全部核实 2026-09-22）**：

| 组件 | 最新版 | 最近发布 | 状态 | URL |
|---|---|---|---|---|
| `@vscode/codicons` | 0.0.46 | 2026-09-16（dist 更新） | 活跃（微软官方图标） | https://microsoft.github.io/vscode-codicons/ |
| `@vscode-elements/elements` | 2.5.1 | 2026-02-21 | 活跃（社区 web components，还原 VSCode 控件：button/tree/panel/checkbox 等） | https://github.com/vscode-elements/elements |
| vscode-web-ui-toolkit | — | — | **已废弃**（npm 404，仓库 2024 归档；官方指引改用主题 token + codicons，社区建议 vscode-elements） | https://github.com/microsoft/vscode-webview-ui-toolkit |
| 主题 CSS 变量 | `--vscode-*` token | — | 官方主题色参考表（可导出 CSS custom properties，提取 Dark+/Light+ 做默认主题） | https://code.visualstudio.com/api/references/theme-color |
| `monaco-editor` | 0.56.0 | 2026-07-20 更新 | 活跃（译文编辑区） | https://www.npmjs.com/package/monaco-editor |
| `allotment` | 1.20.5 | 2025-12-19 | 活跃；灵感来源就是 VSCode split view（嵌套 + persisted layout + snap） | https://github.com/johnwalley/allotment |
| `react-resizable-panels` | 4.13.1 | 2026-09-20 | 非常活跃（bvaughn）；更轻 | https://github.com/bvaughn/react-resizable-panels |

allotment vs react-resizable-panels：两者都是独立 React 库（非 svelte 衍生）。做 **VSCode workbench 嵌套分栏选 allotment**（VSCode 式交互手感 + 布局持久化）；只做简单两栏可用 react-resizable-panels。

**骨架方案**：左侧 activitybar（codicons）+ allotment 嵌套出 sidebar / editor / panel + 底部 statusbar；样式全部走 `--vscode-*` token；控件尽量用 `@vscode-elements/elements`，缺失的自己按 token 补。工作量量级 2–4 周出骨架。

### D13. Electron 工具链（核实日期：2026-09-22，npm registry 直查）

| 包 | 最新版 | 最近发布 | 备注 |
|---|---|---|---|
| `electron` | **44.4.3** | 2026-09-18 | 44.0.0 stable 于 2026-08-25；**Chromium 152 / Node 24.18.1**（官方 schedule.json；部分搜索结果声称 Chromium 144/Node 22 系，以 schedule.json 为准）；最低 macOS 11；45 计划 2026-10-20（Chromium 156） |
| `electron-vite` | 5.0.0 | 2025-12-07 | peer: vite ^5‖^6‖^7；社区 DX 首选（三进程 HMR）；`npm create @quick-start/electron` 含 React+TS 模板 |
| `@electron-forge/cli` | 7.11.2 | 2026-07-02 | 官方工具链，`--template=vite-typescript` |
| `electron-builder` | 26.15.3 | 2026-09-07 | 打包/签名/公证/extraResources |
| `electron-updater` | 6.8.9 | 2026-09-02 | 自动更新 |

URL：https://releases.electronjs.org/schedule.json 、https://www.electronjs.org/docs/latest/tutorial/electron-timelines 、https://github.com/alex8088/electron-vite 、https://www.electronforge.io/ 、https://www.electron.build/docs/configuration

**结论**：**electron-vite（脚手架 + dev/HMR）+ electron-builder（打包分发）** 是当前社区主流组合（electron-vite 赢在 DX，Forge 赢在官方背书但 maker 打包 sidecar/签名公证不如 electron-builder 成熟）。Rust sidecar 用 electron-builder `extraResources` 打进 `Contents/Resources`（macOS）/`resources`（Windows），运行时 `process.resourcesPath` 定位；macOS 签名公证走 `afterSign` 钩子（electron-notarize）+ hardened runtime，**sidecar 二进制也要一并签**；自动更新用 electron-updater（GitHub Releases / 通用 feed）。

### D14. PDF 预览（pdf.js）（核实日期：2026-09-22）

| 项 | 核实结果 |
|---|---|
| `pdfjs-dist` 最新版 | **6.3.289**（2026-08-29，npm + GitHub Releases 双核实）；主版本线 v6（6.0.227 于 2026-05-30） |
| 构建产物 | 现代构建全部 ESM（.mjs）：`build/pdf.mjs` / `build/pdf.worker.min.mjs`；旧浏览器另有 `legacy/` |
| v6 breaking | `getDocument` 必须传参数对象；删除 `PDFDocumentProxy.destroy`；`getAttachments`/`getDestinations` 等改返回 Map/Set；v5.7 起最低 Node 22 |

URL：https://www.npmjs.com/package/pdfjs-dist 、https://github.com/mozilla/pdf.js/releases 、官方 webpack 示例 https://github.com/mozilla/pdf.js/blob/master/examples/webpack/main.mjs

**Electron 中推荐姿势**：
- **worker**：Vite 下 `import PdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?worker'` + `GlobalWorkerOptions.workerPort = new PdfWorker()`（或 `new Worker(new URL(...), { type: 'module' })` + `workerSrc`）。
- **本地文件**：主进程 `protocol.handle` 注册自定义协议（如 `app-pdf://`）流式返回本地 PDF，渲染进程 `getDocument(url)`；比 `file://`（webSecurity 限制、路径转义坑）和手动 ArrayBuffer 干净；安全上 `webSecurity: true` + 不开 nodeIntegration。
- **canvas + HiDPI**：`viewport = page.getViewport({ scale })`；`canvas.width = viewport.width * dpr`；`ctx.setTransform(dpr,0,0,dpr,0,0)` 后 `page.render({ canvasContext, viewport })`。
- **bbox 叠层坐标换算惯例**：布局模型输出 PDF 用户空间坐标（origin 左下、y 向上）；用 `viewport.convertToViewportPoint(x,y)` / `convertToViewportRectangle` 一步完成翻转+缩放+旋转 → CSS 像素（origin 左上、y 向下）；叠层用绝对定位 div（`left/top/width/height`，旋转页加 `transform: rotate`）；DPR 只影响 canvas 内部像素，不影响 CSS 布局坐标。
- **`react-pdf`（wojtekmaj）11.0.0（2026-09-10）否决**：peer 强制 React 19，且层级封装挡手（精细控制 canvas/叠层/增量重渲是负资产）——直接用 pdfjs-dist 裸 API。注意与 `@react-pdf/renderer`（React→PDF 生成器，另一回事）无关。https://github.com/wojtekmaj/react-pdf

### D15. 与 Rust 引擎通信的落点（核实日期：2026-09-22）

- 安全基线：`nodeIntegration: false` + `contextIsolation: true` + `sandbox: true` 下渲染进程不能 spawn 进程，一切经 preload + `contextBridge.exposeInMainWorld` 暴露白名单 API。https://www.electronjs.org/docs/latest/api/context-bridge
- MessagePort（`MessageChannelMain`、`webContents.postMessage(..., [port])`）可做主进程 ↔ 渲染进程的专用进度通道，避免 ipcMain 全局广播。https://www.electronjs.org/docs/latest/tutorial/message-ports 、https://www.electronjs.org/docs/latest/api/ipc-main
- utilityProcess 不支持 stdin 管道（见 C10），故 JSONL 协议必须用主进程 `child_process.spawn`。

**推荐架构**：主进程 `child_process.spawn(engine)` + JSONL over stdin/stdout（请求带 id，进度事件 `{"type":"progress",...}`，取消 `{"type":"cancel","id":...}`）；主进程聚合后 `webContents.send` 转发，或启动任务时用 MessagePort 直连渲染进程做高频流（逐段进度、bbox 预览数据）。preload 只暴露 `startTranslation / cancel / onEvent` 等少数方法。后期引擎要多窗口共享/浏览器预览页，再升级为引擎常驻本地 WS（随机端口 + 本机 token）。napi-rs 否决（崩溃隔离为零）。

---

## E. 同类开源项目参考（核实日期：2026-09-22）

### E16-1. pdf2zh / BabelDOC 生态总览

| 项目 | 仓库 | Stars | 最近推送 | 许可证 | 最新版 | 状态 |
|---|---|---|---|---|---|---|
| pdf2zh 1.x（主仓库） | PDFMathTranslate/PDFMathTranslate | 37,126 | 2026-09-21 | AGPL-3.0 | PyPI 1.9.11（2025-07-11） | 活跃；2026-09-08 新增实验性 OCR |
| pdf2zh 2.0 | PDFMathTranslate/PDFMathTranslate-next | 694 | 2026-04-08 | AGPL-3.0 | 无 GitHub Release | 官方「调用 BabelDOC 的参考实现」，自述不再面向社区贡献 |
| BabelDOC | funstory-ai/BabelDOC | 9,586 | 2026-08-05 | AGPL-3.0 | 0.6.4（2026-07-16） | 活跃，自述「早期阶段」 |

三者关系（README 核实）：1.x 主仓库 2026-03-23 起提供 `--mode precise`（隔离环境调 v2 内核）与 `--babeldoc` 旗标；2.0 整体迁往 next 仓库、以 BabelDOC 为内核；BabelDOC 自述「主要设计为嵌入其他程序」，端用户自部署应用 pdf2zh 2.0。注意：**Byaidu/PDF2ZIP 不存在**（GitHub API 404）；pdf2zh 作者 Byaidu，主仓库现归 PDFMathTranslate 组织。

URL：https://github.com/PDFMathTranslate/PDFMathTranslate 、https://api.github.com/repos/PDFMathTranslate/PDFMathTranslate 、https://github.com/PDFMathTranslate/PDFMathTranslate-next 、https://github.com/funstory-ai/BabelDOC 、https://pypi.org/pypi/pdf2zh/json 、https://pypi.org/pypi/BabelDOC/json

### E16-2. pdf2zh 1.x 内容流回写机制（读源码，main@2026-09）

管线：**pdfminer.six 解释 → 字形级几何 → 逐段翻译 → 重新生成页面内容流 → PyMuPDF 落盘**。

| 环节 | 实现（文件） | 关键细节 |
|---|---|---|
| 解析 | `pdf2zh/converter.py` 的 `PDFConverterEx(PDFConverter)`，重载 `render_char` 把 `cid` 和原字体对象 hack 进 `LTChar` | 字形级 bbox、matrix、fontsize、字体、CID 全拿到；`pdfinterp.py` 处理 cropbox 偏移与页面旋转 |
| 移除原文字 | `pdf2zh/pdfinterp.py` 的 `execute()` 逐算子重放：**过滤所有 T 系文字算子**及 `"`、`'`、`EI`、`MP/DP/BMC/BDC`，其余算子（图像、路径、gs、cm…）重新序列化为规范化字符串 | 当前版本**已不用白色矩形遮盖**，直接丢弃文字算子；新页流 = `q {保留的非文字算子} Q 1 0 0 1 x0 y0 cm {新文字 BT…ET}` |
| Form XObject | `pdfinterp.py` 的 `do_Do` 递归解释后**内联拍平**为 `q {ops_base} Q {a b c d e f} cm {ops_new}` | XObject 拍平进页面流；另在 `high_level.py` 遍历全部 xref，把新字体键写进 Resources/Font 与 XObject 资源字典（`xref_set_key`） |
| 加新文字 | `converter.py` `gen_op_txt`：`/F size Tf 1 0 0 1 x y Tm [<hex>] TJ`；自写逐字形断行（右边界判定 + 行距表 zh 1.4/ja 1.1/en 1.2 + 超界 0.05 步进压缩） | 公式不翻译：按字体名正则 + Unicode 类别（Lm/Mn/Sk/Sm）+ 上下标字号比 0.79 识别，输出时以 `{vn}` 占位并用**原字体原 CID** 逐字形重排 |
| 嵌字体 | PyMuPDF `page.insert_font("tiro", None)`（Times 基础字体）+ Noto 远程下载；完成后 `doc.subset_fonts(fallback=True)` 子集化 | 字形宽度用 `noto.char_lengths()` 与 pdfminer `font.char_width()` |
| 双语页 | `doc_en.insert_file(doc_zh)` 后 `move_page` 交错排列；`write(deflate=True, garbage=3, use_objstms=1)` | — |
| 布局模型 | `doclayout.py` 从 `babeldoc.assets` 取 DocLayout ONNX，onnxruntime CPU/CUDA/DML | 1.x 依赖 `babeldoc>=0.1.22,<0.3.0`（仅取资产） |

源码：https://github.com/PDFMathTranslate/PDFMathTranslate/blob/main/pdf2zh/pdfinterp.py 、…/blob/main/pdf2zh/converter.py 、…/blob/main/pdf2zh/high_level.py

### E16-3. BabelDOC 架构（v0.6.4）

分层流水线，**纯 Python，仓库内无任何 .rs 文件**（funstory-ai 组织下亦无公开 Rust PDF 仓库）：

| 层 | 模块 | 说明 |
|---|---|---|
| 前端 | `format/pdf/document_il/frontend/il_creater(_active).py` + `new_parser/` | 从 PyMuPDF/预置页构建 IL |
| 中端 | `midend/`：layout_parser、paragraph_finder、typesetting、il_translator(_llm_only)、table_parser、styles_and_formulas、remove_descent | IL 上的多趟 pass |
| IL | `document_il/il_version_1.py` + **.xsd/.rng/.rnc 形式化 schema** | 有正式 schema 的中间表示，可导出/复放 |
| 后端 | `document_il/backend/pdf_creater.py`：RenderUnit/CharacterRenderUnit 体系，`BitStream` 拼 draw ops；SUBSET_FONT / SAVE_PDF 阶段 | 回写进原文档对象，保留 render_order |
| 自研 PDF 写入 | `format/pdf/babelpdf/`：cidfont.py、cmap.py、encoding.py、type3.py、base14.py、win_core.py | **不依赖 PyMuPDF 嵌字**：自己处理 CIDFont/CMap/编码/Type3/Base14 |
| 字形栈 | freetype-py（度量）+ uharfbuzz（整形）+ bitstring + pyzstd | pyproject.toml 核实 |

定位/许可：AGPL-3.0；默认输出带水印（`--watermark-output-mode` 可关）；1.0 目标「版式错误 <1%、内容损失 <1%」（README 自述目标）。商业化：托管于 Immersive Translate 的 BabelDOC 服务（每月 1000 页免费额度）。URL：https://github.com/funstory-ai/BabelDOC 、https://funstory-ai.github.io/BabelDOC/

### E16-4. Immersive Translate（闭源公开资料）

官网将「文档翻译」拆三档：**BabelDOC 保版式翻译 / PDF 翻译（免费本地）/ PDF Pro 翻译**。PDF Pro 自述：AI 解析 + 公式/表格识别 + OCR，**多栏重排为单栏的「重排版」路线**（"converting complex multi-column layouts into clearer single-column formats"，逐段上下对照），并非逐字保版式；免费档自述「传统本地算法无法处理含公式/表格/图片的 PDF」（即浏览器扩展的本地 pdf.js 叠层方案）。内容流回写与否未公开。URL：https://immersivetranslate.com/document/pdf-pro-translator/ （站点导航经首页核实）

### E16-5. Rust 实现的保版式翻译项目（GitHub 搜索 `pdf translation language:rust` 等）

| 项目 | Stars | 状态 | 相关度 |
|---|---|---|---|
| wx-rdc/diptych | 1 | 2026-09 新建，Apache-2.0，Rust ~74 万行 + TS | **最相关**：「版式保留、左右对照」PDF/DOCX 双语**阅读器**（非回写）；MinerU VLM 解析 + LLM 翻译；技术栈 `pdf 0.10.0` + `lopdf 0.45.0` + axum(SSE) + rusqlite(bundled) + reqwest(rustls)——值得持续跟踪 |
| Floratina/InsituTranslate | 6 | 开发中（2026-09-19），**无 LICENSE**，Rust 171 万行 | 不可取用代码 |
| withmargin/pdf-translate | 11 | 2026-05 后停更，MIT | 仅 2 天开发量 |
| rikonaka/translator-rs | 53 | 2024-08 停更，GPL-3.0 | 取词翻译，非保版式 |
| xiaoze-cn/BabelForge | 2 | 2026-08，AGPL-3.0 | BabelDoc 的 Rust 包装（AGPL 传染） |

**结论：目前没有成熟的开源 Rust「内容流改写式」保版式 PDF 翻译项目——生态位基本为空。**
URL：https://github.com/wx-rdc/diptych 、https://github.com/Floratina/InsituTranslate 、https://github.com/withmargin/pdf-translate

### E16-6. Rust 的 PDF 文本替换/编辑库

| crate | 版本（日期） | 许可 | 做法 | 评价 |
|---|---|---|---|---|
| **rasura** | 0.1.2（2026-08-16） | MIT/Apache-2.0 | **与本项目目标最接近**：glyph→字符还原→重断行→块级编辑→「未编辑字节逐字节原样」追加式保存；分层 rasura-cos/-content/-font/-layout/-edit/-flow；字体子集化/注入/嵌入；fidelity 分级 exact/reembedded/substituted/overlaid；1030 个真实 PDF 回归 + qpdf/pdf.js/pdfium 三方校验 | 0.1 早期、4 stars、限制明显（无粗斜体混排、无 CFF 嵌入、无表格）——**宜学架构不引依赖** |
| gema-edit（gemapdf 项目） | 0.7.0（2026-09-20） | MIT/Apache-2.0 | 「remove glyphs inside a region / replace text inside content streams」，基于 lopdf，**真改内容流而非遮盖矩形**；有 WASM 导出 `replace_text_glyphs` | 主项目是 PDF 压缩器，文本编辑为附属能力；自述「非 redaction 原语」 |
| open-redact-pdf 家族（8 个 crate） | 0.6.0（2026-05-02） | — | 浏览器优先的结构化（非遮盖式）redaction：内容流算子建模 + 确定性全量重写 | **算子级「删文字」参考实现** |
| pdf_oxide | 0.3.78（2026-09-08） | MIT/Apache-2.0 | 纯 Rust 引擎（1042 stars），文本/图像提取 + PDF 创建与编辑，3830 PDF 100% 通过率 | 增长极快，偏读取/提取；编辑深度待验证 |
| pdfspine（VoldemortGin） | WIP（2026-09-19） | Apache-2.0 | 「纯 Rust 重实现 PyMuPDF(fitz)」：解析/修复/解密/提取/编辑/增量保存/redaction | 6 stars，仅观察 |

URL：https://crates.io/crates/rasura 、https://github.com/myketheguru/rasura 、https://crates.io/crates/gema-edit 、https://github.com/p4ranoic0/gemapdf 、https://crates.io/crates/open-redact-pdf 、https://crates.io/crates/pdf_oxide 、https://github.com/VoldemortGin/pdfspine

### E16-7. typst 的 PDF 导出栈（2026 现状）

typst 0.15.1（2026-07-17）的 `typst-pdf` 自 0.13（2025-03）起底层改用 **krilla**（0.8.2，crates.io 2026-06-04；GitHub LaurenzV/krilla，444 stars，2026-09-20 仍有推送，Apache-2.0）；krilla 基于 **pdf-writer 0.15.0**（2026-05-27，仍在 typst org 维护）。结论：**typst 官方输出路径 = krilla（高层）+ pdf-writer（低层基座）**，「新建内容流 + 子集嵌字」直接对齐。URL：https://github.com/typst/typst/releases 、https://github.com/LaurenzV/krilla 、https://crates.io/crates/pdf-writer

### E16-8. pdf.js「渲染重排后打印回 PDF」路线（否决输出端）

- 采用者：Google 翻译文档模式与多家在线服务走「PDF→HTML/CSS→翻译→headless Chrome `--print-to-pdf`/Puppeteer」；Immersive Translate 免费档即此弱化版。
- 局限（社区共识）：丢失原 PDF 矢量图/字体/链接/书签；分页位置漂移；打印版式不可控（页眉页脚、分栏断页）；无法回写原文件。**对本项目只适合做降级预览，不适合输出端。**

### E 组对重写决策的要点

1. **pdf2zh 1.x 已验证「丢弃文字算子 + 保留其余算子 + 重建文字算子」的内容流改写路线可行**（不再是白矩形遮盖）；其 `pdfinterp.py` 的逐算子过滤/重放 + Form XObject 递归内联 + 资源字典补字体键，就是 Rust 版引擎要对齐的**行为基准**；其已知短板（行距启发式、公式字体名正则、Type3/CID 特例）可直接列为测试集。
2. **BabelDOC 证明「形式化 IL + 多趟中端 pass + 自研嵌字层」是工业级做法**（xsd/rng schema、uharfbuzz 整形、Type3/CMap/Base14 全自研），但 AGPL + 默认水印，不可复用代码，只能借鉴架构。
3. **Rust 保版式翻译生态位为空**：diptych（Apache-2.0）值得跟踪；rasura 的「字节级非局部性禁令 + fidelity 分级」与 gemapdf 的「区域删字形」是内容流改写最直接的 Rust 参考实现（都太早期，宜学设计不引依赖）。
4. 输出端字体子集化直接对齐 typst 官方栈（krilla 0.8.2 / pdf-writer 0.15.0）。

---

## 推荐技术栈一览表

| 层 | 推荐 | 备选 | 否决（理由） |
|---|---|---|---|
| PDF 解析/字形几何/渲染 | **pdfium-render 0.9.4**（static 静态链接或自带 dylib） | hayro 0.7.1（Device trait 优雅，长期跟踪） | mupdf（AGPL）、pdf-extract（太弱）、pdf-rs 单挑（无渲染，但 Op 枚举要参考） |
| 内容流回写 | **自写 Op 过滤器（参考 pdf-rs `Op`，递归 Form XObject + 累积 CTM）+ pdf-writer 0.15.0（Type0/CIDFontType2/Identity-H/ToUnicode/W）** | lopdf 0.45（对象级原地改）；krilla「整页 XObject+叠字」降级模式 | krilla 直接改写（README 明确只创建不改） |
| 字体（度量/塑形/子集化） | **skrifa 0.47 + harfrust 0.13.3 + subsetter 0.2.6 + fontdb 0.24** | cosmic-text / parley（整栈过重） | rustybuzz（停更，被 harfrust 取代）、ttf-parser（维护模式） |
| 断行/禁则 | **icu_segmenter 2.3.0（Strict，CJK 禁则现成）+ hypher 0.1.8（西文）** | 自补 JIS X 4051 风格标点悬挂 | unicode-linebreak（被 icu_segmenter 超集覆盖） |
| 图像 | image 0.25.10 + tiny-skia 0.12.0 | ort + LaMa inpainting（后期可选，首发不做） | — |
| ONNX 推理 | **ort =2.0.0-rc.13**（coreml feature；静态链接或 load-dynamic；Apple Silicon 可走 ANE） | candle 0.11（VLM 场景） | tract（RT-DETR 算子兼容风险）、burn（过重） |
| 布局模型 | **PP-DocLayoutV2 起步（官方 ONNX）→ PP-DocLayoutV3 升级（142MB，带阅读顺序/分割）**，经 oar-ocr 0.9.2 或仅参考其前后处理 | docling DSPE（英文向） | DocLayout-YOLO（AGPL+停更）、内嵌 MinerU 4.x（VLM ≥0.8GB+llama.cpp 太重、许可附加条款） |
| OCR | PP-OCRv6 small（oar-ocr 内置 ONNX 下载） | PP-OCRv5 | — |
| LLM 客户端 | **genai 0.6.5**（Anthropic 原生 Messages + OpenAI chat/Responses + 兼容端点 + SSE） | reqwest 0.13(rustls) + eventsource-stream 手写 | rig（过重）、社区 Anthropic crate（弱维护；官方无 Rust SDK）、async-openai 单用（无 Anthropic） |
| 持久化/序列化 | **rusqlite 0.40.2(bundled) + serde + ciborium 0.2.2 + zstd 0.14.0**（布局缓存 CBOR+zstd 存 BLOB） | minicbor 2.3.0 | sqlx（桌面单机用不上，构建重） |
| 进程模型 | **Electron 主进程 `child_process.spawn` sidecar + stdin/stdout JSONL**（id 关联、cancel+kill 兜底、崩溃隔离、可独立 CLI 调试） | axum 0.8.9 本地 WS（多客户端/浏览器复用时升级） | napi-rs（无 Electron 官方支持+无崩溃隔离）、utilityProcess（不能跑原生二进制、stdin 只能 ignore） |
| 并发 | tokio 1.53（调度/LLM IO，Semaphore 限流）+ rayon 1.12 限核 CPU 池 + channel 解耦；pdfium 调用段收敛单线程 | — | 在 tokio worker 直接 par_iter（饿死任务）；无限流的 ort intra-op 线程叠加 |
| 前端路线 | **(c) 自建 Electron + React + VSCode 视觉系统**：allotment 1.20.5 分栏 + `--vscode-*` token 主题 + @vscode/codicons + @vscode-elements/elements 2.5.1 + monaco-editor 0.56.0（译文编辑区） | Theia 1.75（仅当将来做通用 IDE） | fork Code-OSS（月度 upstream rebase 升级税不可承受）、vscode-web-ui-toolkit（已废弃） |
| Electron 工具链 | **electron 44.4.3（Chromium 152/Node 24.18.1）+ electron-vite 5.0.0（脚手架/HMR）+ electron-builder 26.15.3（extraResources 放 sidecar、afterSign 公证、sidecar 一并签）+ electron-updater 6.8.9** | Forge 7.11.2（官方背书但 sidecar 打包/公证弱） | — |
| PDF 预览 | **pdfjs-dist 6.3.289 裸 API**（`?worker` module worker + `protocol.handle` 自定义协议读本地文件 + HiDPI setTransform；bbox 叠层用 `viewport.convertToViewportRectangle`） | — | react-pdf 11（强制 React 19、封装挡手） |
| 前后端通信落点 | 主进程 spawn + ipcMain/webContents.send 转发（高频流用 MessagePort 直连）；preload + contextBridge 白名单（startTranslation/cancel/onEvent） | 渲染进程直连引擎 WS（升级期） | 渲染进程直接 spawn（contextIsolation 下不可行） |
| 参考实现 | pdf2zh 1.x（行为基准，AGPL 只读不抄）、BabelDOC（IL 架构参考）、rasura / gema-edit / open-redact-pdf（Rust 内容流改写设计）、diptych（同赛道跟踪）、typst 输出栈（krilla/pdf-writer 用法） | — | 直接复用 AGPL 代码（pdf2zh/BabelDOC/DocLayout-YOLO） |

## 主要风险与缓解

| 风险 | 缓解 |
|---|---|
| ort 长期停留在 2.0 rc | 钉死 `=2.0.0-rc.13`；rc.9 以来近两年无破坏，生态事实标准 |
| pdfium-render 全局锁 vs 页级并行 | pdfium 调用段收敛到专用单线程队列；推理/排版/LLM 并行不受影响 |
| harfrust 年轻（2025-06 首发） | 背靠 HarfBuzz 官方、发布密集；API 与 rustybuzz 近似可临时回退 |
| oar-ocr 单维护者、0.x | 只用其前后处理代码 + 自管 ort session；模型从 ModelScope/HF 自管分发 |
| PP-DocLayoutV3 无官方 ONNX、142MB | 起步用官方 PP-DocLayoutV2 ONNX；V3 经 PaddleX paddle2onnx 自转 + FP16/量化压缩 |
| lopdf 历史停更印象 | 已核实 2026 年复活且发版勤（0.43/0.44/0.45），仅作对象级备选 |
| fork Code-OSS 的诱惑 | 已核实月度 rebase 成本 + 数十分钟级构建；自建路线 2–4 周即可达 70–85% 视觉还原 |

---

*调研方法说明：本报告由 5 条并行核实线索（A. PDF 读写栈；B. ML 推理；C. 服务/基础设施；D. Electron 前端；E. 同类项目）汇总，所有版本号经 crates.io API / GitHub API / npm registry / 官方文档当日核实，核实日期均为 2026-09-22。*
