# 下一位 Agent 接手：Rust PDF 翻译后端

交接日期：2026-09-23。用户要求本会话整理文档，由用户自行交给下一位 Agent；本会话不启动接手进程、不继续功能开发。**后端整体未完成，MVP是明确标记的部分译文。**

本文件是交接快照。后续进度只更新唯一 [task-state.md](task-state.md)，不另建状态副本。旧 [handoff-codex.md](handoff-codex.md) 是2026-09-22历史，旧第5节任务已经完成，禁止照旧重做。

## 1. 开工位置与必读

```text
/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端
```

阅读顺序：

1. 根 `AGENTS.md`、`ARCHITECTURE.md`；架构前半段主要描述旧Python，Rust看第7节。
2. 唯一状态的绝对路径：`/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端/docs/reports/2026-09-22-rust-electron-rewrite/task-state.md`。
3. 本文、[MVP验收](12-MVP真实翻译验收.md)、[Rust当前边界](../../reference/rust-pdf-backend.md)。
4. [后续接口契约](07-后续接口契约.md)：它含计划，不能全当现有功能；R2/R3实施情况以状态和代码为准。
5. 要委派时先读 `docs/guide/delegation.md`；需要源绘制路线时读 `hjfy-architecture/ARCHITECTURE.md`、`hjfy-architecture/docs/reference/pdf-pipeline.md` 和 [深挖](research/02-hjfy-engine-deep-dive.md)。

## 2. 用户当前最重视什么

- **尽快迭代可看、可复跑的MVP，让用户依据真实PDF指导开发。**最近追问为什么还有大量回退、能否修复；下一步优先减少整段英文保留，先处理引用/公式和区域归属，不回到无止境的性能调查。
- 继续完成Rust后端，不做界面，不改Electron，也不顺手重写旧Python排版。
- **字号保持原文或用户指定值；禁止自动缩字。**真正容纳失败明确提示；二次编辑也适用。当前不自动缩行距。
- PP-DocLayout-V3和Apple GPU。本机已用CoreML CPUAndGPU，不是PyTorch MPS；不能只注册EP就声称GPU参与。
- Markdown one-shot，闭合块立刻编译，主请求与补救请求分别计数。真实翻译provider仍是本机pi，不与开发子代理混淆。
- 原文样式对应、作者/机构/地址/脚注/图片/reference等保护；公式源绘制与链接正确保留；Knuth–Plass正文两端对齐、字体/位置稳定。
- 最终还要A3横向dual（左原文、右译文）、中文目录/书签及目的地、按layout块修改字体/字号并重新编译导出。**这些尚未交付。**
- 不重复p19绑定调查，不重启已结束worker；全部dirty/untracked保留，尤其既有`cache/`。生产不调用LaTeX，TeX/旧bdt只能作质量对照。
- 如需委派：用户指定 **gpt-6-sol、high、fresh context、Orca独立worktree**，主控分工、亲审diff/复验/合入；叶子不委派、不改task-state、不push/merge。一树一writer，任务最多两轮/30分钟、提前5分钟checkpoint。不要照旧交接使用astra或旧Pi开发harness。

## 3. Git、工作树与活动任务

交接整理前主树：`feat/desktop-develop`，HEAD `e0abcea8`；最新实现提交 `9939ed9b`。交接文档提交会在其后，接手以 `git log/status` 为准，不reset到此快照。

```bash
git status --short
git log -8 --oneline
git -C ../repair-r1-bind status --short
git -C ../repair-r1-bind rev-parse --short HEAD
```

本次核对：

- 主树只有 `?? cache/`，没有未提交源码。文档整理会单独提交；不暂存/清理cache，不用`git add -A`。
- `../repair-r1-bind`：HEAD `b47c7283`，干净；其候选已经审查、提交并合入主树，**不是待集成候选**。
- 没有运行中的子代理、syncpdf-cli、cargo或rustc任务；不用恢复/等待旧会话。所有前轮worker已结束，旧Orca标签不代表活跃。
- `../repair-r1-layout/tmp/backend-repair/ortlib`是现有ORT动态库只读位置，不能删该树。其它旧树保留，不恢复旧任务。

最近有效提交：

| 提交 | 内容 |
|---|---|
| `8f3bcd84` | 部分TJ删除精确推进、固定栏归属、R3排版收尾 |
| `8a843766` | CoreML session、显式回退与profile |
| `1ef8ba22` | 最近祖先Resources、q/Q文本状态及跨Contents恢复 |
| `e9174346` | V3锁定权重、GPU pipeline、缓存与事件 |
| `4fe625d7` | bdt Rust试用入口候选合入 |
| `9939ed9b` | 普通数字原子、真实缓存重编译、bdt状态/保护修正 |
| `e0abcea8` | MVP实测、长期文档和剩余边界 |

