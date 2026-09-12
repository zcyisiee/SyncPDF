# BabelDOC 原始解析流程与改进方案

> 这份文档回答两个问题：BabelDOC 原本怎样把 PDF 从底层对象逐步组织成行、段落和翻译单元；在已经拥有 MinerU 这类更强文档结构模型之后，哪些部分应该保留，哪些部分需要重新设计。

## 结论先行

BabelDOC 的基本判断是正确的：PDF 通常没有“段落”这个对象，解析器必须从页面内容流中还原文字出现的位置，再逐级构造字符、行、段落和样式结构。

但需要准确区分三个层次：

1. **PDF 原生解析器**读取的是 PDF 操作符和资源，产出字符、字体、坐标、图形和 XObject 等底层对象。
2. **layout model**识别页面上的区域类型，例如标题、正文、表格、图片、公式、页眉和页脚。它告诉系统“这片区域是什么”，通常不负责输出 BabelDOC 最终使用的字符，也不直接构造可翻译段落。
3. **ParagraphFinder**使用字符坐标和 layout 区域把字符分组、拆行、合并或切分，才形成当前 IL 中的 `PdfLine` 和 `PdfParagraph`。

因此，MinerU 的价值不是简单替换 `ParagraphFinder`，而是提供更好的区域层级、阅读顺序和语义块。要发挥这些信息，BabelDOC 需要把当前的“版面框标签”升级为“文档结构 IR”，同时保留原始 PDF 字符和对象层作为重建真源。

当前用户提供的结果已经暴露出这个缺口：目录页的内容最终进入了 `P02-002` 这样的 `layout_label=text` 段落，翻译视图只看到一整段文字；当前 MinerU 适配器只把 MinerU 叶子块转换为 `bbox + class_name`，没有把 `lines`、`spans`、层级、阅读顺序和目录语义传入 IR。即使模型本身识别到了更多信息，下游也无法使用这些信息。

## 一、需要区分的“原始 BabelDOC”

仓库里同时存在历史解析入口、当前产品入口和 agent 工具入口。它们共享 IL 和后续处理逻辑，但底层解析器有所变化。

### 历史入口：PDF 内容解析器直接驱动 ILCreater

较早的流程由以下组件组成：

```text
PDFParser
  → PDFDocument / PDFPage
  → PDFPageInterpreterEx
  → TranslateConverter
  → ILCreater
  → Document IL
```

`PDFPageInterpreterEx`解析页面内容流和资源；`TranslateConverter.render_char()`接收每个文字 glyph，创建带位置和字体信息的中间字符；`ILCreater.on_lt_char()`把它转换成 `PdfCharacter`。同一个 interpreter 还处理曲线、图片、Form XObject、裁剪路径和图形状态。

当前仓库仍保留这条路径用于兼容和 parse-only 工具，入口在 [`babeldoc/format/pdf/legacy_parse.py`](../babeldoc/format/pdf/legacy_parse.py) 和 [`babeldoc/format/pdf/converter.py`](../babeldoc/format/pdf/converter.py)。

### 当前产品入口：native parser 投影到相同的旧 IL

当前主翻译流程在 `_do_translate_single()` 中先准备 PDF，然后调用 native parser：

```text
原始 PDF
  → 修复临时 PDF
  → PreparedPdfPage / resource tree
  → native tokenizer
  → TextContentInterpreter
  → TextRunEvent / PathPaintEvent / XObject event
  → NativeTextRunPositioner
  → ActiveILCreater
  → legacy-shaped Document IL
```

关键代码在 [`babeldoc/format/pdf/high_level.py`](../babeldoc/format/pdf/high_level.py)、[`babeldoc/format/pdf/new_parser/native_parse.py`](../babeldoc/format/pdf/new_parser/native_parse.py)、[`babeldoc/format/pdf/new_parser/page_content_execution.py`](../babeldoc/format/pdf/new_parser/page_content_execution.py)、[`babeldoc/format/pdf/new_parser/text_positioning.py`](../babeldoc/format/pdf/new_parser/text_positioning.py) 和 [`babeldoc/format/pdf/document_il/frontend/il_creater_active.py`](../babeldoc/format/pdf/document_il/frontend/il_creater_active.py)。

native parser 的目的主要是更可靠地解释 PDF 内容流和资源，最终仍然把字符等对象投影到当前 IL。后续 `LayoutParser → ParagraphFinder → StylesAndFormulas → ILTranslator → Typesetting → PDFCreater` 的总体形态没有改变。

### agent 工具入口

`skills/document-translate` 在产品解析阶段复用 native parser 和上述中端处理，然后从 `PdfParagraph` 生成 Markdown、sheet 和状态文件。相关代码在 [`babeldoc/tools/agent/workflow.py`](../babeldoc/tools/agent/workflow.py) 和 [`babeldoc/tools/agent/markdown_view.py`](../babeldoc/tools/agent/markdown_view.py)。

agent 的“整篇 Markdown 翻译”改善了模型上下文，但它仍然按照一个段落一个 ID 写回当前页内的 `PdfParagraph`。因此它目前是**全篇翻译视图**，不是**全篇版面重排模型**。

## 二、从 PDF 到字符对象

### 1. PDF 的真实输入是内容流和资源

PDF 页面通常由以下部分组成：

