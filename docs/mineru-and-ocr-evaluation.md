# 文档解析模型评估：MinerU 与百度文档解析

> 状态：技术评估初稿
>
> 目标：判断 MinerU 是否适合作为 BabelDOC 的文档结构解析层，并比较百度云文档解析 / OCR、PaddleOCR 生态等替代方案，明确目录、超链接和版式保真问题应由哪个 pipeline 层负责。

## 结论先行

1. **MinerU 值得作为 PDF 版面与阅读顺序解析器进行 A/B 测试，但不能被视为完整的 PDF 保真方案。** 它擅长把页面视觉内容转换成有顺序的结构化内容，通常覆盖标题、正文、表格、图片/插图、公式、代码、页眉页脚、脚注、参考文献等类别，并能输出 Markdown、JSON 和中间图片/公式资源。
2. **目录要分成两种含义。**
   - 页面上印刷出来的“目录页”：可以被识别为标题/文本/列表等视觉内容，取决于版面模型和阅读顺序；通常不会天然得到可靠的目录树语义。
   - PDF 的交互式书签/Outline：不是 OCR 或版面类别，MinerU 的通用解析结果不能替代 PyMuPDF 等 PDF 对象层读取。必须从原 PDF 直接读取、校验和写回。
3. **超链接也不是 OCR 的主要能力。** URL 字符串可能被识别，页面上的链接文字也可能被识别，但 PDF `/Link` 注释、URI、内部跳转目标、矩形和页内坐标属于 PDF 对象层。解析器输出 Markdown 链接并不等于保留了可点击链接。
4. 用户提供的糟糕结果更像是 pipeline 的结构契约和重建问题的叠加：目录页被归为 `text` 会破坏翻译视图的层级；链接如果靠译文字符串搜索重定位会漂移；PDF 书签若只在拼页时复制页码也可能与翻译后的章节结构脱钩。模型切换只能修复其中一部分。
5. 推荐路线是：**保留原生 PDF 对象层作为保真基线，引入 MinerU 或百度作为结构候选层，再用确定性规则和原始坐标对齐融合。** 不建议直接把 OCR/Markdown 结果作为最终重建真源。

## MinerU 能识别和输出的内容类别

MinerU 不同版本、后端和输出格式的字段名称会变化，下面按能力而不是某个版本的枚举名归类。接入时必须以实际版本的 `middle.json` / `content_list.json` schema 做适配，并保留未知类别。

| 类别 | 常见输出 | 对翻译的意义 | 主要风险 |
|---|---|---|---|
| 标题 / 章节标题 | title、heading、层级或阅读顺序 | 可映射为 H1/H2/H3，避免整篇变成正文 | 标题层级常是推断结果；论文的 Abstract、编号标题可能被判为正文 |
| 正文段落 | text、paragraph | 主要翻译单元 | 双栏、跨栏、文本框、浮动段落可能被拆分或合并错误 |
| 列表 / 列表项 | list、list item，或普通 text | 保留编号、项目符号和嵌套关系 | 版本间类别不稳定；不能仅依赖类别名识别目录 |
| 表格 | table、表格结构、单元格或 HTML/Markdown | 可做单元格级翻译与重建 | 合并单元格、跨页表格、表格中的公式和链接需要单独处理 |
| 图片 / 插图 | image、figure、图片裁剪资源 | 可保留原图；图注可单独翻译 | 图片内文字通常不会自动成为可重排的翻译文本 |
| 图注 / 表注 | figure caption、table caption | 通常应翻译 | 可能被并入正文或图像区域 |
| 公式 | equation、inline/display formula、LaTeX 或公式图片 | 可作为受保护 token，或转为公式重建 | 公式识别错误会造成 token、字体和版式问题；OCR 结果不能直接覆盖原 PDF 公式 |
| 代码 | code、code block | 可保护原文，仅翻译注释/说明 | 等宽字体、缩进和跨行结构容易损坏 |
| 页眉 / 页脚 / 页码 | header、footer、page number | 通常保护或按规则处理 | 重复文本会被误合并进正文；页眉页脚的链接也不能只靠文本恢复 |
| 脚注 / 尾注 | footnote、page footnote、endnote | 可建立脚注关系并翻译 | 脚注标记和正文引用需要坐标/编号对应 |
| 参考文献 | reference、bibliography | 可按策略保护、翻译或仅翻译标题 | 参考文献中的 DOI、URL、引文链接不能破坏 |
| 侧栏 / 引文 / 注释 | aside、quote、annotation | 需要独立样式和阅读顺序 | 容易被归入普通正文 |
| 分隔线 / 装饰图形 | 部分后端可输出 bbox 或图片 | 通常保留原图形 | 不是文字语义，不能期待 OCR 帮助其跟随重排 |
| 旋转文本 / 多方向文本 | 旋转角度、bbox | 需要保留方向和局部坐标 | OCR 读对文字不代表重建位置正确 |

