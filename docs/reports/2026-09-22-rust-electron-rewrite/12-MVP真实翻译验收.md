# Rust 真实翻译 MVP 验收

2026-09-23。目标是尽快给用户可查看、可复跑的后端样张；不做界面。完整功能目标仍未完成。

## 已接入

- `bdt rust-translate` 调用原有Rust sidecar和本机pi，默认deepseek/deepseek-flash、low；保留唯一bdt产品入口。
- PP-DocLayout-V3 Apple GPU、Markdown one-shot、首个闭合块立即排版、固定原字号、源内容保护与部分结果状态贯通。
- `--cached-from` 只读复制先前真实译文缓存；缓存仍按源文本/语言/协议校验，缺失与坏块回退，主请求/补救请求均为0。这样可独立重复验证排版，无需等待模型。
- 普通数字原子仅在单一样式/真实源范围明确且段落不与注释相交时按原文回填；公式、引用、URL原子继续保留。

## 真实运行历史

输入：`/Users/zhengcaiyi/Downloads/2106.04690v2.pdf`，23页。证据均在本仓库`tmp/backend-repair/`。

| 运行 | 结果 |
|---|---|
| `mvp-real-p1-3` | 63.10秒；主请求1/补救0；16成功、14原子回退、0溢出；首次typeset53.57秒，首页落盘56.17秒 |
| `mvp-real-full-v1` | 352.39秒；主请求1/补救10；90成功、72回退；首次typeset133.50秒，首页落盘136.10秒，翻译阶段351.94秒结束，已证明编译早于模型完成 |
| `mvp-real-full-v2` | 接数字后107段排版就绪，但278.38秒时最后补救响应出现unknown escape，4块未落定；仅最后已保存快照可用，不能把107段准备好当成107段均已发布 |

最终交付使用下一节的真实缓存重编译结果；v1/v2证据保留，没有改成通过。

## 工程检查

- 主控完整review并合入bdt候选`4fe625d7`，补PageReady的1基页号、产物IO错误、矛盾成功状态及中断子进程组处理。
- Rust相关3crate：269通过/0失败/5 ignored；pipeline普通数字专项包含141 unit通过，仍保护公式/引用/URL/注释相交/跨样式；cache-only验证有效缓存命中、坏身份拒绝、未命中保留原文、真实通道0调用。
- bdt入口、cache只读复制与单入口守卫：26 pytest通过；Ruff、fmt、strict Clippy通过。release及最终真实重编译结果另附。
- 日志：`tmp/backend-repair/mvp-runner/`。

## 当前限制

A3 dual、中文目录、链接点击框重建和逐块字体/字号编辑尚未交付。未能安置的公式/引用、源区域冲突及固定字号溢出仍保留原文。普通正文有中文翻译但全文仍为部分结果；不能将MVP写成完整论文翻译质量通过。动态库仍依赖本机开发环境，CLI指南有运行命令；打包分发未完成。

## 最终 MVP 交付

产物目录：`tmp/backend-repair/mvp-20260923/`，包含`translated.pdf`、`preview-3-pages.pdf`、`README.md`未成功块清单、事件/结果JSON与保留内容量测。

- 全23页完成保存、验证和发布；18.993秒，真实缓存158段，主请求0/补救0，未生成模拟译文。
- 107块成功排版并写入，55块回退（40原子、12固定字号溢出、3无有效译文；4个缓存未命中块中的1块已按原子回退计入）；另40源区域重叠和1旋转侧注保留，均显式提示。作者机构/脚注/图表/reference仍按保护策略保留。
- 62943个保留字符位置、字号、颜色核对，变化0；qpdf通过。主控目视首页中文标题/摘要与p7正文/表格旁文字；仍可见中英混排和留白，交由用户反馈，不宣称完整排版质量。
- 23个PageReady、0 error事件；RunFinished false / bdt exit1正确表示部分译文，不能改为完整成功。
- Rust相关269测试通过、strict Clippy/fmt/release通过；bdt26pytest与Ruff通过。

### 本机复跑