- 页面字典：MediaBox、CropBox、Rotate、Resources、Contents；
- 内容流：例如 `q`、`cm`、`BT`、`Tf`、`Tm`、`Tj`、`TJ`、`ET`、`Do`、`m`、`l`、`re`、`f` 等操作符；
- Font 资源：编码、CMap、ToUnicode、字体宽度、字体文件和字体矩阵；
- XObject：图片、Form XObject 和嵌套资源；
- 页面级注释、Link、Outline、命名目标等对象层数据。

内容流本身通常没有“这是标题”“这是正文”“这是同一段”的信息。即使一条 `Tj` 操作看起来像一句话，也可能只是一个局部字串；同一行也可能被拆成许多 `TJ` 操作，或者文字被放在不同的 Form XObject 中。

### 2. 解析操作符并维护 PDF 状态

parser 逐个解释操作符，并维护：

- 当前变换矩阵 CTM；
- Text Matrix 和文本行矩阵；
- 当前字体和字号；
- 字符间距、单词间距、水平缩放、rise、leading；
- 图形状态、颜色、线宽、裁剪路径；
- 当前页面和 XObject 层级。

例如 `TJ` 数组中的数字不是字符，它表示下一段文字的位移；一个负数可能让字符靠近，一个正数可能造成视觉空隙。不能只按字符串拼接文字，还要把文字坐标和位移保存下来。

当前 native parser 把文字操作组织成 `TextRunEvent`，字段包括 text segments、text matrix、line matrix、CTM、font name、font size、字符间距、单词间距、水平缩放和 XObject 路径。`NativeTextRunPositioner`再逐个解码 CID，计算字符矩阵、位置和 advance。

### 3. 最小文字单元是 glyph occurrence

IL 中的 `PdfCharacter` 更准确地说是一次 glyph 出现，而不是天然的 Unicode 字符。它通常包含：

| 字段 | 含义 |
|---|---|
| `char_unicode` | 通过字体编码/CMap/ToUnicode 得到的文本；失败时可能是 `(cid:N)` |
| `pdf_character_id` | 写回原字体时使用的 CID 或字符编码 |
| `box` | PDF 字体或内容流推导出的实际框 |
| `visual_bbox` | 更接近视觉墨迹的框，用于空间判断 |
| `advance` | 文本状态计算出的推进距离 |
| `pdf_style` | 字体、字号、图形状态 |
| `xobj_id` | 字符所在的 Form XObject 层级 |
| `render_order` | 页面绘制顺序，用于重建层叠关系 |
| `vertical` | 是否为竖排文字 |
| `formula_layout_id` | 是否位于公式版面区域 |

这一步还会解析字体的 bbox、字体子类型、Type3 资源、CMap 和字体特征，供后续字体映射、公式处理和 PDF 写回使用。

### 4. 空格不能只相信字符层

PDF 中的视觉空格经常不是一个真实空格 glyph，而是两个字符之间的横向间距。因此 BabelDOC 在后续阶段使用 `add_space_dummy_chars()`：根据字符之间的距离估计中位间距，在必要处插入没有原始 CID 的 dummy `PdfCharacter`，其 `char_unicode` 为空格，`advance` 为该空隙宽度。

这一步是翻译和重排的必要信息，但也有边界：

- 列间距、目录点引导线、对齐数字可能被误判为空格；
- `TJ` 位移和真实空格的语义不同；
- 跨栏或不同文本框的字符不能直接按横向距离插空格；
- 公式中的空格有时属于公式本身，不能当普通正文空格。

### 5. 非文字对象同样进入 IL

除了 `PdfCharacter`，IL 还保存：

- `PdfCurve`：线段、贝塞尔曲线、矩形、填充和描边；
- `PdfForm`：图片或 Form XObject 的位置、矩阵和图形状态；
- `PdfXobject`：嵌套资源、字体和原始操作；
- `PdfFigure`：图像或图形区域；
- 页面基础操作和每个对象的 passthrough graphic state。

这些对象决定了翻译后哪些原始元素可以直接透传，哪些元素需要跟随文本一起移动。公式内的曲线和 Form 也会在后续被挂到 `PdfFormula` 下。

## 三、layout model 在原始流程中的真实作用

### 1. baseline layout model 输出什么

原生 `DocLayoutModel` 的主要路径是：

```text
PDF page
  → raster image
  → ONNX object detector
  → boxes / confidence / class names
  → PDF 坐标系 PageLayout
```

[`babeldoc/docvision/doclayout.py`](../babeldoc/docvision/doclayout.py) 会把页面渲染为图片，模型预测区域框，再把图片坐标转换为 PDF 坐标。图片坐标一般以左上为原点，而 IL 使用 PDF 坐标，所以转换过程中会反转 y 方向。

模型输出的是类似下面的区域信息：

```json
{
  "id": 12,
  "box": {"x": 48, "y": 685, "x2": 571, "y2": 742},
  "conf": 0.98,
  "class_name": "title"
}
```

它本身不提供可靠的字符字符串、PDF CID、字体资源或 Link 对象。

### 2. layout model 不等于 OCR，也不等于段落模型

原始 BabelDOC 的 layout model 主要回答：

- 这个区域是不是文本区域；
- 它更像标题、正文、表格、图片、公式、页眉还是页脚；
- 字符是否应该进入可翻译文本，还是保留原样；
- 公式、图表和表格区域需要怎样保护。

它没有直接回答：

- 当前区域包含几段；
- 一段文字是否跨页；
- 两个区域是否属于同一个逻辑章节；
- 目录行的标题、层级、页码和目标是什么；
- PDF `/Link` 注释和文字之间的对象关系是什么。