当前 BabelDOC 已使用或兼容的标签包括 `title`、`text`、`author`、`reference`、`figure`、`figure_caption`、`table`、`table_text`、`table_caption`、`code`、`code_caption`、`header`、`footer`、`page_number`、`page_footnote`、`aside_text`、`formula`、`isolate_formula` 和 `fallback_line`。这说明当前适配层已经有类别契约，但 `fallback_line` 数量过高时，模型输出没有真正改变段落结构；应把覆盖率作为验收指标，而不是只检查 API 成功。

## 是否包含目录

### 印刷目录页

**可以识别其视觉内容，但不能保证输出为目录树。** 目录页通常由标题、编号、点引导线、页码和若干缩进层级组成。MinerU 可能输出为标题加文本行、列表，或者多个普通文本块。即使阅读顺序正确，Markdown 中的 `1. Introduction ...... 3` 也只是文字；它不会自动变成“章节标题到页码”的可验证目录对象。

因此 BabelDOC 应做一个独立的 TOC-page detector：结合页首标题（Contents / Table of Contents / 目录）、重复的页码尾部、点引导线、缩进层级、编号模式和后续正文标题，输出 `toc_entries[]`。目录页的翻译只修改显示文本，不能让模型改变 entry 数量、层级、目标页和锚点。

### PDF 交互式目录

**不应假设 MinerU 会保留或重建 PDF Outline。** 书签存放在 PDF Catalog/Outline 对象中，和页面文字及 OCR 结果是不同层次的数据。BabelDOC 必须在解析开始时直接读取：

- 书签标题、层级、目标页、目标坐标、打开状态；
- 目标是页内位置、外部 URI 还是命名目标；
- 原 PDF 页数和页面 ID；
- 翻译后是否仍使用同一页面，或发生插入/删除/拼页。

重建时应通过 `source_page_id -> output_page_id` 映射恢复书签，再按段落或标题的旧新坐标映射调整页内目标。对于 dual PDF，左右页面的书签策略要显式定义，不能只复制原页码。

## 超链接与其他必须单独保存的 PDF 对象

解析层应同时保存一份不可由 OCR 重建的对象快照：

- `/Link` 注释的矩形、页面、URI、GoTo 目标、命名目标和外部文件目标；
- 文本层字符与链接矩形的覆盖关系；
- 书签/Outline；
- 页内命名目标、注释、表单域、附件和图层（若产品范围包含）；
- 页面 mediabox/cropbox、旋转角度、字体资源和 XObject 引用。

链接重定位不能只用“在译文中搜索原链接文字”。建议按以下优先级：

1. 若链接覆盖的字符未移动，沿用原矩形；
2. 记录每个源字符或源 span 到译文 glyph/span 的对应关系，用旧 bbox 到新 bbox 的仿射或分段映射计算新矩形；
3. 对 URL、DOI、引用编号使用不可翻译 token 作为辅助锚点；
4. 无法可靠映射时保留原链接并输出 `link_unresolved`，阻止静默生成错误跳转。

## MinerU、百度与 PaddleOCR 横向比较