```bash
source tmp/backend-repair/codex-r1-integration/env.sh
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools rust-translate \
  /Users/zhengcaiyi/Downloads/2106.04690v2.pdf \
  --workdir tmp/rust-mvp-next --cached-from tmp/backend-repair/mvp-real-full-v1 \
  --layout-device coreml
```

去掉`--cached-from`即使用真实pi模型翻译。每次用新workdir；默认字号不缩小。后续优先按样张反馈修复阻碍阅读的公式/引用与源区域归属，再接dual/目录/链接和局部编辑；本报告不代表完整后端目标完成。

<a id="r4-citations-ink"></a>

## R4 首批：数字引用与行间碰撞误判（2026-09-23）

最新交付：`tmp/backend-repair/mvp-r4-20260923/translated.pdf`，同目录有前3页预览、未完成清单、JSON验证和重点页PNG；原MVP与中间版本`mvp-r4-citations-v1`均保留。仅主控执行，未启动旧worker，未做UI或p19专项。

- 原40个atom回退按实际源IR拆分：34段纯引用、3段引用+数字、3段纯数字；其中71个Other全是数字引用，12个Number，**没有Formula**。本论文源与目标PDF的链接注释均为0，不能据此声称链接重排通过。
- 只放行严格数字引用语法、完整连续源span、单一样式且注释不相交的原子；邮箱/URL/公式/跨样式/未知注释仍拒绝。先单独复排达到137写入/25回退。
- 再修行间碰撞：整行bbox交叠只作初筛，逐glyph实际墨迹框/原子矩形确认；非相邻行也检查。7段从误报overflow变为可写入，**其逐字形坐标、字号、行距、断行完全没变**。没有放宽0.01pt碰撞容差。
- 最终 **144块写入 / 18块回退**，比基线107/55多37块、0既有成功块退化；回填70个引用原子。11排版失败、3原子样式不可靠、4无有效缓存译文；另40保护重叠和1旋转侧注仍在，不隐藏统计。
- 全23页PageReady和最终发布；18.158秒，158段真实缓存命中、0主请求/0补救，0 error事件。RunFinished false / bdt exit1仍正确表示部分结果。
- 44,009个保留字符位置/字号/颜色变化0；独立从输出PDF量测14,186个非空白译文字形的位置/字号，缺失或变化0，源run字号不匹配0，行间墨迹框碰撞0；qpdf通过。已目视p1/p2/p5/p7/p18/p23，仍有中英混排、留白及缓存译文质量问题，不代表全篇质量通过。

工程验收：workspace **660 passed / 0 failed / 8 ignored**；额外显式执行真实缓存inventory测试1 passed（不把ignored算已执行）；相关4crate strict Clippy、fmt/release通过；bdt与单入口26 pytest通过。日志和完整red/green证据：`tmp/backend-repair/r4-text-atoms/`。最终量测：交付目录`delivery-verification.json`、`source-retention.json`。文档strict本轮仍失败：4处既有锚点告警（HTTP→pipeline、Rust参考→CLI、状态→handoff两处），未计通过；不扩大本批修复范围。

剩余11个排版拒绝的缓存几何诊断（可重叠）：5段有`∼`缺字，5段超安全frame，3段触发源障碍碰撞检查；不是都因译文太长。剩余3个Number回退中，P05-004跨源样式，P06-016/P21-001目标KEEP位于不同源样式，未擅自移动或放宽样式校验。另发现既有数字正则会把`2 shows`/`250 samples`的词首s当单位，后续修复会改变传输单元和缓存键，需明确重译/缓存失效，不能暗改译文伪造命中。后续状态仍只维护唯一task-state。

<a id="r5-relative-typography"></a>

## R5：显式缩小字号与1.3倍行距（2026-09-23）

用户要求实际调整字体大小/行距，并强调行距与字号挂钩，不单独维护12pt等绝对设置；同时要求解释原子样式不匹配与缺有效译文。本批仅实现全局排版设置与真实样张，未实现译文页再layout/三轮动态扩框。

