# Codex 接手：Rust PDF 后端修复

交接日期：2026-09-22。原主控宿主是 Pi；用户要求当前子任务结束、主控验收后转到 Codex。**本文件是交接快照，最新状态仍只维护在 [task-state.md](task-state.md)，不要复制第二份 task state。**

## 0. 先看结论

- **用户要继续完成后端，不做界面**：打通输入 PDF → 流式翻译 → 高质量译文 PDF，继承 hjfy 内容流级编辑路线，生产不用 LaTeX，保留公式/图形/样式/链接，质量不逊于旧 bdt。
- 本轮最后一个子任务 `81f8a3f7-8375-4dee-b342-e3a409675e46` 已结束，主控局部验收通过。**当前没有运行中的子代理，也没有需要继续等待/恢复的任务。**未启动 Codex 进程；由用户自行切换。
- 指定论文全部23页、78508个源字形现在通过绑定门禁；原p19可见但无PDFium字符的操作已用目标自身证据恢复。**这不是全篇翻译/排版/视觉验收通过。**
- writer / layout / stream 三项已经合入主树；**R1-bind的全部累积改动仍在另一个worktree中dirty，未暂存、未提交、未合入。**主树本身没有这些绑定修复。
- **第一个任务：核对两棵树与保全指纹，完成绑定候选的最终累积串联review；满足条件后统一提交/集成，再做四项修复合批验证。不要先重做p19调查、重开原worker或直接开发R2。**

## 1. 开工位置与必读顺序

建议在以下主目录打开 Codex：

```text
/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端
```

依次读：
1. 本主目录的 `AGENTS.md` 与唯一状态文件：
   `/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端/docs/reports/2026-09-22-rust-electron-rewrite/task-state.md`
2. 本交接文档、[执行计划](05-后端修复执行计划.md)。[初始失败验收](04-合并后引擎快速验收.md) 是e925d3b6历史基线，不是当前所有问题仍未修复的清单。
3. 主目录 `ARCHITECTURE.md`、`docs/guide/delegation.md`；注意根架构/CLI指南主要描述旧Python/bdt，**不能把旧LaTeX管线当成Rust生产设计**。
4. `hjfy-architecture/ARCHITECTURE.md`、`hjfy-architecture/docs/reference/pdf-pipeline.md`、[引擎深挖](research/02-hjfy-engine-deep-dive.md)。hjfy目录在主树，子树缺它时跨树只读。
5. 主目录 `tmp/backend-repair/parent-review/object/acceptance.md` 与本轮精确diff，按第5节开展最终审查。

如果Codex沙箱不能访问同级绑定树，请请求访问权限，**不要把旧主树误当候选，也不要为了规避权限复制/覆盖整份engine**。

## 2. 工作区事实：不要丢dirty，不要混代码版本

| 角色 | 路径（共同父目录 `/Users/zhengcaiyi/orca/workspaces/ieeTranslater/`） | 分支 / HEAD |
|---|---|---|
| 主树、任务状态、集成目标 | `桌面端` | `feat/desktop-develop` / `9e3fd455d8737b54996cabf8255a3cd3fb41a2fd` |
| 绑定候选、下一步代码审查位置 | `repair-r1-bind` | `zcyisiee/repair-r1-bind` / `e925d3b63a2a6dab75c273c3d90a8f0258cfe4a7` |
| 已合并的布局树、只读ORT库位置 | `repair-r1-layout` | 不再分派；保留其 `tmp/backend-repair/ortlib` |

主树已经合入：writer `915120b1`（merge `5a3bf814`）、stream `93e40ba5`（merge `83e18b94`）、layout `6c3d22d4`（merge `9e3fd455`）。**绑定树基线没有这三项，绑定树测试不能替代主树集成验收。**

### 绑定树：6个已跟踪修改 + 4个未跟踪正式测试

```text
M  engine/crates/syncpdf-pdf/src/bind.rs
M  engine/crates/syncpdf-pdf/src/pdfium.rs
M  engine/crates/syncpdf-pdf/src/patch.rs
M  engine/crates/syncpdf-pdf/tests/writeback.rs
M  engine/crates/syncpdf-pipeline/src/stages/source.rs
M  engine/crates/syncpdf-pipeline/src/stages/writeback.rs
?? engine/crates/syncpdf-pdf/tests/repair_bind_semantics.rs
?? engine/crates/syncpdf-pdf/tests/repair_object_evidence.rs
?? engine/crates/syncpdf-pdf/tests/repair_patch_shared.rs
?? engine/crates/syncpdf-pipeline/tests/repair_bind_gate.rs
```