| 方案 | 强项 | 目录/书签 | 超链接 | 表格/公式/图片 | 部署与成本 | 适合 BabelDOC 的角色 |
|---|---|---|---|---|---|---|
| **MinerU** | 面向 PDF/文档的版面解析、阅读顺序、Markdown/JSON、公式和表格抽取；开源生态，便于本地化和调试 | 印刷目录可作为视觉内容识别；PDF Outline 需自行读取 | 不能作为 PDF `/Link` 保真层；需自行保留注释 | 对论文、扫描件和复杂版面较有针对性；图内文字仍需独立 OCR/视觉处理 | 可本地部署，也有云端能力；模型、GPU、版本兼容需要维护 | **首选候选结构层**，尤其适合提供 layout、reading order、table/formula 候选 |
| **百度智能文档解析 / 文档 OCR** | 云端 API，覆盖 doc/pdf/图片/xlsx 等多种格式；宣传能力包括版面、表格、阅读顺序、标题层级、旋转角度和 Markdown 输出，多语言支持 | 宣传的标题层级和阅读顺序有帮助；仍不能替代 PDF Outline，目录页是否输出树需实测 | 一般不能保证保留源 PDF 注释、目标和矩形；必须自行抓取原 PDF 对象 | 多格式接入方便，中文、扫描件和企业文档场景有优势；具体公式/图内文字能力依 API 版本和参数实测 | 云服务、按量计费、配额和数据出境/隐私约束；黑盒版本变更风险 | **中文扫描件和企业文档的对照基线或降级服务**，不作为唯一保真源 |
| **PaddleOCR / PP-Structure / PaddleOCR-VL** | 开源 OCR、版面分析、表格识别、公式识别和文档视觉语言模型生态；可拆分部署和训练 | 可推断标题/阅读顺序，不能原生代替 PDF Outline | 同样不是 PDF 注释保真工具 | 适合自建、定制、中文场景和离线处理；需要自己拼 pipeline | 本地可控，工程维护成本较高；模型组合和显存需求需评估 | **本地 OCR/扫描件补充层、MinerU 的替代或 fallback** |
| **原生 PDF 解析（PyMuPDF + 现有 BabelDOC parser）** | 字符坐标、字体、矢量、页面对象、书签、链接和注释 | **最可靠的源数据** | **最可靠的源数据** | 语义理解较弱，扫描件没有文字层 | 本地、可审计、成本低；复杂排版需要自行开发 | **必须保留的保真基线和对象层** |

百度文档解析的公开描述明确提到：可处理 18 种格式，并输出版面、表格、阅读顺序、标题层级、旋转角度，支持多语言和 Markdown。这个能力适合拿来和 MinerU 做同一批样本的结构准确率对比；“识别准确率 90%+”属于服务宣传指标，不能直接当作本项目的目录、链接或版式保真率。

## 建议的 pipeline 目标架构

```text
原始文件
  ├─ PDF 对象快照：字符、字体、图片、Link、Annotation、Outline、页面几何
  ├─ 原生解析：可编辑 PDF 的真实文字和坐标
  ├─ MinerU / 百度 / PaddleOCR：版面、阅读顺序、表格、公式、扫描文字候选
  ├─ 结构融合器：按 bbox、字符覆盖率、页面 ID 和置信度合并
  │    ├─ heading hierarchy
  │    ├─ toc page + toc entries
  │    ├─ reading order
  │    ├─ table/figure/formula/code regions
  │    └─ link/bookmark associations
  ├─ 结构化 IR：每个块有 source object id、bbox、类别、父子关系、阅读序号
  ├─ 翻译：只允许修改可翻译 span；保护 token、URL、引用编号和对象 ID
  ├─ 对齐写回：源 span → 译文 span/glyph 的确定性映射
  └─ 重建与验证：文字、版面、链接、书签、页数、目录树逐项验收
```

关键设计原则：