- 新增`bdt rust-translate --font-scale 0.9 --line-height 1.3`，默认不传仍维持源字号/源行距比例。所有目标run同比缩放，标题和小字层级不变；源IR、翻译缓存和保护原文不改。首行源基线不动，fit不进一步缩字或压行距。
- 最新交付：`tmp/backend-repair/mvp-r5-typography/translated.pdf`。正文9.9626pt→8.9663pt，基线间距由字号×1.3得到约11.6562pt；不是固定12pt。包含前3页预览、重点页PNG、README、验证JSON及`fallback-examples.md`。
- **147写入/15回退**，相对R4新增P06-006、P07-011、P08-012，0原成功段退化。回退为8排版、3原子、4缺有效缓存；另40源重叠/1旋转文本保护仍在。158真实缓存命中、0主请求/0补救，约20.177秒，23 PageReady及最终发布。正确保持RunFinished=false、bdt exit1。
- 43,436个保留源字符位置/字号/颜色变化0；独立核对输出PDF的14,335个非空白译文字形，缺失/位置/字号变化0；275处相邻基线间距满足请求倍数，行间墨迹框碰撞0；qpdf通过，目视p1/p2/p7/p18。仍有中英混排、留白和缓存译文质量问题，不是全篇质量通过。
- workspace **664 passed / 0 failed / 8 ignored**，另显式真实inventory测试1 passed；相关3crate strict Clippy、fmt/release通过；bdt/单入口35 pytest与Ruff通过。日志：`tmp/backend-repair/r5-typography/`。

具体解释已查证：P06-016的20%随中文语序从style3移到style5，二者外观完全一致；P21-001的27%/23%也在同外观编号之间换序。P05-004的90与%来自两个源字体片段，一个原子跨样式；均为现实现限制，不应概括为数值译错。P19-018源文`G Evading Neural Cleanse`的历史日志为语言比例不足；对诊断候选`G 规避 Neural Cleanse`实际复现中文2/字母类16=12.5%，触发50%门禁，但它不是历史模型回包原话。P18-010历史记录数字/语言比例违规，缺失原回包不能判断具体哪个数字变化；另外两段亦不能仅凭cache miss臆断具体违规。R5当时未更改这些保护/校验规则，也未将诊断候选写入缓存或PDF。

<a id="ccs3764-yagni-test"></a>

## R6规则调整与CCS 3764先实测（2026-09-23）

按用户要求将YAGNI、先实测后优化写入AGENTS.md；不再追加开发，直接测试主树`ccs2026b-paper3764.pdf`。R6已删除语言比例、样式数量/空片段门槛，允许已知样式换序及跨相邻源样式文本原子；提示词同步，块/KEEP/数值身份和注释安全检查保留。专项Rust272通过/0失败/6 ignored、35pytest及release通过；完整workspace因超时/用户中断未验收，不宣称全绿。

- 真实`deepseek/deepseek-flash`，21页，0.9字号/1.3x行距，CoreML。1主请求/0补救/0缓存命中，157个译块全部通过校验并缓存；**88写入/69回退**（34排版、35原子），另625策略/保护不替换。总201.416秒，所有页保存，RunFinished=false/exit1。
- **流式通过**：P01-001于149.009秒排版，首页153.060秒保存，翻译200.659秒才结束；中途另存可读PDF快照。
- **动态bbox未通过**：仅翻译前源layout一次，frame固定；尚未实现译后检测/动态回收净空。
- **链接位置未通过**：319链接、180命名目标、28书签均保留，目标均能解析；但22点击框错位（15表、5节、1附录、1公式引用）。无atom段绕过注释相交检查，原链接Rect未随译文重定位。p7 `Table 2`旧框罩在中文“下”字上；不得以链接数量不变当通过。
- 94,049保留字符位置/字号/颜色变化0，无KEEP泄漏，qpdf通过；目视p1/p2/p7/p15仍明显中英混排和留白。两个外部URI原本就是模板DOI占位符，未做网络可访问性认证。

样张（含已知错位，不是合格交付）：`tmp/backend-repair/ccs3764-real-v2/translated.pdf`；同目录README、预览、test-summary.json及PNG。证据/命令/代码身份在`tmp/backend-repair/ccs3764-test/`，含流式快照、link-misalignment.json与p7错位对照图。v1因nohup清除动态库路径而未启动引擎，未调用模型；v2只在外部记录器恢复路径。**该轮停在实测结论；后续修复见下方R7。**

<a id="ccs3764-r7"></a>