这些是当前系统由 ParagraphFinder、规则和 PDF 对象层分别处理的内容。

### 3. fallback line 的含义

layout model 覆盖不足时，`LayoutParser` 调用 `extract_char.process_page_chars_to_lines()`，从字符 bbox 生成 `fallback_line` 区域。它大致包含以下步骤：

1. 按字符在副轴上的重叠创建 band；
2. 在 band 内使用 DBSCAN 聚类主轴位置，形成候选行；
3. 对过高或过宽的候选行再次聚类，拆分可能混在一起的列或行；
4. 根据字符间距插入文本空格；
5. 合并重叠或相邻的行；
6. 按页面位置排序，生成 fallback 行框。

这些 fallback 行是**区域框**，不是最终的 `PdfLine`。它们在下一步仍会被字符重新命中。

当前 `DocLayoutModel.provides_complete_layout` 默认为 `False`，所以原生模型会增加 fallback 行。当前 MinerU 适配器将该属性设为 `True`，于是不会再追加 fallback 行。这只表示“适配器声明 MinerU 已经覆盖页面上的文本区域”，不表示下游已经使用 MinerU 的逻辑段落、行顺序或跨页信息。

## 四、ParagraphFinder 如何构造行和段落

### 1. 用 R-tree 建立区域索引

`ParagraphFinder.process_page()` 首先为页面的 `page_layout` 建立空间索引。对每个字符，`get_character_layout()`使用字符的 `visual_bbox` 查询相交区域，计算字符框落入区域的比例，并按照预设优先级选择一个 layout。

优先级的作用是处理重叠区域。例如公式、表格文字、图注、标题和正文区域可能相互覆盖，系统会优先选更具体的类别。当前代码还允许受保护标签获得更高优先级，以便图内文字、参考文献、表格内部等内容保持原文。

这里有一个重要限制：**layout 区域是按空间命中的，不是按 MinerU 的 block ID 命中的。** 一个 block 中的字符只要空间相交，就可能被分配到相同 layout；两个相邻 block 如果标签或框边界不稳定，也可能被拆开或合并。

### 2. 按 layout id 和 XObject 连续分组

当前 `_group_characters_into_paragraphs()`按页面字符流顺序遍历字符，遇到以下条件时开始一个新 `PdfParagraph`：

- 当前字符和上一个字符命中的 layout id 不同；
- 当前字符来自不同的 XObject；
- 当前字符是段落开头的项目符号；
- 某些特殊小字符或高度不可靠字符触发例外处理。

新段落保存：

```text
layout_id
layout_label
debug_id
pdf_paragraph_composition = [PdfParagraphComposition(pdf_character=...)]
```

不属于可识别文本区域的字符不会被翻译，它们会留在 `page.pdf_character` 中，之后由 PDFCreater 继续渲染。当前 MinerU 模式的保护标签会允许某些区域进入段落结构，以便它们在结果中保留原文，但这些区域随后会被翻译选择层跳过。

这解释了为什么 layout model 不直接产生段落：模型只产生 `PageLayout`，ParagraphFinder 才把连续命中同一区域的字符拼成段落候选。

### 3. 在段落内部拆分行

一个初始段落可能包含多行。`_split_paragraph_into_lines()`使用字符的垂直 bbox 做扫描：

1. 计算段落内所有字符的有效 y 范围；
2. 以约 0.25pt 的步长从上到下扫描；
3. 统计每个扫描位置穿过多少字符；
4. 字符数为零的位置被视为行间隙；
5. 以这些间隙为分隔，将字符分配给多行；
6. 每一行变成一个 `PdfLine` 组合。

因此当前行识别主要依赖字符几何，而不是 layout model 的“行”字段。对于高质量 PDF，这种方法可以恢复普通段落；对于双栏、目录点引导线、表格、脚注交错和跨文本框内容，准确性会下降。

当前实现中行内字符通常保留 parser 产生的顺序，相关的显式按 x 排序代码被关闭过。这个选择可以保留复杂 PDF 的绘制顺序，却也意味着：如果 PDF 内容流顺序与视觉阅读顺序不一致，单纯的字符聚类无法自行修复。

### 4. 清理空行和尾随空格

`process_paragraph_spacing()`会：

- 删除完全空白的行；
- 删除行首没有前置内容的空格；
- 删除行尾空格；
- 重新计算行和段落 bbox。

然后 `add_space_dummy_chars()`会补上由几何间距推断的空格。

### 5. 短行和目录点引导线启发式

`process_independent_paragraphs()`会尝试从一个多行段落中切分独立段落：

- 如果前一行宽度明显低于全页行宽中位数，则认为这里可能是段落边界；
- 如果下一行以项目符号开头，则切分；
- 如果一行包含至少 20 个连续句点，则认为它可能是目录项的点引导线，并在此处切分。

这个规则只能识别“像目录”的视觉行，不能建立真正的目录对象。它存在几个问题：

- 目录中的点可能被 PDF 编码为空格、单独 glyph 或多个短间距；
- 目录第一行可能是 `1 Introduction 4 2 Architecture 7`，不含 20 个连续句点；
- 多列目录和数字对齐会被视为普通正文；
- 切分后仍然只是多个 `PdfParagraph`，没有 entry 层级、目标页或内部锚点；
- 如果上游 MinerU block 被压成一个 `text` 区域，ParagraphFinder 没有足够信息恢复原始列表结构。