## 4. 已交付产物与准确结论

输入：`/Users/zhengcaiyi/Downloads/2106.04690v2.pdf`，23页。此前记录SHA-256为`cd775d0b24e27d02134867727451533eb11503794df14d0ae7c751c4ab743968`；接手若输入改变需重新核对，禁止混版本绑定。

**当前给用户的MVP**：`tmp/backend-repair/mvp-20260923/`

- `translated.pdf`：23页部分译文；`preview-3-pages.pdf`：前3页。
- `README.md`：未成功块清单；`events.jsonl`、`result.json`、`review-summary.json`、`source-retention.json`；页面PNG。
- 107块译文实际写入；55块回退：**40 atom_source_unplaced、12 typeset_overflow、3 translate_missing**。
- 另40源区域保护重叠、1旋转侧注提前保留，不计入55；作者/脚注/reference等按策略不译，不能把所有英文都算同一类错误。
- 158段真实缓存命中，0主请求/0补救；18.993秒；23个PageReady，0 error事件，完成最终自检与发布。
- 62,943个保留字符核对，位置/字号/颜色变化0；qpdf通过。只目视重点页，**不是全篇视觉质量通过**。
- `RunFinished ok:false`和CLI exit1是正确的部分结果状态，不能改成成功掩盖缺口。

### 真实LLM历史不能混用

| 目录（`tmp/backend-repair/`下） | 实际结论 |
|---|---|
| `mvp-real-p1-3` | pi真实前3页63.10秒，16成功/14回退；1主/0补救 |
| `mvp-real-full-v1` | pi真实全文352.39秒，90成功/72回退；1主/10补救；**后续复排使用此目录的真实缓存** |
| `mvp-real-full-v2` | 数字修复后107块排版准备好，但补救输出unknown escape，4块未落定；只保留最后快照，不是最终发布 |
| `mvp-20260923` | 上述真实缓存重编译，107成功/55回退，全部页保存并发布，是当前交付 |

首次全篇typeset在133.50秒、首页落盘136.10秒，翻译阶段351.94秒才结束，已证明编译早于模型结束。fake日志只算工程验证，不能替代这些真实模型结果。

## 5. 下一个具体任务：减少整段回退

### 建议第一批范围（尚未实施）

先解决“一个引用使整段不翻译”，同时确认链接点击位置。拿MVP清单中的可重复样本做窄修，用既有真实缓存复排。优先覆盖常见文本型引用，再扩展真正的公式源绘制；不要一次把所有atom保护解除。

1. **40个原子回退**：当前只允许符合源范围/单一样式/无注释相交条件的`AtomKind::Number`。`Other`包括引用编号与邮箱，`Url`、`Formula`等仍整段回退。不能把40个都叫数学公式；需按kind和链接关联分类。
2. **40个源区域重叠**：可译区域与Formula/Code/Table等保留区域共享源glyph时保护整段。需要可靠源归属/行内分割；不能扩大不可译区域或清除warning伪装修复。
3. **12个溢出**：统一Overflow也可能来自安全frame、actual ink、碰撞、shaping覆盖等；尚未逐块归因，不能都断言为译文过长。先修误判，真正空间不足继续明确提示，禁止缩字。
4. **3个缺有效译文**：最终缓存模式未命中；共4个缓存未命中，其中1块先走原子回退，因此不在translate_missing计数。真实模型历史有坏转义、保护字面量/目标语言比例不合规；不要把这些简单说成网络故障或只反复重试。

用户未授权放宽字号或质量门禁。优先交付可看结果和具体对比，再继续A3 dual、目录、完整链接和编辑事务。原子/链接的共享IR需主控先冻结，不能让多个writer同时改core、protocol和run.rs。

### 已核实的代码入口与陷阱