累积tracked diff为1603 additions / 200 deletions（不包含未跟踪测试）。**禁止reset/clean/stash/覆盖继承改动；不要只备份git diff而漏4个测试；不要对已经dirty的绑定树再次apply累积patch。**

主树另有尚未提交的任务状态机制文档：`AGENTS.md`、`ARCHITECTURE.md`、`docs/index.md`、`docs/guide/delegation.md`；未跟踪的 `docs/lessons/`、本计划目录的04/05报告、task-state和本交接文档。既有未跟踪 `cache/` 与本任务无关，**不清理、不暂存**。不要使用 `git add -A` 混入用户缓存。

主控已保存候选完整备份：
`桌面端/tmp/backend-repair/parent-review/object/{cumulative.patch,untracked/,sha256-before.txt,status-before.txt}`。
保全与验收后校验通过，源码未再变化；末次状态/指纹见同目录`status-final.txt`、`sha256-final.txt`。这些tmp证据不在Git里；不要删除两棵树或清理tmp。若搬机器，必须另行带走dirty源码、未跟踪测试、任务文档和证据，不能只clone远端。

## 3. 最后一次主控验收到底证明了什么

主证据目录：`桌面端/tmp/backend-repair/parent-review/object/`。

| 实际执行 | 结果与证据 |
|---|---|
| 完整阅读本轮bind/pdfium精确delta与专属测试，核实权限 | 局部通过，见`acceptance.md`；未动core/patch/pipeline的新一轮实现 |
| PDF + pipeline相关全部测试 | **141 + 101 = 242项报告通过，0 failed**，见`tests.log` |
| workspace fmt；pdf/pipeline全部targets strict clippy | 通过，`fmt.log`、`clippy.log` |
| 主控独立原件23页probe | 78508字形源code/字节核对；正常非空白字形几何和新增对象几何分支各自校验；23页gate均通过，`paper-independent.log` |
| p19实际PatchSet删除 vs 独立清空原Tj副本 | 72/144/288dpi逐像素一致；对原件改变11/27/68像素；22个非目标页完整PDFium对象/几何不变，原对象除目标页字典外不变 |
| worker正式manual原件probe由主控显式执行 | 1 passed，`paper-worker-test-rerun.log` |
| p19删除副本 qpdf --check | exit0，`p19-qpdf.log`；这只证明结构检查，不证明译文质量 |
| 文档 `mkdocs build --strict` | **未通过**：当前与干净HEAD均只有既有`reference/http-api.md`→`pipeline.md`中文锚点警告，见`mkdocs-{final,baseline}.log`及`mkdocs-comparison.json`；本次新增链接告警已修，不顺手扩修旧HTTP文档 |

测试口径：242项报告中仍有旧`ci-test.pdf`无text-show的一次早退，不能算真实文本改写覆盖；默认ignored的原件probe已用`--ignored`实际执行。52个既有补充平面Unicode fallback仍单列，不冒充PDFium独立Unicode认证；空格折叠几何也未因本probe全面认证。真实up-vns 12页预绑定删除/非目标页保护和重复Form/stale/版本混用拒绝回归均保持通过。

布局门禁与绑定门禁也要分开：此前主控确认layout修复后p1/p5未覆盖率降到0，但worker全篇报告p7/p15仍有模型框收紧缺口，须在R1集成验收中复核，未获授权放宽coverage门限。

### p19已解决的具体根因与窄契约

- 输入原件：`/Users/zhengcaiyi/Downloads/2106.04690v2.pdf`。
- SHA256：`cd775d0b24e27d02134867727451533eb11503794df14d0ae7c751c4ab743968`。
- PDFium会对重叠/近重叠重复字符做文本页去重，但第二个绘制对象仍在。p19 stream264/op165，Tj `[7]`，对象108、Form路径[0]缺字符但实际可见。不是空白，不是漏遍历。
- Form264自身Resources中的R8是font271:0，Type3；Encoding Differences code7→`/four`→CharProc305:0。不能借另一个Form的同名R8，也不能直接把raw code7当Unicode。
- 新支路只支持严格匹配、单完整源code的Tj、没有非generated字符、目标自身geometry可用、有明确ToUnicode/Encoding证据。简单字体恰1字节，Type0只接受明确Identity-H的2字节；未知/变长CMap和Identity-V拒绝。
- `pdfium::TextObject.object_bounds`：GetRotatedBounds四角经祖先Form变换后求页AABB；origin由自身matrix与祖先链得到。不重复应用自身matrix，不伪造TextChar或loose-char bbox。
- `BoundPage::object_geometry_evidence()`只读证据带源GlyphId/OpKey、font_id、对象index/path、code/字节/Unicode来源；`BindStats::object_geometry_bound_ops`独立计数，门禁核对关联一致性。
- p19只有1个新恢复操作，Unicode `'4'`；origin约[129.19322,123.0569]，bounds约[129.42798,123.0569,131.97203,126.54957]，与此前原生GetBounds证据一致。