当前样例的 `agent/anchors.json` 中，目录页的主要内容进入了 `P02-002`，其 `layout_label` 是 `text`，并且一个记录包含多条目录行。这不是翻译模型的问题，而是当前“区域标签 → 字符分组 → Markdown 行”的结构损失。

### 6. 段落后处理

ParagraphFinder 还会执行：

- 合并被行号交替切开的正文段落；
- 调整相互重叠的段落 bbox；
- 设置段落 render order；
- 在 OCR workaround 模式下给文字区域添加白色背景，并清理原始文字层。

这些处理都是页面级的。`ParagraphFinder.process()`对每一页独立执行，不维护跨页段落状态。

## 五、StylesAndFormulas 如何把段落变成可翻译结构

### 1. 公式识别

`StylesAndFormulas`会在段落行内重新分类字符。公式候选来自：

- layout model 标记的 `formula` 区域；
- 公式字体名和字体族；
- 数学符号、希腊字母、特殊字符；
- 字符 bbox 和 visual bbox 明显不一致；
- 小字号上标/下标；
- 竖排字符；
- 公式开始和公式中间的状态机。

连续公式字符会成为 `PdfFormula`。公式的曲线、Form 和其他元素会通过 bbox/IoU 关联到公式，使它们在重排时可以一起移动。

数字、空格和某些逗号组成的简单“公式”可以重新视为普通文本；复杂公式则作为受保护的不可翻译对象。

### 2. 样式分组

系统计算段落中普通文字样式的交集，得到段落基准 `PdfStyle`，再把连续同样式字符合并为 `PdfSameStyleCharacters`。

基准样式包括：

- 字体 ID；
- 字号；
- graphic state；
- 颜色和其他可透传状态。

如果段落中有加粗、斜体、不同字号或不同颜色，样式分组会保留这些组合，供翻译时通过样式占位符恢复。

### 3. 这一层的结构边界

StylesAndFormulas 处理的是**段落内部的成分**，例如：

```text
文本 run → 公式 → 文本 run → 斜体文本 run
```

它不负责判断两个页面上的段落是否属于同一个逻辑段落，也不负责生成标题层级和目录树。

## 六、ILTranslator 如何生成翻译单元

### 1. 普通段落通常独立翻译

当前 `ILTranslator.translate()`遍历每个页面的 `pdf_paragraph`，为每个段落提交一个翻译任务。翻译任务的输入是段落的 `unicode` 和必要的占位符。

这意味着当前原生翻译单元实际上是：

```text
一个页面内的一个 PdfParagraph
```

它不是一个跨页的逻辑段落，也不是一个完整章节。

### 2. 占位符保护公式和富文本

多成分段落会被编码为类似：

```text
英文文本 {v1} <style id='1'>styled text</style> 后续文本
```

具体实现使用公式占位符和富文本左右边界占位符。翻译结果解析时：

- 公式占位符恢复原 `PdfFormula`；
- 样式占位符恢复原字符样式，或生成新的 Unicode 文本组合；
- 普通翻译文本成为 `PdfSameStyleUnicodeCharacters`；
- 占位符顺序和数量被检查。

这个设计对公式和样式很有价值，应当保留。但占位符只解决段落内部的成分对应，不解决跨页面 span 对应。

### 3. agent 全文 Markdown 的实际能力

agent 工具会把多个段落按照页面顺序拼成连续 Markdown，并用 `<!-- id=... -->` 标记每个段落。这样翻译模型可以看到全文上下文、术语和标题序列。

但是写回仍然是：

```text
Markdown id
  → translated.jsonl
  → 原来的 PdfParagraph
  → 当前页的 Typesetting
```

因此“模型看到全文”与“排版器可以跨页重排”是两件不同的事。当前实现可以改善上下文和术语一致性，但不能自动把两个页片段合成为一个可跨页流动的 paragraph object。

## 七、Typesetting 和 PDF 重建的边界

### 1. TypesettingUnit

翻译后的段落被转换为 `TypesettingUnit`。一个 unit 可能是：

- 原始 PDF 字符，可直接透传；
- 原始公式，可直接透传并带着关联曲线/Form 移动；
- 原始同样式字符；
- 使用映射字体生成的新 Unicode 字符。

如果所有 unit 都可以透传，系统尽量保留原始位置。如果包含新译文，就进入排版。

### 2. 当前排版算法

当前排版器以原段落 bbox 为边界，从左上方开始放置 unit：

1. 计算每个 unit 的宽高；
2. 按当前字号和语言规则计算换行；
3. 遇到行宽不足时换行；
4. 遇到段落底部不足时降低 scale；
5. 必要时扩展段落下方或右侧空间；
6. 仍然无法放下时继续降低 scale，最后尝试放宽英文断行规则。

它还处理：

- 中英文混排间距；
- 中文行距；
- 首行缩进；
- 悬挂标点；
- 不允许出现在行尾的标点；
- 公式和图形的同步移动。

### 3. 当前排版器不会做全局分页

`Typesetting` 接收的是一个 `Page` 和其中的一个 `PdfParagraph`。它可以在该段落框内压缩或局部扩展，但不会：

- 将一个段落从页面底部流到下一页；
- 将后续段落整体向后分页；
- 在多个栏区域之间进行全局排版；
- 根据 MinerU 的逻辑 block 重新计算整份文档的页布局；
- 让一个跨页逻辑段落共享一份翻译结果和一组全局 glyph/span 对齐关系。