| 路径（`engine/crates/`下） | 接手重点 |
|---|---|
| `syncpdf-pipeline/src/stages/paragraph.rs` | `detect_atoms`、源字形归属、保护重叠、段落与样式 |
| `syncpdf-pipeline/src/stages/text_atoms.rs` | 当前仅Number源原文回填；注释相交/未知几何、跨样式拒绝 |
| `syncpdf-pipeline/src/run.rs` | `handle_block`的回退、`writeback_page`事务；不能排版成功但未保存就报写入成功 |
| `syncpdf-pipeline/src/stages/typeset.rs` | `atom_size`仍有估算；`para_glyph_bbox`实际返回None，不能拿现值当真实原子几何 |
| `syncpdf-core/src/ir.rs`、`syncpdf-typeset/src/` | LineBox.kept_atoms只有ID，没有完整源绘制身份/目标放置；Atom宽高占位不代表可见内容已重放 |
| `syncpdf-pdf/src/{bind,patch,pdfium}.rs` | 源code/字节/字体/CTM与源绘制身份；共享流隔离、stale拒绝 |
| `syncpdf-pdf/src/links.rs` | 当前主要审计Annots数量/可解析性，**没有完成译文Rect/QuadPoints重建** |
| `syncpdf-translate/src/{unit,markdown,prompt,validate,translator,pi}.rs` | 样式/原子身份、传输校验、重试、缓存重编译 |

额外注意：

- `Glyph.matrix`当前绑定主要保存原点平移，不能把它当完整源对象绘制矩阵。不要用简单重画字符替代公式而忽略字距、水平缩放、旋转、字体、裁剪等状态。
- DisplayItem的Path/Image尚无足够精确源操作身份支撑任意公式图形移动。复制整页Form再裁剪会带入隐藏文本；旧位置图形也可能残留，不要用它冒充干净的源原子复制。
- 不删源文本再只画占位宽度，不抹掉KEEP校验，不把引用变成普通译文后丢点击框。链接身份、源范围、目标几何需贯通。
- 当前链接Markdown契约`[文字]{link=1}`仍是计划，不是实现；加实现需考虑传输版本/旧缓存兼容，缓存失效要明确，不能伪造命中。
- PDFium和lopdf必须同一不可变输入；一次apply后旧绑定stale。源宽度未知（部分无Widths简单字体）、重复/嵌套目标Form仍有拒绝边界。
- 不重复p19绑定调查。它已修复并验收；常规全23页回归可覆盖该页，但不要重启旧专项。

## 6. 本机环境与最快复现

从主树根运行；`env.sh`在ignored tmp中，但下面给出等价设置，避免交接依赖单个临时脚本：

```bash
cd /Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端
export CARGO_TARGET_DIR="$PWD/engine/target"
export TMPDIR="$PWD/tmp/backend-repair/codex-r1-integration/runtime"
export CARGO_BUILD_JOBS=2
export ORT_LIB_LOCATION="$PWD/../repair-r1-layout/tmp/backend-repair/ortlib"
export ORT_PREFER_DYNAMIC_LINK=1
export DYLD_LIBRARY_PATH="$ORT_LIB_LOCATION"
export PDFIUM_DYNAMIC_LIB_PATH="$PWD/engine/vendor/pdfium/lib"
mkdir -p "$TMPDIR"
```

等价捷径：`source tmp/backend-repair/codex-r1-integration/env.sh`。

- Rust实际链接ORT **1.23.2**，conda Python的ORT1.30.0不等于Rust运行库。当前release缺rpath，直接运行可能报dyld找不到ORT，先加载上面环境，不能误判为翻译失败。
- 不重装共享conda。Python：`/Users/zhengcaiyi/miniconda3/envs/bdt/bin/python`。从cwd用`python -m babeldoc_tools`，裸bdt可能指向另一editable树。
- pi 0.87.1、`deepseek/deepseek-flash`、low。配置已存在，本轮真实请求成功；不要打印凭据。若子进程使用坏的Homebrew node，可在**本次进程**前置`/Users/zhengcaiyi/.nvm/versions/node/v24.14.1/bin`到PATH，不改用户全局配置。
- pi的1810秒timeout目前按每次stdout读取/等待退出计时，不是整个请求总时限；默认最多3轮分组补救，可能产生多个请求。修排版优先cache-only。
- `engine/vendor/`、fixtures、ORT库只读；每树自己的target/runtime，不并发共享可写target。测试产物只放本树tmp，不碰`~/.sp`或既有`cache/`。

### 先看现有PDF，再按需要复排

```bash
MVP_OUT="tmp/backend-repair/next-mvp-$(date +%Y%m%d-%H%M%S)"
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools rust-translate \
  /Users/zhengcaiyi/Downloads/2106.04690v2.pdf \
  --workdir "$MVP_OUT" \
  --cached-from tmp/backend-repair/mvp-real-full-v1 \
  --layout-device coreml
```

可先加`--pages 1-3`。每次用新workdir。**当前预计exit1（partial），不能用set -e直接中断后续检查，也不能把exit1当全部崩溃。**核对events里的error、PageReady、document_finished及最终状态。