本轮精确源码指纹、代码审查细节和所有命令结论见`acceptance.md`。原调查位于绑定树`tmp/backend-repair/p19-evidence/`、主树`tmp/backend-repair/parent-review/p19/`，**无需从头重做调查**。

## 4. 必须继承的工程边界

- 主控（现在由Codex接手）维护唯一task-state；开始、恢复/compact后、委派前读它，决策/验收/阻断/下一步变化后更新。叶子只能读，brief必须给主树canonical绝对路径。
- 用户最后指定的子代理仍是 **pi-subagents + `openai-codex/gpt-6-astra:low` + fresh context**，显式cwd、`worktree:false`，一树一writer。**Codex主控迁移不等于授权改子代理协议/模型。**Codex可以亲自审查、验证与开发；若继续委派但新宿主没有相应工具/模型，先向用户确认，不假装有Pi工具或静默换Codex/Claude CLI子进程。
- 当前Pi任务全部结束；不要resume旧run。新任务最多两轮实质尝试、30分钟、提前5分钟checkpoint；叶子不委派、不push/merge，主控审完整diff并复验。当前用户停点已到，是否重新委派由接手主控结合工具能力处理。
- PDFium和lopdf必须读**同一不可变输入版本**，先绑定再修改。旧up-vns p3的512/510来自版本混用，不是原件坏；回归已经显式制造混用，别再恢复错误结论。
- 共享Contents/Resources/Form按页隔离，精确Do定位，失败不发布candidate。`PatchSet::delete_glyphs(&BoundPage, ids)`整批预检。段落删除排入同一PatchSet；一次apply后旧绑定stale，不能再次apply复活原文，需保存并从新版本重新绑定。
- 重复Form实例在source明确拒绝；嵌套/重复目标Form在写回前拒绝。**绑定能识别嵌套变换，不代表嵌套目标Form已支持编辑。**无显式空映射的继承启发式、普通字符路径Type0 Encoding/码宽、畸形CMap fallback未全面认证。
- 不放宽断言、不丢可见字符、不吞错误、不用原文保留/文件存在/局部green冒充成功译文。不以缺依赖skip充真实验收。
- 不改Electron、旧bdt生产行为、vendor/fixtures，不增加产品入口。已有`syncpdf-cli`是Rust内部sidecar/开发验收入口，不把它扩成第二个产品工具包。TeX/旧bdt只做质量oracle。
- 运行产物放本树`tmp/backend-repair/`；target/cache不得并发跨树共享可写。`~/.sp`和既有`cache/`不碰。全局Python env是`~/miniconda3/envs/bdt`，不新建每树venv，测试从目标cwd用`python -m ...`。

## 5. Codex的第一个具体任务：完成R1-bind最终集成门禁

### A. 先核对，不改代码

```bash
MAIN=/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端
BIND=/Users/zhengcaiyi/orca/workspaces/ieeTranslater/repair-r1-bind
git -C "$MAIN" status --short
git -C "$MAIN" rev-parse HEAD
git -C "$BIND" status --short
git -C "$BIND" rev-parse HEAD
git -C "$BIND" diff --cached --name-only
(cd "$BIND" && shasum -a 256 -c "$MAIN/tmp/backend-repair/parent-review/object/sha256-final.txt")
```

预期HEAD与第2节一致，绑定树暂存区空、10个候选文件指纹匹配。不一致先核实是谁改动，不reset回旧指纹。

### B. 最终串联review，不只看最后一轮