这是当前系统支持“跨页整体翻译”时必须面对的架构变化。仅把 MinerU 的段落文本一次性发给模型，不能解决这个问题。

### 4. PDFCreater 做什么

PDFCreater 把 IL 中的对象转换回 PDF 内容流：

- 对每页建立新的绘制指令；
- 渲染翻译后的字符；
- 透传原始字符、图形状态、曲线和 Form；
- 写回 XObject 内容流；
- 添加所需字体和编码；
- 生成 mono PDF；
- 用原文 PDF 和译文 PDF 生成 dual PDF。

当前产品还会在输出阶段处理部分 Link 和 TOC：源 PDF 的 Link 注释需要保留或重新插入，dual 的 `show_pdf_page()`不会自动复制注释和 Outline，因此需要额外搬运。这个逻辑属于 PDF 对象保真层，不属于 layout model。

历史版本中的 `fix_null_xref()`曾经直接清除页面注释，导致 Link 丢失；当前分支已经增加保留 Link 和迁移目录的逻辑。但当前链接重定位仍有按文本搜索的补救路径，不能保证所有重排场景的矩形都准确。

## 八、为什么 MinerU 现在还没有发挥完整能力

当前 [`babeldoc/docvision/mineru_doclayout.py`](../babeldoc/docvision/mineru_doclayout.py) 的关键适配过程是：

```text
MinerU layout.json
  → 遍历 para_blocks / discarded_blocks
  → 展开叶子 blocks
  → 读取 bbox 和 type
  → 映射为 BabelDOC class_name
  → 构造 YoloResult
  → LayoutParser 写入 PageLayout
```

它目前保留了：

- 页面编号；
- 叶子块 bbox；
- block type 到 BabelDOC label 的映射；
- conf（当前适配为 1.0）；
- 部分作者区、表格、图片、公式、代码等保护标签。

它目前丢失或没有进入 BabelDOC 主 IR 的信息包括：

- MinerU 原始 block ID/index；
- 父子 block 层级；
- `lines`；
- `spans` 和识别文本；
- block 内部阅读顺序；
- 列表项和嵌套列表关系；
- 表格单元格、合并单元格和行列结构；
- 公式识别结果和资源引用；
- 图片/图表与 caption 的关系；
- 跨页逻辑 block 的 continuation 信息；
- MinerU 原始置信度和版本信息。

所以当前 MinerU 模式的准确描述是：

> 用 MinerU 生成更丰富的页面区域框，再由 BabelDOC 原有字符层和 ParagraphFinder 继续恢复段落。

它不是：

> 直接使用 MinerU 的完整文档结构作为翻译和排版 IR。

## 九、建议的新 IR：把区域、片段、逻辑块分开

当前 `PageLayout → PdfParagraph` 是单层模型，无法表达目录项、列表、跨页段落和对象关系。建议引入一层独立于旧 IL 的结构 IR。

### 1. 原始对象层

解析一开始就保存不可由 OCR 重建的数据：

```text
SourceDocument
  ├─ source_sha256
  ├─ pages[]
  │   ├─ page_id / page_number
  │   ├─ mediabox / cropbox / rotate
  │   ├─ content_stream_refs
  │   ├─ characters[]
  │   ├─ fonts[]
  │   ├─ figures / forms / curves
  │   ├─ links[]
  │   └─ annotations[]
  ├─ outline[]
  ├─ named_destinations[]
  └─ attachments / metadata
```

每个字符都应该有稳定的 `source_char_id`，至少包含 page、xobject、render order、char index 和 bbox。这样后面可以把 MinerU block、翻译 span、译文 glyph 和 Link 矩形连起来。

### 2. 版面区域层

```text
LayoutRegion
  ├─ region_id
  ├─ page_id
  ├─ bbox
  ├─ label
  ├─ confidence
  ├─ source_provider: native / mineru / baidu / paddle
  ├─ parent_region_id
  └─ raw_provider_ref
```

`LayoutRegion`只表达页面上某个区域的类别和空间位置，不直接代表翻译单元。

### 3. 页面片段层

```text
BlockFragment
  ├─ fragment_id
  ├─ page_id
  ├─ region_id
  ├─ bbox
  ├─ source_char_ids[]
  ├─ source_lines[]
  ├─ visual_order
  ├─ column_id
  └─ continuation: previous / next / none
```

一个逻辑段落可以由多个页面片段组成。片段是重建和分页的基本单位，它保留自己的源坐标。

### 4. 文档逻辑块层

```text
LogicalBlock
  ├─ block_id
  ├─ kind: heading / paragraph / toc_entry / list / table / figure / formula / code
  ├─ fragments[]
  ├─ reading_order
  ├─ parent_block_id
  ├─ heading_level
  ├─ translation_policy
  ├─ source_spans[]
  └─ target_spans[]
```

`LogicalBlock`才是翻译上下文和文档结构层的对象。它可以跨页，但不直接替代页面片段。

### 5. 交互对象层

目录和链接必须有独立对象：

```text
TocEntry
  ├─ entry_id
  ├─ display_block_id
  ├─ level
  ├─ printed_page_label
  ├─ target_source_page_id
  └─ target_source_point

Bookmark
  ├─ bookmark_id
  ├─ level
  ├─ title_source
  ├─ target_source_page_id
  ├─ target_source_point
  └─ action

Link
  ├─ link_id
  ├─ source_page_id
  ├─ source_rect
  ├─ covered_source_char_ids[]
  ├─ uri / destination
  └─ target_rects[]
```