要真实翻译时去掉`--cached-from`，可显式加`--model deepseek/deepseek-flash --thinking low`。改代码后先重新build release；不能拿旧binary跑结果算新代码验收。

针对同一输入的完整23页，已有检查脚本：

```bash
~/miniconda3/envs/bdt/bin/python tmp/backend-repair/mvp-runner/review.py "$MVP_OUT"
qpdf --check "$MVP_OUT/translated.pdf"
```

该脚本硬编码原件路径/重点页，适用于本论文完整23页；换输入或选页需审脚本，不要盲用。它会写新目录里的报告/PNG/预览，不要在已交付目录上覆盖证据。`typeset`只是准备状态，必须结合PageReady确认已保存；bdt最新实现据此统计successful_blocks，另列typeset_blocks/saved_pages。

## 7. 验证门禁与已有证据

本次文档交接不重跑模型或测试。已有有效门禁：

| 阶段 | 已执行结果 | 日志 |
|---|---|---|
| GPU接线后的workspace | 648 passed / 0 failed / 7 ignored；这是MVP前快照 | `tmp/backend-repair/layout-final-review/v3-parent-workspace-v2.log` |
| q/Q部分删除 | 8真实PDF通过，含跨Contents；原red保留 | 同目录`r3-graphics-state-final.log`、`r3-graphics-state-red.log` |
| 最新MVP相关Rust 3crate | 269 passed / 0 failed / 5 ignored；不是最新全workspace数字 | `tmp/backend-repair/mvp-runner/cache-tests.log` |
| strict Clippy/release | 通过 | 同目录`cache-clippy.log`、`cache-release.log` |
| 最新bdt与单入口 | 26 passed；Ruff通过 | 同目录`delivery-bridge-tests.log`、`delivery-bridge-ruff.log` |
| 最终MVP保护/qpdf | 62,943保留字符0变化；qpdf通过 | 同目录`delivery-review.log`、`delivery-qpdf.log` |

按修改范围执行：

```bash
cargo fmt --manifest-path engine/Cargo.toml --all --check
cargo test --manifest-path engine/Cargo.toml -p syncpdf-translate -p syncpdf-pipeline -p syncpdf-cli
cargo clippy --manifest-path engine/Cargo.toml -p syncpdf-translate -p syncpdf-pipeline -p syncpdf-cli --all-targets -- -D warnings
cargo build --manifest-path engine/Cargo.toml --release -p syncpdf-cli
~/miniconda3/envs/bdt/bin/python -m pytest tests/test_rust_backend_cli.py tests/test_single_entry.py \
  --basetemp="tmp/pytest-next-$(date +%Y%m%d-%H%M%S)"
~/miniconda3/envs/bdt/bin/ruff check babeldoc_tools/rust_backend.py babeldoc_tools/__main__.py tests/test_rust_backend_cli.py
git diff --check
```

改pdf还要跑相应真实删除/共享资源/源保护测试；改共享IR做workspace门禁。已有无文本夹具早退、ignored不是有效真实覆盖。文档strict构建有已核实的旧HTTP→pipeline中文锚点告警，仍失败，未算通过，不顺手扩大范围。

### GPU证据无需再调查

锁定本机bbox V3：`~/.cache/babeldoc/paddle-models/inference_bbox.onnx`，SHA-256 `fe3bc78476c982401caf389a8e8e928cb94cc0dbb89be73a363838f19fcaf271`。旧vendor完整图CoreML不兼容，Auto会明确CPU回退；严格coreml失败报错。测试已证明Apple M5 Pro GPU参与（计算计划+ORT profile）；全23页推理中位约99ms/页，初始化仍明显。运行选项见`engine/models/README.md`、[GPU验收](11-V3-GPU验收.md)，不再优先细调启动性能。

## 8. 交付边界与本地文件保全

- 用户已拿到MVP并指出回退问题；下一轮要展示实际改善的PDF和对比，不只报测试数量。
- 保留旧失败和原MVP。新结果放新目录，记录减少的回退、是否新增溢出/保护冲突、源内容变化、实际链接和视觉证据。
- `tmp/`、vendor、模型缓存、原件均不随Git传输。换机器须另带原件、`mvp-real-full-v1/cache/translate.db`、最终MVP/日志与运行依赖；不能只clone后声称已具备复现资料。不要上传密钥。
- 当前经验已提炼到 `docs/lessons/pdf-binding-and-render-evidence.md`。只有完整目标真实完成后才能把task-state标完成；此次是交接停点，不能标整项完成。