## R7：链接、数学字形与有限动态bbox（2026-09-23）

> **用户复核后的验收纠正：正文翻译覆盖不通过。** 下述3回退只统计157个已送译块；另40个候选块因源区域冲突在翻译前被阻断，另1页有覆盖缺口。第2页P02-009/011/017/024含行内公式，整个正文块被标为not_replaced，均未送给模型；不是模型漏译，也不是排版回退。此前突出“剩余3段”会误导，应以[完整缺陷及逐块证据](../../issues/rust-inline-formula-coverage.md)为准。

按用户顺序直接实施，不重跑模型、不改界面。最终样张：`tmp/backend-repair/ccs3764-final/translated.pdf`；沿用157段真实DeepSeek缓存，源字号×0.9、行距1.3倍、CoreML。**154写入/3回退**，相对88/69新增66成功、既有成功回退0；21页全保存，19.671秒，主请求0/补救0。625策略/源保护不替换仍单列；不能把这些内容当作已翻译。

- **链接与URL**：精确KEEP锚点、可消歧的普通交叉引用与目标字形关联；内部样式标记只用于排版，不改缓存/Markdown。页候选内更新原注释Rect/QuadPoints，Dest/A不改；歧义与不支持注释仍回退。HTTP(S) URL要求精确源span。原35个原子/链接回退本批均解除；319链接、180命名目标、28书签保留，233点击框更新，保存后全部链接标签对应检查通过，原22错位消除。外部模板DOI仅保留，不认证网络可达。
- **数学/起笔**：内嵌STIX Two Math 2.12 b168及OFL，保持`∗/𝜆/𝛼/𝑠/𝜖`原Unicode；左负侧承在右侧可容纳时实际平移起笔，不改容差或继续缩字。独立用完整原字体在相同原点/字号绘制，对照交付PDF的30个数学字形，3,272个参考墨迹像素中缺失0；不是仅凭文本可提取判定。
- **真实障碍**：源解析补齐`n`消耗未绘路径及Form入口CTM下的BBox裁剪；此前第3页有跨页巨型假障碍。空交先判定再构造会归一化的Rect。第3页保留图区域逐像素相同。
- **动态bbox**：页保存前从不可变源页和已接受译文墨迹重算同栏垂直净空，最多3轮，无改善即停，字号不变、不跨栏、不调用第二次布局模型。本样本第1轮救回P20-014/P20-015，首基线分别上移约3.674/7.358pt；没有反复修改已发布页。消费者按段ID取PageReady前最新状态，初始fallback可能被精修成功替代。
- **最终仍回退3段**：P05-007 `（a）MergeGuard`、P20-005 `（b）6 个中有 3 个后门。`、P20-021 `（b）8 个中有 4 个后门。`。译文在窄子标题框内换成两行，与表格文字/框线冲突，纵向净空不足；未擅自缩字、挤占单元格或改写缓存译文。下一步可单独验证单元格安全横向扩展或更简洁译文。

验收：57,225个保留字符的位置/字号/颜色变化0，无KEEP泄漏，qpdf通过；目视p3/p20及数学放大图。pipeline库158 passed/0 failed/3 ignored，字体/排版专项、两个PDF裁剪回归、35pytest通过；之前读取所有历史fallback的E2E断言改为最终段状态后单独复跑通过。相关四crate严格Clippy、fmt、release与diff-check通过，**未跑完整workspace，不沿用R5全绿数字**。证据在`ccs3764-final/{audit.json,math-and-figure-audit.json,events.jsonl,result.json}`及`ccs3764-fix/`日志/脚本。

中间`ccs3764-optimized/`虽154/3，但CoreML原生诊断污染stdout，桥接正确报`engine_events_invalid`，不能算通过；保留失败产物，随后独立复跑及最终目录事件流正常。此原生输出隔离风险未修复。最终仍为`engine_incomplete`、RunFinished=false、exit1；保护内容中仍含英文、页面留白及语义翻译质量未全面验收，非完整产品交付。


<a id="inline-formula-coverage"></a>

## 行内公式与正文完整覆盖修复（2026-09-23）