这样目录页的显示文字可以翻译，目录 entry 的层级和目标仍由确定性对象控制；PDF 书签可以保持原始目标并按页面映射写回；超链接可以按源字符/span 映射到译文位置。

## 十、MinerU 应如何真正接入

### 第一阶段：保留原始 JSON，不改变重建行为

MinerU 适配器首先应保存完整原始结果，例如：

```text
agent/source/mineru/layout.json
agent/source/mineru/version.json
agent/source/mineru/pages/P0001.json
```

每个输出区域保留 `raw_provider_ref`，便于在结果异常时回到原始 block。

这一阶段继续使用原生 PDF 字符作为文本真源，确保接入 MinerU 不会因为 OCR 文本替换而破坏字体、CID、公式和 Link。

### 第二阶段：不要把 MinerU 压成 YoloResult

可以继续兼容 `PageLayout`，但应同时生成完整的 provider IR：

```text
MinerU block tree
  → ProviderDocument
  → LayoutRegion + BlockFragment + LogicalBlock candidates
  → deterministic fusion
```

`YoloResult`只适合表示平面检测框，不适合承载层级文档结构。向后兼容可以保留 `PageLayout`，但翻译和目录模块应该读取新的结构 IR。

### 第三阶段：用 bbox 把 MinerU 行/span 对齐回原生字符

对于可编辑 PDF：

1. 取 MinerU block/line/span bbox；
2. 查询 bbox 内的原生 `PdfCharacter`；
3. 使用字符中心、覆盖率、阅读顺序和文本校验建立一对多映射；
4. 记录未匹配字符、重复匹配和低置信度；
5. 仍使用原生字符作为写回对象。

对于扫描 PDF：

1. MinerU/PaddleOCR 的识别文本成为 source text；
2. OCR glyph bbox 成为合成源字符；
3. 原始页面图片必须作为背景保留；
4. 翻译文本需要覆盖旧图像区域；
5. Link/Outline 仍从原始 PDF 对象层读取。

### 第四阶段：使用 MinerU 阅读顺序和父子关系

读取顺序应优先使用 provider 的 block 顺序和列结构，并由确定性规则校正：

- 页面级顺序：页码顺序；
- 栏级顺序：左栏到右栏或 provider 指定顺序；
- 栏内顺序：上到下；
- 图表/表格：caption 与 body 建立父子关系；
- 脚注：与正文分开，按脚注区域规则处理；
- 目录：单独识别 entry，而不是普通 text；
- 跨页：同一 provider block 或相邻 block 的 continuation 合并为一个候选逻辑块。

规则不能盲信 provider。每次融合都要记录：

```text
provider_order
native_order
final_order
order_confidence
order_conflicts[]
```

## 十一、跨页段落整体翻译的正确实现方式

### 1. 不要直接扩大 PdfParagraph 的页面范围

现有 `PdfParagraph` 的 bbox、composition 和 Typesetting 都假设对象属于一个页面。直接把多个页面的字符塞进一个 paragraph 会破坏坐标、字体、页面绘制和 PDFCreater。

更安全的方式是：

```text
一个 LogicalBlock
  ├─ page 1 BlockFragment
  ├─ page 2 BlockFragment
  └─ page 3 BlockFragment
```

翻译使用 `LogicalBlock` 的完整源文本；重建仍使用各个 `BlockFragment` 的页面坐标。

### 2. 最小可行版本：跨页整体翻译，分页边界固定

第一版可以不做全局分页，只实现：

1. 根据 MinerU/native 结构识别 page 1 和 page 2 属于同一段；
2. 将两个片段的源文本合并为一个翻译输入；
3. 模型只返回一个带 span token 的完整译文；
4. 根据源片段字符比例或明确的行边界，把译文切回两个 fragment；
5. 每个 fragment 在自己的原始区域内 Typesetting；
6. 如果某个 fragment 放不下，输出低置信度并进入 review。

这种方案可以改善上下文和术语一致性，但无法保证译文在页面边界处自然流动。

### 3. 完整版本：文档级分页和重排

要让译文真正跨页流动，需要引入 `FlowLayoutEngine`：

```text
LogicalBlock sequence
  → reading regions / columns / page constraints
  → translated inline units
  → page fragment allocation
  → new glyph positions
  → new page-to-block map
  → PDF render + object remap
```

它需要处理：

- 页面和栏的可用区域；
- 标题不可孤立；
- 段落不能覆盖图表、表格和脚注；
- 段落溢出时向下一个 flow region 延伸；
- 后续 block 整体后移；
- 原始图片、曲线、公式和表格的位置；
- 页码、页眉页脚和书签目标；
- mono 与 dual 的页映射。

这是排版架构升级，不是简单接入 OCR 模型。建议在固定边界版本稳定后再实现。

## 十二、当前最值得改进的地方

### P0：先修复结构损失和对象保真

#### 1. 引入完整 provider IR

当前 MinerU 适配只输出平面框。应保留原始 block tree、line、span、index、confidence 和资源引用，并建立 provider IR。

验收条件：

- 未知 block 类型不会静默变为普通 `text`；
- 每个 provider block 都有 raw reference；
- 叶子 block 和父 block 的关系可追踪；
- 解析报告能统计 provider block 数、融合 block 数、丢弃数和冲突数。

#### 2. 将目录从普通文本中独立出来

实现 `toc_page` 和 `toc_entry`：