全部累计入口：绑定树`git diff`加4个未跟踪测试。按顺序审：
1. `bind.rs / pdfium.rs`：源操作与字符/对象证据、空映射、CMap、实例身份、Text槽顺序、页号、source门禁。
2. `patch.rs`：整批预检、页/流/资源copy-on-write、精确Do、stale/重复/嵌套拒绝、失败原子性。
3. pipeline `stages/source.rs / stages/writeback.rs`：source拒绝和一批删除接线，不能误以为run.rs发布事务/错误传播也修好了。
4. 所有新增与调整测试，尤其真实up-vns不可变原件与故意混用版本的区别。

已有分项主控验收可避免重复探索：主树`tmp/backend-repair/parent-review/{shared,identity,cmap/final,object}/acceptance.md`。最后一轮只有bind/pdfium/专属测试，**最终累积串联审查与集成验证尚未由本会话完成**。发现新red只拆该项，不重写整套引擎。

### C. 通过后由主控统合

先完整保全tracked diff与未跟踪测试，再只提交这10个候选文件；在主树正常合入绑定分支（或按仓库既有合并策略），不要复制整个旧基线覆盖已合入的writer/layout/stream。遇冲突亲自审，不用一边全选覆盖。主树文档改动独立审阅/提交，不把用户cache带入。

随后在**主树自己的target/runtime**重新执行workspace fmt/test/clippy和本轮真实probe，验收指定论文首页/第3页/全23页fake输出。当前`run-paper-probe.sh`硬指向绑定树；合并后须显式调整测试目标/重新编译，不能引用子树日志声称主树通过。

## 6. 可直接复跑的验证命令

以下是在当前绑定候选上已经成功的环境；执行前确认无另一个writer/cargo任务占用同一树。ORT目录只读复用，不要把混有旧PDFium的目录放进DYLD_LIBRARY_PATH。

```bash
cd /Users/zhengcaiyi/orca/workspaces/ieeTranslater/repair-r1-bind
ROOT="$PWD"
MAIN="$ROOT/../桌面端"
export CARGO_TARGET_DIR="$ROOT/tmp/backend-repair/target"
export TMPDIR="$ROOT/tmp/backend-repair/runtime"
export CARGO_BUILD_JOBS=2
export ORT_LIB_LOCATION="$ROOT/../repair-r1-layout/tmp/backend-repair/ortlib"
export ORT_PREFER_DYNAMIC_LINK=1
export DYLD_LIBRARY_PATH="$ORT_LIB_LOCATION"
export PDFIUM_DYNAMIC_LIB_PATH="$ROOT/engine/vendor/pdfium/lib"
mkdir -p "$TMPDIR"

cargo test --manifest-path engine/Cargo.toml -p syncpdf-pdf -p syncpdf-pipeline --no-fail-fast -- --nocapture
cargo fmt --manifest-path engine/Cargo.toml --all --check
cargo clippy --manifest-path engine/Cargo.toml -p syncpdf-pdf -p syncpdf-pipeline --all-targets -- -D warnings
git diff --check

OBJECT_EVIDENCE_PDF=/Users/zhengcaiyi/Downloads/2106.04690v2.pdf \
  cargo test --manifest-path engine/Cargo.toml -p syncpdf-pdf --test repair_object_evidence \
  original_paper_object_evidence_probe -- --ignored --nocapture
```

主控独立probe源码和复现脚本：`桌面端/tmp/backend-repair/parent-review/object/{paper_object_probe.rs,run-paper-probe.sh}`。脚本选择当前cargo JSON中的确切rlibs，不复用旧二进制；执行23页源证据、p19实际删除、22页保护和qpdf。它会覆盖同目录同名日志/临时PDF，复跑前保存旧证据或把脚本/输出参数改为新的本树tmp目录。

```bash
SP_PAPER_PATH=/Users/zhengcaiyi/Downloads/2106.04690v2.pdf \
  bash /Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端/tmp/backend-repair/parent-review/object/run-paper-probe.sh
```

主树合并后的检查（**尚未执行，不是通过证据**）：在主树重新设置`ROOT/CARGO_TARGET_DIR/TMPDIR`，其余只读库路径同理：

```bash
cargo fmt --manifest-path engine/Cargo.toml --all --check
cargo test --manifest-path engine/Cargo.toml --workspace --no-fail-fast -- --nocapture
cargo clippy --manifest-path engine/Cargo.toml --workspace --all-targets -- -D warnings
cargo build --release --manifest-path engine/Cargo.toml -p syncpdf-cli
```