**本轮最终产物：`tmp/backend-repair/inline-full-v5/translated.pdf`。** 此节取代R7样本的未完成结论；历史失败和错误报告保留，不把它们改成成功。

### 实现与实跑

- 公式建立独立源字形归属、bbox/基线和KEEP原子；上下标规范分行，原字体/内容流/分式路径按原尺寸重放。页候选内删除旧字形、裁去旧路径，再原子保存。嵌套图片Form仅在公式私有副本中去掉无关文本，原图片保持。
- 可译区域重叠按较小框唯一分配字形；单位正则补词界。第20页漏检子标题凭标签/单行/邻近面板证据恢复，居中排版使用面板宽度。公式内链接同步移动。
- `inline-page2-v2`真实主请求1次，18块送译，16写入/2公式行间碰撞；局部增加必要行距后`inline-page2-v3`复排18写入/0回退。原先第2页四个缺缓存块已经实际送译。
- `inline-full-v1`在公式副本处理嵌套图片Form时失败；修复后`inline-full-v2`达到193候选、187写入/6回退。随后修正5个子标题的宽度和1个公式引用链接。
- `inline-full-v3`的182缓存块均排版成功，但单位词界修复后的真实补译遇非法Markdown中止；`inline-full-v4`额外完整真实请求也遇到非法正文转义中止。新增闭合坏块的有界补译守卫，保留流截断/通道错误的失败行为，不接受坏译文。
- `inline-full-v5`合并v3/v4的真实缓存，按原source hash和原译文`INSERT OR IGNORE`，没有手填译文或伪造缓存。最终1主请求/0补救/183缓存，真实补译剩余10块，65.989秒。播种来源保存在`cache-provenance.json`。

### 最终 PDF 核验

| 项目 | 结果 |
|---|---|
| 保存页 / 应译块 | 21页 / 193块全部写入 |
| 回退 / 送译前冲突 / 覆盖缺口 | 0 / 0 / 0；RunFinished=true、bdt exit0 |
| 正常保留实体 | 576个；不将公式/表格/参考文献等实体数当成独立正文段数 |
| 字号 / 行距 | 源字号×0.9；普通基线间距为目标字号×1.3；仅高公式所需位置增加间距 |
| 公式原绘制 | 94个源公式，53,384参考墨迹像素，缺失0（4倍渲染，1像素抗锯齿位置容差） |
| 保护内容 | 成功段源框外36,327字符位置/字号/颜色变化0；第3页图片区域像素相同 |
| 链接 / 书签 / 命名目标 | 319 / 28 / 180保留；314点击框移动，全部目标及点击标签核对通过 |
| PDF与泄漏 | qpdf通过；无KEEP残留 |

主控目视p1/2/3/4/5/9/20；确认第2页四块正文中文、数学上下标与分式可见、图中文字保留、第20页六个子标题均译出。译文仍按源段落锚定，因此可有较大留白；此次证明该样本的覆盖/安全写回，不宣称任意PDF或翻译措辞均已全面认证。此样本匿名，作者保护另有已有前置信息行为守卫。

证据：最终目录的`result.json`、`events.jsonl`、`audit.json`、`qpdf.log`及页面PNG；审计脚本`tmp/backend-repair/inline-audit.py`。PDF SHA-256：`ec4ba55722162f974236dfc60b8f553ca20cf35c37c15fd5a99e0ae90a621ced`。

### 工程验证

相关core/pdf/typeset/translate/pipeline共528项通过、0失败、8 ignored；随后协议补译改动重新跑translate：115通过、0失败、2 ignored。包含公式原字号/行间碰撞、源字形唯一归属、图片嵌套Form不重复提取、公式引用点击框、流式坏块补译及截断失败等守卫。CLI/单入口36 pytest通过。五crate全target严格Clippy、release构建、fmt、Ruff、diff-check通过。未额外运行全workspace或未改动的前端测试。

日志：`tmp/backend-repair/inline-{tests,protocol-tests,pytest,clippy,build5,docs}.log`。严格文档构建仍有既存HTTP参考的中文锚点警告，不能记为通过。实现边界见[后端参考](../../reference/rust-pdf-backend.md)，经验见[源公式归属](../../lessons/pdf-binding-and-render-evidence.md#inline-formula-ownership)。