- 目录页检测：Contents/目录标题、点引导线、页码、缩进和编号模式；
- entry 识别：标题文本、打印页码、缩进层级、点引导线区域；
- 目标匹配：与正文 heading 的文本、编号和相对位置匹配；
- 目录翻译：只翻译显示文本，entry 数量、层级和目标由程序保持；
- 低置信度：阻断静默生成，要求复核。

#### 3. 保存 Link 和 Outline 的源快照

在解析前直接获取：

- 每个页面的 `/Link` 注释；
- URI、GoTo、Named destination；
- 原始矩形和覆盖字符；
- Outline 标题、层级、目标页和目标坐标。

这些信息不应从 Markdown 或 OCR 结果反推。

#### 4. 链接重定位改为源字符映射

当前按文本搜索译文的方式只能作为 fallback。主路径应为：

```text
source link rect
  → covered source char/span IDs
  → target span/glyph IDs
  → target glyph bbox union
  → output link rect
```

如果映射不唯一或没有命中，报告 `link_unresolved`，不要把矩形留在可能指向错误文字的位置。

#### 5. 统一页码约定

当前 agent 工具存在 0-based 和 1-based 页码混用的风险。建议：

- 内部全部使用稳定 `source_page_id` 和 0-based index；
- 面向用户的报告统一使用 1-based display page；
- artifact schema 明确字段后缀，例如 `page_index` 和 `page_number`；
- 修复和审查使用 block/link/bookmark ID，不依赖人工页码。

### P1：让 MinerU 的结构真正控制阅读顺序

#### 6. 将阅读顺序从页面字符流中抽离

当前 ParagraphFinder 基本依赖 parser 输出顺序。应先产生全局 `reading_order`，再把字符匹配到 block fragment。这样可以处理：

- 双栏文章；
- 图注和图体；
- 侧栏；
- 脚注；
- 跨栏段落；
- 目录多列数字。

原生 parser 顺序和 provider 顺序冲突时，保留冲突并按置信度决定，不静默覆盖。

#### 7. 让标题、列表、目录和正文使用不同的结构类型

当前 `layout_label` 同时承担区域类别、翻译策略和 Markdown 表现，职责过多。建议拆分：

```text
kind: heading / paragraph / list_item / toc_entry / table / figure
policy: translate / protect / translate_caption / review
presentation: title / text / caption / code
```

这样“目录项被判为 text”不会直接导致它失去目录语义。

#### 8. 表格和公式使用 provider 结构，但保留原生绘制对象

表格可使用 MinerU/Paddle 的行列和单元格结构生成翻译输入；公式可使用识别结果帮助建立 source span；最终重建仍优先使用原始 PDF 的曲线、Form 和字符对象。

这能避免把识别错误直接写进原 PDF，同时允许后续逐步支持单元格翻译和公式语义重建。

### P2：升级为文档级排版

#### 9. 跨页 LogicalBlock 翻译

先实现固定分页边界的整体翻译，再实现可流动分页。两者都需要新的翻译协议：

- `block_id`；
- `fragment_id`；
- source span 数量和顺序；
- 公式、URL、引用号和 Link token；
- target span 到 fragment/glyph 的映射。

现有只按 `PdfParagraph` 的 Markdown ID 协议不足以表达这些关系。

#### 10. 文档级 FlowLayoutEngine

在完成对象快照、阅读顺序、结构 IR 和 span 映射后，再开发全局分页。否则排版器即使能把文字放到下一页，也无法可靠更新 Link、Outline、页码、dual 页映射和浮动对象。

#### 11. 视觉回归和结构指标同时验收

每次 parser 或模型升级都应比较：

- block/fragment 数量；
- 标题层级；
- 阅读顺序；
- 目录 entry 数、层级和目标；
- Link 数、URI 集合和矩形命中率；
- 公式和表格数量；
- 页数、溢出、重叠和字体缩放；
- 视觉渲染差异。

单独看 Markdown 或翻译质量无法发现结构和交互对象损失。

## 十三、推荐实施顺序

### Phase 0：固定当前行为和基准

选取以下样本：

- 用户提供的 51 页 DeepSeek 技术报告；
- 双栏论文；
- 带 PDF 书签的长文档；
- 有印刷目录但没有书签的文档；
- 扫描 PDF；
- 公式密集文档；
- 大量 DOI、交叉引用和 URL 的文档；
- 复杂表格和图内文字文档。

建立原始 PDF 对象快照和基准指标，尤其是 P02 目录、P07/P10/P27 图表、跨页正文和 Link 密集页。

### Phase 1：结构 IR 和对象快照

这一阶段不改变翻译结果，只增加：

- source page/char/object IDs；
- Link/Outline snapshot；
- provider raw JSON；
- LayoutRegion、BlockFragment、LogicalBlock 候选；
- 结构 diff 报告。

### Phase 2：MinerU 结构融合

- 保留 MinerU block tree；
- 使用 lines/spans/index；
- 对齐原生字符；
- 生成 reading order；
- 将目录和列表从普通 text 中分离；
- 与 native model 做 A/B 对比。

### Phase 3：目录和链接闭环

- 目录页和目录 entry 独立处理；
- Outline 按 source page/object 映射；
- Link 按 source char/span 映射；
- 对 unresolved 对象阻断或进入人工审查。

### Phase 4：跨页整体翻译