fake验收按当前CLI已存在参数运行；使用新的独立缓存/输出目录，不覆盖旧证据，先首页和第3页再全23页：

```bash
OUT="$ROOT/tmp/backend-repair/codex-fake-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
"$CARGO_TARGET_DIR/release/syncpdf-cli" translate \
  --input /Users/zhengcaiyi/Downloads/2106.04690v2.pdf \
  --pages 1,3 --translator fake:cjk --output "$OUT/translated.pdf" \
  --cache-dir "$OUT/cache" > "$OUT/events.jsonl" 2> "$OUT/stderr.log"
```

全篇时去掉`--pages`且换新OUT。**必须检查事件、文字、渲染与未就绪页，不可只信进程exit0**，CLI错误吞没仍有已知缺口。fake不代表真实译文质量；坏输入/叠印未解决前不再花模型费用跑全篇。

两种Pi不要混淆：开发主控迁移到Codex，不自动改产品的`syncpdf-translate/src/pi.rs`翻译provider。真实翻译目前仍使用已有pi适配器（历史验收`deepseek/deepseek-flash`、low）；工具/模型不可用须明确报告，不能拿fake当真实结果。

## 7. 后续路线与验收终点

仅在R1职责范围内的整批门禁通过后，才按执行计划拆下一批；后续既有缺口仍须如实列出，不能用“已记录问题”代替应通过的门禁。**当前R1 gate仍未过**：

1. **R2源文→发布一致性**：段落空格/阅读序/范围；准备成功后才删除并写入；快照不重放未删原文页；幂等与取消；错误传到run_finished与CLI退出码。`run.rs`若被transaction/status共用则串行，不并行抢文件。
2. **排版**：box/glue/penalty、段落级最优断行、中文禁则、中英间距、shaping cluster与字体fallback；不切坏连字/组合字，不靠极端缩字号遮溢出。
3. **公式/链接/容纳接线**：源公式/图形优先保留绘制，原子不仅有占位宽度还要有可见内容；链接身份贯穿译文，按最终行几何重建Rect/QuadPoints，不只是保留Annots；样式、障碍、加宽、合理缩字。
4. **固定译文与旧bdt/TeX oracle对照**：23页逐页检查内容完整/对应、文本可复制、墨迹字号、行距、碰撞、公式图表和实际链接点击；原生文本型学术PDF是当前范围，不宣称任意PDF/OCR全面支持。
5. **最后再评估性能**：冷/热缓存、首块/首页可读时间、模型/排版/快照分开计时；Document candidate clone和逐对象bounds采集成本未量化。损坏输出的6.5秒不算有效提速。

初始真实翻译只完成88/460段后因损坏人工停止，`tmp/accept-2106/translated.pdf`不是最终译文。根因列表见04报告；writer/stream/layout已有修复，其余必须逐项核实，不能从测试数量推导产品完成。

## 8. 证据与恢复索引

| 位置（相对主树，除注明外） | 用途 |
|---|---|
| `tmp/backend-repair/parent-review/object/` | 本次主控验收、独立probe、原件/删除日志、完整候选保全和worker报告本地副本 |
| `../repair-r1-bind/tmp/backend-repair/object-evidence/` | 本次worker精确delta、源码开工快照、基线补录red、合成测试日志 |
| `tmp/backend-repair/parent-review/{shared,identity,cmap/final}/` | 之前各项主控验收，不需要盲目重做调查 |
| `tmp/backend-repair/parent-review/{paper,p19}/` | p19原始red与原生调查主控复验 |
| `tmp/backend-repair/{recovery,recovery-astra}/` | 原超时任务保全与空映射red；历史，不是活跃任务 |
| `../repair-r1-bind/tmp/backend-repair/FIX-SINGLE-OBJECT-EVIDENCE.md` | 最后一次范围/契约；实现结果以验收与代码为准 |
| `tmp/backend-repair/dispatch-manifest.json` | 已补记结束/合并状态，最初model字段只是历史派发值 |

Pi run报告已复制到主树证据目录，不需要依赖`/var/folders/...`保留期目录、Pi会话恢复或Pi专有工具才能接手。旧Orca创建回执/workspace的in-progress标签不代表worker仍运行；实况以进程、Git和canonical task-state为准。

任务整体尚未完成，因此没有提前升格为成功经验；最终完成后由接手主控提炼到`docs/lessons/`，同步长期架构/参考文档、task-state完成状态和AGENTS当前任务入口。