- **模型输出是候选，不是最终真相。** 保留模型原始 JSON，融合决策可复现、可审计。
- **块类别和对象类别分开。** `toc_page`、`toc_entry`、`bookmark`、`link` 不应被压成 `text`。
- **翻译输入和重建输入分开。** Markdown 只服务翻译；重建必须使用带源坐标、对象 ID、样式和关系的 IR。
- **所有保护对象都要有计数和哈希。** 翻译前后比较 Link 数、URI 集合、Outline 节点数、目录 entry 数、公式 token 数、表格数量。
- **低置信度要阻断或降级。** 目录页识别、跨栏顺序、链接映射无法确认时，应输出人工复核状态，而不是静默接受错误 PDF。

## 推荐的评测集与指标

至少选取以下样本：可编辑双栏论文、带 PDF 书签的长报告、印刷目录页、扫描 PDF、复杂表格、公式密集论文、图中含文字的手册、含大量 DOI/交叉引用的文档。每个样本保留原 PDF 和人工标注，不以单一翻译结果判断解析器。

| 指标 | 计算方式 | 发布门槛建议 |
|---|---|---|
| 块类别准确率 | title/text/table/figure/formula/header/footer/reference 等按 bbox 的 precision/recall | 关键类别分别统计，不能只报总准确率 |
| 阅读顺序准确率 | 块顺序与人工序列的 Kendall tau 或 pairwise accuracy | 双栏、脚注、跨页单独报告 |
| 目录页识别 | toc page precision/recall；entry 数、层级、页码和目标匹配率 | 任何 entry 丢失或错跳都应暴露 |
| PDF Outline 保真 | 节点数、层级、标题、目标页和坐标前后比对 | 目标错误为阻断级问题 |
| 链接保真 | URI/目标集合、Link 数、覆盖文字、矩形命中率 | URI 丢失或跳转错误为阻断级问题 |
| 表格结构 | 行列数、合并单元格、单元格文字和顺序 | 不能只比较 Markdown 视觉相似度 |
| 公式保真 | 公式数量、占位 token、LaTeX/图片对应关系 | 翻译不得破坏公式对象 |
| 重建版式 | 页数、溢出、重叠、字体、裁剪框、渲染差异 | P0/P1 几何问题必须为零 |

## 最终建议

第一阶段不要立即替换现有解析器。用同一评测集并行跑原生解析、MinerU、百度文档解析和 PaddleOCR，产出结构 diff。重点先回答四个问题：

1. MinerU 是否显著降低 `fallback_line`，并正确识别标题、目录行、表格和参考文献；
2. 百度是否在中文扫描件上明显优于 MinerU，优势是否足以抵消云端成本和数据限制；
3. 两者是否会丢失或误造 Link/Outline；如果会，继续由原生 PDF 对象层负责；
4. 哪些类别可以进入翻译，哪些类别必须保留原始内容或需要人工复核。

在没有完成对象层快照、源译文 span 对齐和目录/链接验证前，不应把任何 OCR 服务的 Markdown 直接接入 PDF 重建。优先实现 `toc_page/toc_entry`、`bookmark`、`link` 三类独立 IR，以及基于源字符坐标的链接重定位；这比单纯更换翻译模型或 OCR 模型更可能解决当前报告中的格式错误。

## 参考资料

- MinerU 项目主页与文档：<https://github.com/opendatalab/MinerU>
- MinerU 文档：<https://opendatalab.github.io/MinerU/>
- MinerU 输出文件说明：<https://opendatalab.github.io/MinerU/reference/output_files/>
- 百度智能文档解析：<https://cloud.baidu.com/product/ocr/doc_parser>
- 百度 OCR 文档解析 API 文档：<https://cloud.baidu.com/doc/OCR/s/llxst5nn0>
- PaddleOCR 版面分析：<https://www.paddleocr.ai/latest/en/version3.x/module_usage/layout_analysis.html>
- PaddleOCR 公式识别：<https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/formula_recognition.html>
- BabelDOC 当前翻译 pipeline：[`skills/document-translate/reference/pipeline.md`](../skills/document-translate/reference/pipeline.md)