- LogicalBlock 级翻译；
- 固定 fragment 边界回填；
- 译文 span 与源 fragment 对齐；
- 对溢出和边界异常生成审查报告。

### Phase 5：全局分页和版式迁移

- Flow regions；
- 跨页重排；
- 标题/图表/脚注约束；
- 页码和书签重建；
- mono/dual 统一映射。

## 十四、应保留和应替换的部分

| 当前组件 | 建议 | 原因 |
|---|---|---|
| native PDF parser | 保留并继续强化 | 它掌握真实字符、字体、坐标、图形和 PDF 内容流 |
| `PdfCharacter` / 字体资源 | 保留 | 是精确写回和 span 对齐的基础 |
| `PdfCurve` / `PdfForm` / XObject | 保留 | 公式、图表、图片和原始绘制需要它们 |
| `LayoutParser` 平面兼容接口 | 短期保留 | 兼容现有 downstream 和 debug 工具 |
| `PageLayout` | 作为 projection 保留 | 适合渲染 debug 框，不足以承担完整文档结构 |
| `ParagraphFinder` 几何算法 | 保留为 fallback，逐步降级为校正器 | 对没有 provider 结构的 PDF 仍有用 |
| `StylesAndFormulas` | 保留并接入 provider hints | 原生字符和公式对象仍需要它来组织 |
| 占位符翻译协议 | 保留并扩展 | 公式、样式和保护 token 的经验成熟 |
| 当前 per-page `PdfParagraph` 翻译 | 逐步替换为 LogicalBlock 翻译 | 无法表达跨页整体翻译 |
| 当前局部 Typesetting | 保留为 fragment renderer | 需要增加文档级 flow allocator |
| Markdown 作为唯一翻译真源 | 降级为视图/交换格式 | 无法承载对象、层级、span 和页面映射 |
| 文本搜索式 Link remap | 降级为 fallback | 译文扩展、重复文本和目录页会导致误定位 |
| 点号目录启发式 | 保留为候选信号 | 不能继续作为目录的唯一识别机制 |

## 最终判断

用户对原始 BabelDOC 的理解大体正确，但应把“layout model 拼段落”改成更精确的描述：

> layout model 给出页面区域和类别；PDF parser 给出带坐标的最小文字和图形对象；ParagraphFinder 再根据区域命中、字符连续性和几何关系构造行与段落。

MinerU 可以显著提升这个流程，但前提是把它的结构信息完整接入。当前仅将 MinerU block 映射为 `PageLayout`，相当于只使用了它最浅的一层能力。

最优先的架构动作不是立即替换原生 parser，也不是让 OCR 文本直接覆盖 PDF，而是：

1. 保留原生 PDF 对象层；
2. 保存 MinerU 完整 block tree；
3. 增加 region/fragment/logical block 三层结构；
4. 独立建模目录、书签和 Link；
5. 建立 source char/span 到 target glyph 的确定性映射；
6. 先实现跨页整体翻译，再实现全局分页。

完成这些层次后，MinerU 才能从“更好的区域检测器”升级为“文档结构提供者”，而 BabelDOC 才有条件在保持 PDF 格式和交互对象一致的同时，支持跨页段落整体翻译。

## 代码索引

- 高层流程：[`babeldoc/format/pdf/high_level.py`](../babeldoc/format/pdf/high_level.py)
- 历史 PDF 解析入口：[`babeldoc/format/pdf/legacy_parse.py`](../babeldoc/format/pdf/legacy_parse.py)
- native parser 入口：[`babeldoc/format/pdf/new_parser/native_parse.py`](../babeldoc/format/pdf/new_parser/native_parse.py)
- 页面内容解释：[`babeldoc/format/pdf/new_parser/interpreter.py`](../babeldoc/format/pdf/new_parser/interpreter.py)
- 字符定位：[`babeldoc/format/pdf/new_parser/text_positioning.py`](../babeldoc/format/pdf/new_parser/text_positioning.py)
- IL 创建：[`babeldoc/format/pdf/document_il/frontend/il_creater_active.py`](../babeldoc/format/pdf/document_il/frontend/il_creater_active.py)
- layout model：[`babeldoc/docvision/doclayout.py`](../babeldoc/docvision/doclayout.py)
- MinerU 适配：[`babeldoc/docvision/mineru_doclayout.py`](../babeldoc/docvision/mineru_doclayout.py)
- layout 到字符的命中：[`babeldoc/format/pdf/document_il/utils/layout_helper.py`](../babeldoc/format/pdf/document_il/utils/layout_helper.py)
- 行/段落构造：[`babeldoc/format/pdf/document_il/midend/paragraph_finder.py`](../babeldoc/format/pdf/document_il/midend/paragraph_finder.py)
- 公式和样式：[`babeldoc/format/pdf/document_il/midend/styles_and_formulas.py`](../babeldoc/format/pdf/document_il/midend/styles_and_formulas.py)
- 翻译：[`babeldoc/format/pdf/document_il/midend/il_translator.py`](../babeldoc/format/pdf/document_il/midend/il_translator.py)
- 排版：[`babeldoc/format/pdf/document_il/midend/typesetting.py`](../babeldoc/format/pdf/document_il/midend/typesetting.py)
- PDF 重建：[`babeldoc/format/pdf/document_il/backend/pdf_creater.py`](../babeldoc/format/pdf/document_il/backend/pdf_creater.py)
- 当前 agent 流程：[`skills/document-translate/reference/pipeline.md`](../skills/document-translate/reference/pipeline.md)
