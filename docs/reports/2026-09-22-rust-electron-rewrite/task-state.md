# Rust PDF 后端修复 · 唯一 Task state

> 更新：2026-09-24；仅用户和主 Agent 可修改。主树 `feat/desktop-develop`。当前任务：五篇真实论文顺序验证 Rust 编译后端，**进行中**。此前CCS验收属于历史，不是本轮成功证据。

## 最新用户范围澄清（优先）

### 本次恢复与执行计划
- 用户要求继续，主控仅负责方向、拆解、亲审与简洁验收命令；RCA/测试/开发委派fresh `devin-swe2`，沿用Orca隔离树。每个验收阶段立即中文commit，无需批准自动进入下一篇。
- 阶段A已完成：亲审9文件diff后，仅撤出Agent自有未提交的review-feedback/提示候选；完整patch及新增测试移存`tmp/paper-iteration/deferred-translation/`。已恢复HEAD编译源码，用户AGENTS.md/TRANSLATE.md/untitled.md/cache原样保留。主控独立translate148通过/2ignored，Python入口49通过。后续必须重建release，旧二进制含已撤出候选；旧v18–20规则hash不可移植，复排优先v17同规则缓存，源身份变化走真实补译，不伪造hash。
- 阶段B已完成并提交825bef82：冒号followup run3c247166虽在硬限超时，但资产完整且进程已退出；主控亲审后独立验证、整合并真实v22验收（详见下方）。完整超时diff/status及脚本日志保留，不resume旧会话。
- 阶段C：Orca `../iterate-devin-link-audit`已创建，cdf81bfe干净基线；只读61条点击标签差异归因，brief=`tmp/paper-iteration/devin-link-audit-brief.md`，不改源码。之后当前编译版本完整复排和51页版面审查；纯模型缺陷不阻塞编译侧验收、不再新建翻译功能。DeepSeek编译侧验收后立即TRC→ALNS→VNS→2604.03136v6。未完成的检查不报通过。

- Goal 模式已关闭。用户本次要求汇报，并明确：**本轮目标是完善编译侧；纯翻译侧问题无需修改。** 不再为漏译主句、跨块语序、倍数措辞或术语漂移开发提示词/人工语义补译功能，也不以这些纯翻译缺陷代替编译问题。
- 仍须修源解析/公式与正文边界、字形和原子保护、排版、链接及PDF写回；保留所有数字/KEEP/覆盖/碰撞门禁，模型违规只记录，不靠放宽校验求通过。
- review-feedback 链路及未提交academic_rules/prompt候选属于翻译侧扩展，**已备份撤出主树，不提交、不计入编译侧成果**。历史v20-review-34为7/0、1主/0补救/6缓存，仅证明翻译反馈效果，不是编译缺陷修复；完整历史证据保留。
- 冒号阶段已验收：超时followup的两文件已主控亲审整合，独立脚本/主树pipeline284通过（17ignored），真实四专项通过，clippy/fmt/release通过。847段独立比较仅P29-014 atom2的text/range/source变化。经验已提炼至`docs/lessons/pdf-binding-and-render-evidence.md`。
- 最新整篇`deepseek-v22-compile`：沿用已提交规则/原15条词表、v17只读缓存备份，真实agy 1主/0补救/349缓存，351/0、0源冲突/覆盖缺口。88源原子缺墨迹0，34,545保护字/410目标不变，dual两侧135607/76110字及820链接无错，qpdf两份通过；主控已目视第29页，冒号已成为正文句末且公式完整。v21启动误写binary及agy独立thinking参数错误已留证，无模型调用。
- 第一篇完整编译/版面验收仍未结束；v22有60条标签差异、全51页视觉复核未完成。只读link audit run=`7fcbb395-c7cc-4fc9-879f-7761882551c5`（mission979497cd）针对历史v18的61条归因，树`../iterate-devin-link-audit`，现已complete/terminal observed、源码干净。worker将v18归为49正常TOC/10空白差异/2疑似右括号点击缺失；主控重跑v22为49/9/2。主控否决其未经证明的trim根因：prepare_labeled失败后可走whole-block且geometry用ink，字符advance框中心可在点击框外但墨迹仍全覆盖。新fresh只读run=`c2d8830f-2d26-44ea-9742-51c55936748e`（mission124fafc1）沿用已停写link-audit树，只核实这两处真实墨迹/实际分支；brief=`devin-link-ink-challenge.md`，15分钟收束/20分钟硬限。该run已complete/terminal observed、源码未改。实测两处是全角advance盒中心导致误报，whole-block墨迹点击范围正确；主控已看带框图并独立自测/重跑，v22为49TOC/9空白/2ink-covered。诊断脚本像素坐标使用clip原点而非pix.x/y，亚像素数值需矫正后再冻结，不以未经修正的0.007pt作精确结论。最终证据准备run=`4516b453-b9bd-401d-a721-071190e89f9a`（missione57bbfce）已在同一停写Orca树fresh启动，brief=`devin-final-visual-evidence.md`：矫正tmp审计坐标并准备v22全部51页带页码联系图/单页图，10分钟收束/15分钟硬限，供主控最终逐页审查，不改产品。链接误报经验已提炼至lessons/pdf-binding-and-render-evidence.md。其余四篇尚未开始。

## 当前任务：五篇顺序实测（优先于下方历史偏好）

- 用户完整目标：按根目录 `TRANSLATE.md` 更新翻译要求；先 DeepSeek_V41_Tech_Report.pdf，再 TRC时变（核心）.pdf、算法相关综述（ALNS可快速了解方法）.pdf、算法相关综述（VNS，可快速了解方法）.pdf，最后 `/Users/zhengcaiyi/Downloads/2604.03136v6.pdf`。每篇翻译→主控审查→根因定位→委派修复→主控验收；完成一篇自动推进下一篇，不等批准。
- 翻译使用 agy `gemini-3.8-flash-low`；断联后才改 pi `CNB/deepseek-v4-pro:high`。当前 registry 未列 CNB pro，未发生断联，尚未改用其他模型。
- **最新工程委派要求覆盖旧模型/fork偏好**：外部subagent调用本机`devin --model swe-2-high -p`；每项任务新会话，禁止fork/继承主控对话、禁止resume/continue。用户提示256k上下文，brief必须精简自足、明确目标/证据/权限/验收/停止条件。仍用Orca独立worktree、一树一writer、叶子不委派，主控亲审diff与独立测试；翻译模型不变。已联网读取官方commands并实查devin3000.11.1及models list中的swe-2-high，证据`tmp/paper-iteration/devin-cli-research/`。
- 先根因分析，修缺陷类别并加回归；不硬编码论文，不降低覆盖/写回门禁，不缩字/压行距求通过。沿用 Text宋体、1.0字号/1.5行距，输出单语及dual便于审阅；不改UI。
- 用户新增要求：每完成一个阶段性任务，必须按逻辑使用中文提交commit，并简要汇报后继续。此次已开始分批提交；不把未验证worker交付混入，也不提交用户既有脏文件。
- 计划和验收：[13-五篇论文迭代.md](13-五篇论文迭代.md)。全部实测证据在 `tmp/paper-iteration/`，不用 ~/.sp，不改用户未提交 AGENTS.md / untitled.md / cache。
- 初始核实：HEAD 43b54bc2；Rust CLI及bdt适配硬编码pi；TRANSLATE.md为旧Python [[S/F]]/id-label协议，不能原样用于Rust。主控负责提示词语义适配；独立worker负责agy通道，互不写同文件。
- agy worker `1220754a-0bda-4929-a4bf-5504c0d67e9f` 已complete、停止写入，Orca树`../iterate-agy-channel`。已快照整合通道并release实跑；主控补畸形status fail-closed和tool/tool_calls拒绝，保留闭合Markdown块即时交付测试。worker最终新增的Python转发测试已整合，最终全量整合检查待跑，不套用worker测试数。
- 环境根因：旧 `../repair-r1-layout` 被移除，旧env导致dyld缺ORT1.23.2退出134。已下载Microsoft官方相同版本到主树`tmp/paper-iteration/runtime/onnxruntime-osx-arm64-1.23.2/lib`，新环境`source tmp/paper-iteration/env.sh`；用新库DeepSeek 51页inspect成功。不要再用下方历史env。
- DeepSeek首轮真实全文`tmp/paper-iteration/deepseek-v1`已结束：agy/指定模型，1.0/1.5+dual，exit1、244成功/79回退/3送译前冲突，36/50页覆盖缺口。1主请求+14补救、0缓存命中；不是断联，不换模型。其余四篇未开始。
- 主控初审：410链接目标未变、44点击框移动、保护字符67235变化0、58公式墨迹缺失0、0KEEP泄漏；仅证明已写部分的这些守卫，不是完整验收。证据`deepseek-v1/audit.json`，重建inventory在`deepseek-inventory-v1/`，内部page为0基。
- RCA：59条link_target_unplaced中存在两类：普通引用prepare成功但geometry失败（正在核实空白字形无ink）；目录标题已中文但源标签literal匹配失败，不能猜位置放行。3源冲突来自inline_formula用loose字形盒判clip重叠：既有3e-5pt接边误差，也有分数/根号loose盒覆盖邻行的假墨迹，必须用实际ink证据而非放宽容差。36页缺33字为图标题、50页缺24字为图例，二者源Form内且检测Figure框裁掉顶部，不能归为漏译正文。
- 三worker已启动：workflow `ad1fe373-7d32-43bc-935c-b5ddda53e7b8`（mission da8cc56a），link=`fc715e59-21cf-4df3-b8ae-f71831609f77`、figure=`74d7f1df-51a3-4bbf-b3c0-f85b3e55c0dd`、formula=`debbc561-053a-4d96-aa8a-52bcef7d54e5`；Orca树分别`../iterate-link-geometry`、`../iterate-figure-coverage`、`../iterate-formula-ink`。brief在tmp/paper-iteration；曾共享target，后发现跨树产物污染（新增测试编译后执行0test）。已APFS clone成各树私有target，主控为tmp/paper-iteration/main-target并touch本树.rs强制重编；旧共享数字撤销，不clean共享目录。
- 主控补译实测已证实：one million→100万新增数字触发严格literal门禁；邻段window把同一专名外沿空白当泄漏。已加numeric repair明确数字字面数量（不放宽校验）及window trim源文匹配与回归。`validation-probe-v2`10页真实1主+5补救+53缓存，原11invalid全部通过翻译校验；仍6链接+2原子定位失败/36页gap，不能称全篇成功。capture-bin仅诊断tee，真实agy请求/响应保存在captured。
- 目录RCA：多条TOC行被合成1段，点线/页码和链接标题混合。主控已实现source_toc：仅完整明确本地Link+页码栏行拆分，点线/页码保持Other保护；真实inventory第2/3页变为32/23个单行应译标题。已授权link worker新增仅“精确拥有整段全部源字形”可映射整个目标块的分支，部分链接不允许扩大，等待交付。
- 作者名录已修：明确Author List标题范围内严格逗号分隔姓名列表保护，叙述/后续章节不误保护。`author-roster-v1`第46/47页缓存复排6/0、0模型、姓名/保护字符变化0，单语+dual存在；不是全文验收。
- figure worker已交付冻结并整合。主控补“全页Form不构成面板独占证据”守卫/回归；26专项+真实inventory通过，第36/50页57漏字归Figure、其它页区域不变，仍待全文保存审计。
- formula worker已交付冻结并整合，但主控发现并修正3点：Option字段缺失不会自动反序列化失败（旧IR保守None，生产每次重绑定）；簇中部分字符缺ink不能仅并已有盒；擦除/重放clip必须与碰撞box一致，否则tight碰撞通过却loose擦除伤邻字。新回归红→绿，3真实阻断段已可译；PDF像素验收待做。
- 余下排版RCA：P12-007/P16-033微小右越界是side-bearing平移排除了含SourceAtom的行。已允许整行连源原子刚性平移（源框/尺寸不改、无空隙仍拒绝），67 typeset测试通过；`atom-nudge-v1`零模型复排P16-033写入，P12-007排版通过后暴露已知link几何失败。P11-014/015、P25-004、P48-010障碍是原文下划线路径（样张已确认），不能降低碰撞门禁或删格式凑通过，待精确归属/重绘修复。
- 私有target五crate整合581通过/13ignored（wave-integrated-tests.log），release成功；闭合块测试曾2秒启动超时，保留握手证明并将测试限时15秒后重跑通过。全局diff-check仍报用户AGENTS.md既有EOF空行，不擅改。
- `deepseek-v2`明确仅缓存复排（--cached-from强制cache-only）：254/99，其中57未命中。随后SQLite只读备份到v3正常调用agy，`deepseek-v3`真实1主/0补救/296缓存、新译57块：256成功/97回退，0送译前冲突、0覆盖缺口，90link+3atom+4最终overflow；67公式缺墨迹0、59433保护字符变化0、410链接目标变化0。P09-003/P20-011已保存，P15-012暴露atom定位失败。不是论文验收。
- 下划线lane运行`c52c6ca0-60da-4843-9967-13939a05fc04`（mission fcfdd0c4），Orca树`../iterate-text-decoration`，brief `tmp/paper-iteration/decoration-brief.md`。已通过supervisor设计门批准：精确源paint OpKey归属，单纯S/s→n去原线，样式驱动目标重绘；无锚点/未知几何回退，禁止丢格式。主控补充tight-ink边界、同y多线不能直接判表格、原线宽/颜色保护约束。private target预clone，不共享；主控保留link/KEEP。
- 第一波workflow `ad1fe373`超时3600000ms，link子run `fc715e59` failed（3599946ms），其它两路complete。link已停写/无cargo进程；源码在其Orca树、branch zcyisiee/iterate-link-geometry、HEAD43b54bc2，差异已保存main tmp/paper-iteration/link-timeout-{all,owned}.patch与status.txt。按同协议恢复`c21445b8`只收取交付，现已完成冻结；主控审后整合link_text.rs并补空text continuation glyph仍有ink的回归，10专项通过。`deepseek-v4`零模型全文330/23，0送译前冲突/覆盖缺口；目录与普通空白link改善74块，但22链接准备失败+1最终overflow仍未完成。诊断`link-diagnosis-v4.log`证明重复作者/年份及换行名字链接失去身份；主控正在用源cite.*本地链接精确归属创建Citation KEEP，禁止猜候选位置。
- Citation源锚点主控已实现并通过2个专项：仅明确本地cite.*、连续精确源span、无跨段共享，创建Citation KEEP；非citation/未知仍走旧fail-closed。`deepseek-v5`真实1主+2修复+307缓存，352/1、0送译前冲突/coverage gap/link或atom定位失败，最终只P48-010排版回退；保存两PDF。其它3下划线段虽复排成功，源线格式仍待专修/像素验收，不算完整成功。
- 下划线worker第一交付partial未整合（只有cargo check）。恢复`30537de4`补闭环时compact后401(no body) failed；源树/branch zcyisiee/iterate-text-decoration /HEAD43b54bc2不变。已保存main tmp/paper-iteration/decoration-401-{all.patch,status.txt,source.rs}，同协议原模型重试，不切harness。要求不能全局去掉尚未成功翻译owner的路径障碍。同协议重试run=`a3d5cf3f-2bc0-42d2-9365-42e764e6aeb8`已完成冻结交付；主控已选择性整合owned patch和3个新文件，没有整体覆盖其脏基线。
- 主控完成Citation同源样式守卫（3专项）、目录目标measure（8 frame专项）；目录第2/3页55/0零模型，P03-035不再误折两行。全篇v6零模型353缓存仍352/1；34787保护字变化0、84公式缺墨迹0、410目的地不变；dual820链接、两侧全部字符身份/样式/位置通过，qpdf两PDF通过。详细误报修正和证据见计划账本。
- 下划线主控审查修正：W/n后裁剪状态不能清空；只绑定已知画笔/同尺度CTM下直接m/l/S路径，拒绝虚线/透明/未知效果；每个源下划线范围需非空锚点，link内部tag保留范围；目标线加入真实墨迹盒与后续障碍。回归实际红→绿，保留R6普通样式自由。`decoration-v1`真实补译4段，3页25/0，但目视发现3条空格短线残留，按同owner/同画笔/两端连续证据补归属，不扩大字形匹配。
- `deepseek-v7`全文352/1：P48已修复；新P20-011是公式分数线被误判下划线。现排除已由SourceAtom裁剪/重放负责的路径，回归通过。修正DECOR_ALL诊断原先只遍历3页的bug后，真实51页查到17笔归5段（另P24-001说明词underlined），源标签全部合理；证据`decoration-full-inventory/`。公式内线不能被重复拥有，混合源绘图原子与另加下划线仍保守拒绝。
- 最新五crate整合607通过/14ignored（`decoration-integrated-tests.log`）；release正常。`deepseek-v8-fresh`与`v9-fresh`均0旧缓存真实整篇请求，但模型分别将P19-006/P35-015的end写为下一块ID，后者还漏下一block行；捕获证明源输入边界正确、agy终态SUCCESS，是模型协议错误而非断联，未切提供方。门禁正确终止，均非成功PDF。已在规则中明确逐块自核end与当前block匹配，规则仍进入cache hash，下一次真实重译待跑。
- 加强提示后`deepseek-v10-fresh`仍在P12-029→P12-031处写错end，374.477s失败。三次均agy终态SUCCESS、无断联；重复盲重译未解决。RCA：严格parser正确拒绝坏边界，但run_prompt在成功完成的模型响应中也直接返回Transport，阻断已有有界补译。已实现并验证：只在provider明确Ok时丢弃损坏块及其后缀，已严格通过的前缀照常交付/缓存；对本请求仍未落定的源ID走既有最多3轮补译，不改parser、不猜/修ID、不接受损坏内容。provider Err仍原样上抛；若全部请求ID已落定却尾随坏数据，仍fatal，不能吞错误。达到上限回退/非完整成功，不扩上下文或新增分片入口。
- transport-repair叶子`8d618479-ee00-4fc7-b351-481f519c5000`（mission12c61adc，CNB指定模型/fork）已冻结/主控选择性整合3文件：Orca树`../iterate-transport-repair`，branch zcyisiee/iterate-transport-repair，HEAD43b54bc2；只写translator.rs与专项测试，main负责提示词/文档/最终验收。独占target=`该树/tmp/paper-iteration/transport-target`，已clone并touch源码。brief=`tmp/paper-iteration/transport-brief.md`，继承基线=`transport-base.patch`，不能整树合并。
- 主控补transport回归：晚到边界错误不得覆盖先前numeric错误与针对性repair note，红→绿。translate139通过/2ignored，release成功。另五crateClippy通过、CLI/protocol44通过、Python入口43通过、Ruff通过。v7历史产物：17源下划线移除且非owner水平线不变，10目标线对应文字（audit-decoration.py）；保护35187字变化0、410目的地不变、81公式缺墨迹0，dual两侧135607/77203字和820链接无误，qpdf通过；这不是完整论文验收。
- `deepseek-v11-fresh`0旧缓存真实全文1主+8补译，约465s走完，352/1、51页已保存，0源冲突/coverage gap。主响应P30-002/endP30-003错界之后109未落定项被严格丢弃并真实补译；所有块最终翻译校验通过。唯一最终排版fallback=P14-006。下划线/旧P48/P20无最终失败，但仍待本版全页审计。
- 新RCA（不是排版太长）：P14-006源已乱序为`imcshoaaspnt...`。PDF源三行基线328.463/314.913/301.364；两个√的loose高度18pt（font10.909），tight仅10.898pt，loose跨行并使group_lines按最后字形重叠链合成一行、按x交织。该页这两处无Formula检测区域，故inline_formula未归属/正规化。源输入乱码不能靠模型猜译或缩字绕过。证据`deepseek-inventory-v11/{source.json,atom-inventory.json,regions-13.json}`及`deepseek-v11-fresh/render/source-14.png`。
- 下一叶子范围：为带已证明overbar与完整根号/被开方字符的漏检行内根式恢复SourceAtom，复用原子墨迹/裁剪/邻行归属安全门禁，在行聚类前归一几何，既修阅读顺序又保留源根式（不能只Unicode重排而留下旧横线）。Orca树`../iterate-inline-radical`，HEAD43b54bc2，私有target=`该树/tmp/paper-iteration/radical-target`；基线`radical-base.patch`。只可改paragraph相关代码/测试，不改group_lines通用API、门禁、模型、CLI；方案不能安全落地则supervisor具体提问，不扩scope。主控负责文档/语义审计，冻结后才合并。
- radical worker `2bb6da38-3b79-4403-8948-9d1c5316a3f9` 已冻结交付，仅inline_formula.rs/tests.rs两文件增量；报告213单元通过/9ignored及真实P14回归通过，主控尚未独立验证。观察到66 tool calls超过brief40次，已要求立即冻结；没有另开harness。主控审diff发现候选横线路径身份、竞争路径、空Unicode墨迹与radicand缺ink需要强化，不直接套用worker结论。
- v11主控语义对照已读1–42页应译块（非全页图像验收），待修清单在`tmp/paper-iteration/deepseek-v11-fresh/semantic-review.md`：除根式源乱序，Formula框卷入正文标点；P20-002两端可见括号映射U+0002/3未受KEEP保护而丢失；P29两段KEEP数量/变量角色交换；model-in-the-loop误作人在回路；两处错字/跨页漏义。不得以格式门禁通过或保护像素审计替代语义验收。后续源修复改变身份的块真实补译，明确不合格模型块仅失效旧缓存并真正重译，不手写替代译文。
- radical主控加固已红→绿：仅唯一精确overbar OpKey可豁免，拒绝竞争路径/未知有墨迹字形/缺radicand ink/过粗笔，限定局部基线与完整被开方字符。真实P14专项1通过；paragraph专项通过、release完成。`deepseek-v12-radical`从v11只读SQLite备份后真实补译变更块（未cache-only），351/0、51页单语+dual、0冲突/缺口、exit0；根式段源合并减少2块。尚有已知其它语义/符号问题，不能接受整篇。
- 下一最小叶子：只修Formula源范围卷入外层正文标点，复用冻结Orca树`../iterate-inline-radical`，已同步主控根式版为新基线；brief=`tmp/paper-iteration/punctuation-brief.md`，限inline_formula.rs/tests.rs，不动radical_sources。主控同期独占独立source_opaque.rs/run.rs处理可见控制字形保护，并继续审计。新worker限30工具/2纠正轮/15分钟；必须冻结后再整合。
- 标点worker `608ee358-ff5b-483b-a6fa-f7796f797f89` 已返回冻结的未完成交付：只改inline_formula.rs，无测试，最后改动未编译；报告称接到截止提醒。该增量**未整合主树、未提交**，须先保存diff/核实再处理，不能当已修复。
- 主控可见控制字形保护已红→绿，3单元+1真实P20 inventory通过；源归属/墨迹冲突时明确阻断，Python计入blocked。提交前相关7个Rust crate共666通过/16ignored，Python入口44通过、Ruff通过。另审计发现ruled_code在新增PathStroke后错误仅接受None，补元数据不影响算法横线识别回归红→绿（专项4通过），7crate严格Clippy通过。控制符/算法横线补丁尚待下一次真实模型PDF验证，不套用v12产物。
- 中文分批代码提交：782581d8源墨迹/绘制事务；8652a9a9 agy/学术规则；eea96cb7边界错误有界补译；58f89f53源内容归属/目录引用下划线根式；2a99aa31可见控制字形保护。无push；用户AGENTS.md、TRANSLATE.md、untitled.md、cache保持原状。
- 文档提交87dca9fa；run.rs格式整理已提交129ed2be、Devin配置/规范4b26d835，fmt通过，未push。`deepseek-v13-opaque`已真实1主请求/0补译/350缓存：350/1、51页单语+dual、0送译前冲突/缺口；唯一最终失败P20-002 `atom_source_unplaced`。RCA已定位：source_opaque产生Symbol源原子，但text_atoms::resolve_text只准Formula源重放，分类接缝未贯通。不能把源inventory专项冒充端到端成功；新Devin叶子将修此门禁接缝并复测。
- 新Orca树均以87dca9fa为基线且新会话：`../iterate-devin-opaque`只修控制字形源重放接缝；`../iterate-devin-punctuation`只修公式外层标点范围。旧608ee358 run已complete/停写但验收rejected，其未测试候选只作只读参考，不整体继承或恢复旧模型。首发workflow `537cf996-9aed-4266-87f3-de034c55c45d` 已failed：child cwd未发现仅存主树的project agent `devin-swe2`，Devin进程/工程子任务均未启动。两树核实干净、仍87dca9fa；已复制相同runner配置到各树`.pi/agents/`并核对哈希/发现结果，作为主控预置只读基线；只按相同external-cli协议重试，不换harness。第二workflow `d99d9a5b-db4a-43ed-8375-d4f6de60ee58` 的smoke `91047754`已真实返回READY；但opaque `294ea9df-b001-40f9-b4a3-f27eadb247c2`、punctuation `c3c57061-ff6e-4713-bcdf-dfce5ca569c9`均在约143秒后provider `unavailable/retryable:true`失败，进程已终止，git核实仅主控预置.pi文件，无源码diff。实际runner argv=swe-2-high、forkContext=false；不是agy断联。保留原状态/日志，同模型同协议串行重试workflow=`dd948bdd-2165-4b6b-b21b-51e0302b8b2e`（mission cdbb9246）：先opaque `0ecfa005-03eb-491a-aaa0-6bce90a33dc2`，成功才启动punctuation；每项仍新会话，不能把探针通过当工程完成。重试日志已显示真实RCA活动；外部print无Pi toolEvents，60秒无事件告警本身不代表停工，不发不支持的steer/resume。
- opaque叶子`0ecfa005`已完成且明确停写，只改text_atoms.rs；主控保存owned diff后选择性整合。新增Symbol仅在source+精确span成立时进入原尺寸重放，缺源/文本不符/越界/缺style/Code或Other反例保持拒绝。主控实际执行text_atoms12通过/2ignored、source_opaque3通过/1ignored、真实prepare1通过；release、Clippy(all-targets)、fmt通过。`deepseek-v14-opaque-replay`全51页CoreML缓存复排351/0，0模型调用/351缓存、0冲突/缺口，单语+dual/qpdf通过。两控制符每个恰好重放1次、各170参考墨迹像素缺失0、源/目标控制码均各1枚，已亲看第20页及顶段；88源绘图原子审计无缺墨迹、34,545保护字不变、410目的地不变。语义/公式边界尚未解决，仍非论文验收。punctuation叶子`53b51cc3-9631-4edc-b144-5cea8e8283d5`在1800000ms硬上限超时，workflow dd948bdd随之failed；已确认进程terminal observed且无遗留进程，树仍87dca9fa、源码diff为空（只有主控预置.pi）。日志提出释放字形可能受Formula保护区域覆盖门禁阻挡，尚未主控实证；不能当修复。状态/空diff已保存devin-punctuation-timeout-*，需主控核实并缩小接缝后同harness新会话，不resume。
- 主控补完v11第43–51页应译配对语义审核并记录；全页图像未审完。v13保护34,860字不变、410目的地不变、84公式无缺墨迹、dual字符/链接审计无误、两PDF qpdf通过；这些结构证据不抵消P20回退和既知语义缺陷。详细账本见专项计划。
- 控制字形接缝与主控v14验收已提交3b9cc410。主控随后做`deepseek-v15-semantic-retry`：只从v14只读备份删10条确认错误的自动译文缓存，agy真实1主+3补译/341缓存，351/0，未手写目标。KEEP角色互换、错字得到纠正；model-in-the-loop仍错译人在回路、倍数措辞仍歧义；P34主句虽在P35出现但跨块迁移不满足源范围，术语漂移加重。证据`semantic-retry-pairs.md`；未验收。
- 新RCA：现有PromptSpec.terminology能渲染且强制映射，但run.rs仅构造空spec，bdt rust-translate未提供词表参数；缓存身份也未含词表，不能直接接词表后复用旧译文。拟委派单一端到端术语接缝叶子：复用babeldoc_tools/glossary.py CSV加载/校验，贯通bdt→sidecar→PromptSpec、术语感知缓存和回归；不做UI/服务/自动挖词/逐字替换，不硬编码论文词表。泛化跨块/倍数/KEEP语义提示也仅在本次真实失败证据范围改动；主控之后真实agy检验。新Orca树`../iterate-devin-terminology`基于3b9cc410，源码干净、runner已跟随；已把停写opaque树私有target搬来并touch新树源码（旧树不再共享target）。brief=`tmp/paper-iteration/devin-terminology-brief.md`，25分钟收束/30分钟自停/35分钟硬上限，零真实模型调用。主控15条候选词表在`deepseek-terminology.csv`（未硬编码进产品），CSV共享loader通过；既有v15 mono/dual结构审计与qpdf通过仍非语义验收。术语叶子已启动run=`2535ed87-0409-4326-b82c-f83c700a7eb6`、mission=`ce426f2c-12bc-4f40-ab41-de2c34a23018`，同external Devin swe-2-high/fresh。现已complete/terminal observed、冻结7文件diff（373+/26-），主控已保存devin-terminology-owned.diff，正在逐项审查。发现倍数提示用1/N会引入额外阿拉伯1而冲突数字门禁，必须改为保留N且用中文分数表达并补回归；worker测试不是最终验收。主控已选择性整合7文件并补倍数提示红→绿（原1/N会违规，现N分之一不增加数字）；纠正测试中误导的术语示例。主控3crate实际389通过/16ignored、Python49、Ruff/fmt/Clippy通过，release已建。
- `deepseek-v16-terminology-fresh`无旧缓存真实全51页：1主+8补救/0缓存、349/2、0源冲突/缺口。9个真实捕获请求均含词表；model-in-the-loop正确变为模型在回路，Full/Reuse/Reindex及prefill等改善；KEEP角色正确，跨页主句仍有迁移（不得称语义完成）。最终P22-004/P30-002为protected_literal_count：100.6 million被改1.006亿、September被改9月，补救仍重复；证据numeric-P22-004.md/numeric-P30-002.md，属模型数字措辞不遵约、非断联。82源绘图原子无缺墨迹，36,333保护字不变，410目的地不变，dual135,607/77,063字无审计错误、两PDF qpdf通过。
- 术语缓存真实入口验证：同表第21页CPU复排6/0且6命中；改单映射后0/6且0命中，均0主/0补救（terminology-cache-{same,changed}-cpu）。先试CoreML虽engine6/0，但stdout末尾混入Apple E5“Missing ... model.espresso.weights”原生日志，Python严格报engine_events_invalid；保留terminology-cache-same证据，不算通过，不清理用户Library缓存。CPU仅用于隔离验证；该CoreML输出通道问题待跟踪，v16全篇自身无无效事件。
- 主控补核标点接缝：paragraph.rs:43–50将所有非translatable区域中心命中字形标protected；:87–99只豁免source原子覆盖字形。故剥离Formula标点后，必须把已证明的正文标点归属同步传到这个门禁，不能只缩atom或去掉全部Formula保护。真实源P12-029/P12-031为正文逗号；P13-008同类逗号跨物理行后接“so it no longer...”可利用段落阅读顺序，不能只以行尾推断。下一标点任务应允许paragraph.rs最小归属接缝，先窄化可证明边界，保留未配对/内部下标逗号、数学区间及无证据情况。已准备`tmp/paper-iteration/devin-comma-brief.md`：只修尾逗号与归属接缝，20分钟自停/25分钟硬限；避免重复全仓RCA，先红→绿并验证三段真实before/after。为避免已发生的并发provider unavailable，术语叶子已终止，接续以同Devin协议串行启动尾逗号新会话；主控在主树审查术语diff，两者文件/树隔离。尾逗号run=`7535dbe2-35f9-42c6-8397-0d52de73b3c5`（mission ad4a1a0c）现已交付停写，报告3文件/三真实段释放成功，尚未主控审diff或整合；必须独立检查数学上下文反例和释放GlyphId的最终owner接缝，再真译。括号/冒号仍明确待解决。
- 术语链路已提交8e31d3a9；主控确认逗号7535dbe2 complete/terminal observed，owned diff+status存devin-comma-*。初审需反例验证：单字母后继不等于正文、Formula框外未闭合定界符、全页released集合可能误豁免不同最终owner。尚未整合。
- 新数字反馈叶子已准备：Orca树`../iterate-devin-numeric-repair`，8e31d3a9干净基线；brief=`tmp/paper-iteration/devin-numeric-brief.md`。仅给数字违规补救附与validator同源的逐块数字多重集，泛化单位/拼写月份提示；不改门禁、初始请求/学术规则/缓存身份/重试上限，不真实模型。15分钟自停/20分钟硬限。私有target从停写术语树搬来（旧树不共享），主控继续独立逗号审查。数字run=ab86f926-fef3-4782-a589-400b4342d894、mission9f29854f，现已complete/terminal observed，4文件diff+新测试已快照；主控已整合，helper收窄pub(crate)，保留原validator比较行避免重复解析，补实际发送前容量上限回归。translate148通过/2ignored、Clippy/fmt/release通过。`numeric-probe-v1`第22/30页CPU真实1主+2补救/11缓存，13/0；实际两次补救清单分别正确含100.6和不含9，最终100.6百万/2026年九月，数字门禁保持。亲看两页，7源绘图无缺墨迹、129,528保护字/410目标不变、dual135,607/131,982字审计无误及qpdf两份通过。仅此两页数字修复验收，不是全篇/语义完成。
- 主控逗号反例实证5类误放行并红→绿：单字母Roman后继、框外开定界符、错配括号、非finite ink、跨最终paragraph借用released。现要求连续两个完整正文词、定界符栈及最终attached atom同owner；未知后继不跳过。三真实释放GlyphId/clip精查与根式回归通过，pipeline236通过/12ignored，Clippy/release通过（中途collapsible_if已修）。`comma-probe-v1`真实1主/0补救/17缓存，两页20/0，三新译文标点位置正确；但再次CoreML原生stdout污染而bdt engine_events_invalid，不能称端到端成功。
- 新stdout隔离叶子：Orca `../iterate-devin-stdout`，8e31d3a9基线，brief=devin-stdout-brief.md。RCA为原生fd1与StdoutSink共用、析构后仍可能吐日志；拟安全保留原stdout专供JSONL，将原生stdout导stderr，run/translate启动前隔离且保留到进程退出；forbid unsafe、Python门禁和错误传播不放宽。仅CLI/events与必要安全FD依赖/测试，15分钟自停/20分钟硬限；私有target从停写数字树搬来，主控继续数字/逗号整合。run=c4d9e549-0d1d-4da7-ab43-71e282b094a3、mission38404ac9已启动；60秒告警后status仍running，外部协议不支持steer/resume，不将告警当停工。主控已亲看comma-probe第12/13页的三处正确标点，仍待无stdout污染端到端与源墨迹审计。
- stdout叶子c4d9e549已complete/terminal observed，6文件快照`devin-stdout-owned.diff`，主控整合后发现普通dup会让模型子进程继承事件FD。独立CLOEXEC回归真实红→绿，改为原子fcntl_dupfd_cloexec；Unix专用Event测试import也已cfg内置。CLI15、events7、Python49通过，7crate696通过/18ignored；首链240秒超时发生在Clippy阶段，独立重跑七crate Clippy与fmt通过。保持forbid unsafe、错误传播和Python严格门禁。
- `semantic-scope-probe-v1`仅从数字probe只读备份失效P01-007/P34-007/P35-021三条自动缓存，真实1主+1补救/16缓存、3页19/0。P01倍数已变4分之一/437分之一；P34→P35主句迁移仍在，说明缩短请求本身不能解决，不能盲重试或当已验收。
- 最新`deepseek-v17-cached-integrated`全51页CoreML复排351/0、351缓存/0模型，2395行事件全合法，单语+dual存在；88源绘图无缺墨迹、34,545保护字/410目标不变、dual135,607/76,105字及820链接审计无误、两PDF qpdf通过。本次未出现原生噪声，FD隔离机制由子进程噪声回归证明，不宣称本次触发了E5问题。仍有括号/冒号边界、跨块语义及全51页视觉审阅，未验收首篇。
- 主控847段全源inventory独立比对通过：仅P12-029/P12-031/P13-008的atoms变化，其余字段/字形/归属不变（comma-controller-inventory-diff.json）。首轮比较因测试遗漏front-matter策略造成P01作者段假差异，按生产补同策略后重比，非产品改动。三处实际释放精确GlyphId、源clip不碰逗号墨迹；v17全部源原子审计及已亲看的12/13页证明端到端尾逗号修复。经验见[最终归属与stdout隔离](../../lessons/pdf-binding-and-render-evidence.md)。stdout提交797fcd6d，数字2bcede98；尾逗号已独立提交ae020978。括号/冒号仍不在本修复覆盖内。
- 主控亲看v17第9/29页，确认P09-003 KEEP1的正文闭括号与模型补括号重复，P29-014的源尾冒号卡在“回复z_b,j:的奖励中：”内。源证据external-punctuation-evidence.json；同页成对包围W变量的另外两个括号目前显示正确，不盲扩大修复。新Orca `../iterate-devin-closeparen` ae020978基线，仅inline_formula/tests修“匹配开端和正文词在Formula外”的单侧外闭括号类别；brief=devin-closeparen-brief.md，20分钟自停/25分钟硬限，私有target从已停写stdout树搬入。主控独占学术提示词/语义实测，暂不改冒号源范围。run=c3b557e7-519e-46c2-b2d9-a021d6944f59、mission0bf42833已启动，60秒告警后status仍running；不发不支持的steer/resume。
- 主控新增提示候选：跨块主从句泛化示例及KEEP边缘正文标点句法（不放宽任何validator），测试红→绿，translate148通过/2ignored、release通过。`semantic-rules-probe-v1`第29/34/35页真实1主+1补救/0缓存、25/0；P29冒号已在引出位置无“z:的”嵌入，但同一请求内P34→35主句仍迁移，提示示例不是语义保证。RCA支持请求内跨块中文语序耦合；用现有--pages逐页各只失效1条已知错误自动缓存，`semantic-isolated-34/35`各1主/0补救/6缓存、7/0，谓语分别保留原块，未手写目标或改默认one-shot。发现robustness名词译名漂移，外部v2词表仅新增稳健性映射，旧15条CSV保留供复现。
- `deepseek-v18-fresh`当前候选规则/v2词表零缓存全文完成：1主+5补救、351/0、51页、0源冲突/缺口。88原子无缺墨迹、34,545保护字及410目的地不变，dual审计无错误、两PDF qpdf通过；61个点击标签差异仍须核实。倍率/冒号/跨块主句问题复现，不能把提示示例或351/0当语义保证。当前351配对文件与51页图在该目录，完整审阅尚未完成。
- 闭括号叶子c3b557e7已complete/terminal observed、冻结2文件，主控整合后用反例证实三缺口：配对借用另一最终段、开括号无墨迹、找到两词即提前返回漏查后续保护内容。已红→绿加固，记录完整配对字形并验证最终owner；又实证字体变化会把单词片段当两个完整词，改复用严格正文词证明。补未知下标字形不能被基线过滤漏过和真实P09精确GlyphId/clip回归。主控pipeline254单元+9集成通过（16ignored，相关真实项另跑）、真实闭括号/三逗号/根式、Clippy/fmt/release通过。847段独立对比仅P09-003 atom1变化，W成对括号原子不变；证据closeparen-controller-*。
- `deepseek-v19-page-9`只读复制v18并删除已过时自动P09-003行，真实1主/0补救/7缓存、8/0。亲看第9页，正文闭括号已脱离KEEP且无重复；11原子无缺墨迹、132,473保护字/410目标不变，dual两侧135,607/133,737字、0错误及qpdf通过。最后完整单词加固后的closeparen-final-replay为8/0、0模型，51页1.5×像素与前一真译产物完全相同，审计/qpdf再次通过。这只验收闭括号类别，不包含冒号/全篇语义。经验已提炼至lessons/pdf-binding-and-render-evidence。
- 随后逐页仅删除一条已知错误自动缓存，v19-page-1/29/34/35均真实1主（第1页另1补救），缓存4/10/6/6；P01比例正确为4分之一/437分之一，P35不再借主句，但P34主句直接遗漏，P29冒号仍卡在KEEP定语内。逐页隔离不是泛化修复，不能盲重试；最新完整缓存在v19-page-35，未手写任何目标。候选academic_rules/prompt仍未提交，规则不构成质量保证。后续需修冒号源边界，并研究如何把明确人工审查反馈交给真实模型而不是无反馈重采样。
- 闭括号已提交cdf81bfe。下一冒号叶子：Orca `../iterate-devin-colon`，cdf81bfe干净基线；brief=devin-colon-brief.md。仅inline_formula/tests证明段末冒号引出紧邻独立公式，仍须严格末字形/完整正文词/配对/墨迹/最终归属证据，不能泛化数学比例/映射冒号；不安全则停写报告。20分钟自停/25分钟硬限，fresh外部Devin swe-2-high；私有target由已停止closeparen树搬入并touch源码。主控只读后将独立验收，同期研究人工语义反馈接缝，不改该叶子源码。首run=54a9aab3-2516-4ef2-8f2f-5d851e26a81e、mission dae88c06已在约401秒后provider unavailable/retryable失败，terminal observed；核实该Orca树仍cdf81bfe、源码干净，快照devin-colon-failed-*。不是agy断联；同harness/模型新会话重试一次，不resume或换执行方式。重试run=4f0729bb-b9ae-440a-971f-98f0f25f8596、mission4f22d982已启动；注意力告警后status仍running/terminal pending，不向一次性runner发steer。
- 人工反馈RCA实测：v19-page-34真实请求明确包含完整主句`Its performance remains robust`，不是源解析漏文；Rust只有数字/格式自动repair反馈，没有人工逐块语义反馈入口。用既有`bdt harness-call`的agy Flash/low profile做两次隔离诊断（feedback-diagnostic{,-v2}，未入缓存/未生成PDF）：仅提醒漏主句时模型补出了源块没有的“发生变化”，还把end写在同一行；进一步诊断要求保持原主从句顺序、尾部仍是未完名词短语后，模型保留了主句且未编造续句，边界正确。不是通用可靠性或最终PDF验收，不据此放宽门禁。
- 下一主控实现方案（尚未实现）：给现有bdt/Rust链路增加源绑定的逐块审查反馈，而非继续全局改规则/盲采样。候选JSON记录id、精确source_html、message；验证所有ID/源一致、拒绝manual目标与cache-only后才使已指明的自动缓存失效，真实重译沿用全部门禁/既有补救次数。反馈只作同一源规则下的一次修复诊断，不是新翻译规则，不伪造新hash或手写目标；成功译文按原规则/术语身份正常入缓存，后续无反馈可复排。需验证当前源绑定、手工保护、无反馈兼容、主/补救请求作用域、容量上限、失败前不发送/不误交付，再用P34真实入口检验。若改为持久翻译约束，则必须进入缓存身份；不能混淆两种语义。
- 最终仍需完整真实重译、术语与逐页语义/版面/链接审核及最终整合检查。之后按顺序自动进入余四篇，尚未验收任何论文。

> **最新浮动排版验收：1.0字号/1.5行距，21页、190块全部写入，0回退、0送译前冲突、0覆盖缺口；最新Text宋体产物text-serif-v3，见[宋体验收](12-MVP真实翻译验收.md#text-serif-font)。** [标题验收](12-MVP真实翻译验收.md#heading-adaptive-layout) · [验收报告](12-MVP真实翻译验收.md#inline-formula-coverage) · [经验总结](../../lessons/pdf-binding-and-render-evidence.md#inline-formula-ownership) · [缺陷及历史误报](../../issues/rust-inline-formula-coverage.md)。

## 用户意图与边界

- 修复Rust核心翻译后端：所有应译正文实际送译并正确编译，作者/机构/脚注/图片文字/reference等保持保护。用户已开放全部权限继续完成；本轮主 Agent 单独执行，不启动旧worker。
- YAGNI、先实测后优化；用户指定样本为主树 `ccs2026b-paper3764.pdf`。不做UI/Electron，不重复p19绑定调查，不用额外重构延迟实测。
- 用户最新反馈当前行距太紧，希望至少1.5倍行距，并寻找合适字号；原0.9字号/1.3行距仅为历史基线。已用浮动空间修复支持1.0字号/1.5行距，保持样式层级；公式保持原尺寸，仅在发生碰撞处增加所需行距。不通过缩字号/压行距/扩大碰撞容差求通过。
- Markdown one-shot，首个闭合有效块立即编译；主请求/补救分别计数。真实缓存复排不能冒充新的模型测试，不伪造译文缓存。
- R6用户决定继续有效：允许模型换序/拆合/复用/省略已知样式，删除目标语言字符比例门槛；保留块/KEEP/数字身份及PDF写回安全。
- PP-DocLayout-V3、CoreML CPUAndGPU；生产不调用LaTeX。唯一对外入口 `bdt`，Rust binary仅内部sidecar。
- 按逻辑分批提交，使用fix/feat/docs分类和简洁中文说明；不改写历史、不push。既有 `cache/` 保留且不入库；不reset/clean/stash。运行产物仅在仓库tmp，不改共享conda，不用 `~/.sp` 作测试库。
- 若以后委派，先读委派规范；原偏好为gpt-6-sol:high、fresh context、Orca独立worktree，一树一writer，叶子不委派/不写本文件/不push。用户未要求本轮新增委派。

## 正文覆盖修复验收（最新断行样张见下节）

**样张：`tmp/backend-repair/inline-full-v5/translated.pdf`。** 同目录保存事件、result、audit、qpdf日志、重点页PNG及cache来源；详细过程/失败尝试见验收报告。

- 193应译块全部写入，21页保存；576正常保护实体；run_finished=true、exit0。原第2页四块正文实际送译，第20页漏检及狭窄子标题全部写入。
- 最终1真实主请求/0补救/183真实缓存命中，补译10块，65.989秒。前序page2和全文运行也实际调用模型。最终cache由v3/v4原hash原译文合并，未手填内容。
- 94行内公式原字体/路径核验，53,384参考墨迹像素缺失0；成功段源框外36,327字符位置/字号/颜色变化0，第3页图片区域像素相同。
- 319链接目标保留，314点击框移动、全部点击标签对应；28书签/180命名目标保留。qpdf通过，无KEEP残留。
- 已目视p1/2/3/4/5/9/20；仍有源段落锚定留下的空白，不把覆盖通过扩大为任意PDF或模型措辞质量认证。此样本匿名，作者保护依靠已有前置信息行为守卫及本轮未改变的保护策略。
- 相关五crate528通过/0失败/8ignored；随后协议补译更改重跑translate115通过/0失败/2ignored。CLI/单入口36pytest；严格Clippy、fmt、Ruff、release、diff-check通过。未另跑全workspace/前端。
- strict docs仍有既存HTTP参考中文锚点警告，未当通过；本轮移除状态页过时链接并修正所编辑Rust参考的CLI链接，没有扩修其它文档。

## 本轮实现入口

- `paragraph/inline_formula.rs`：公式完整源归属与独立clip证据，上下标按正文基线分行；正文重叠按较小区域唯一拥有字形。数字单位正则补词界，避免`2 shows`的s误作单位。
- `syncpdf-core`的 `Atom.source` / `LineBox.placed_atoms`，`syncpdf-typeset`的 `Inline::SourceAtom`：KEEP与原尺寸几何贯通，局部必要行距、源/目标障碍同时核对。
- `syncpdf-pdf/src/source_atom.rs`：不可变源绘制隔离为Form，保留原字体/路径；仅私有副本剥离无关和嵌套图片文字，成功页裁去原公式路径后重放。仍在页级候选事务内。
- `layout.rs` / `frame.rs`：有标签、单行、紧邻且居中的图表外子标题恢复；按面板宽度居中排版，保留其它内容障碍。
- `link_text.rs`：公式内引用按实际原子位移更新注释，保留Dest/A。
- `markdown.rs` / `translator.rs`：只有正确闭合且ID可识别的坏正文进入既有最多3轮补译，不交付/缓存坏块，后续有效块照常流式交付；半块/边界损坏/通道失败仍报错。
- `rust_backend.py`：结果增加 `blocked_before_translation` / `coverage_gap_pages`，源冲突计入unsuccessful，覆盖缺口不能完整成功。

## 表18/19断行复核（2026-09-23，已完成）

用户指出表18/19断行不自然。已证实译文无换行/br；根因是无glue片段的短行代价过低及左对齐仍按justify选择断点，末行不计余量又留下孤字短尾。已按实际对齐评分、限制短行兜底、均衡ragged末行并避免自动单字末行；保留显式硬换行。新断行暴露的0.031pt右侧墨迹越界，仅用另一侧空隙平移纠正，没有缩字或放宽容差。

**最新样张：`tmp/backend-repair/caption-break-v3/translated.pdf`。** 21页193/0，0源冲突/覆盖缺口；193缓存命中/0模型请求，28.095秒。表18从9行→8行、最短中间行40.9%→78.0%；表19从7行→6行、14.3%→97.8%。193译文HTML和保存PDF非空白字符总量/身份不变。94公式、保护字符、图片、链接和qpdf全部通过；已目视第20页说明段。

234相关Rust单元/集成通过/5ignored；末行评分最后一处更改后typeset重跑65通过；Clippy/fmt/release/diff-check通过。文档仍有既存HTTP锚点警告。v1的192/1及v2收尾仍短的中间证据保留，不冒充最终结果。详细[验收](12-MVP真实翻译验收.md#caption-line-breaks)和[经验](../../lessons/pdf-binding-and-render-evidence.md#mixed-script-break-cost)已同步。修复代码提交 `6245d0f5`，文档另批提交，未push。恢复复排优先使用最新样张目录；inline-full-v5缓存文本未改变，仍可复用。

## 标题语义与自适应宽度（2026-09-23，已完成）

用户要求commit前轮更新并继续解决短标题被迫换行。前轮6245d0f5/8dbb6836已提交。首页两个标题块同属Title区域，居中短续行Merging因左缘缩进误切；附录A/A.2又因编号后的悬挂缩进误切。已按同区、相近字号/行距及中心轴或编号后正文对齐证据合段，整段真实翻译。目标Frame从下方正文确定页/栏宽度，同排邻块/图片及实际墨迹继续阻挡，缺证据沿用原框；不改源IR、不缩字。

**最新样张：`tmp/backend-repair/title-adapt-v2/translated.pdf`。** 首页完整标题与A.2各一行；附录A完整译文超过栏宽，正常两行。21页190/0、0源冲突/覆盖缺口、176缓存+14真实补译、1主请求/0补救、56.255秒。此前v1仅修首页为192/0、179缓存+13真实补译、60.823秒；不把v1当最终全部标题验收。缓存只读备份，变更ID未篡改命中。

66,916应译源字形身份/数量不变且唯一归属；第1/14/15页以外156译文HTML及保存文本不变。94公式/53,384参考墨迹像素缺失0、36,327保护字符变化0，第3页图片像素相同；319链接/28书签/180命名目标正确，qpdf通过。已目视首页和附录两标题；仍保留源段落锚定空白。

pipeline167通过/0失败/4ignored；最后加强新编号守卫后专项1通过；真实inventory1通过。Clippy/fmt/release/diff-check通过，strict docs仍有既存HTTP锚点警告，未跑未改动Python/UI。首次比较脚本错误要求附录A也一行，实测译文较长后纠正验收预期，没有改译文/缩字求通过。详细[验收](12-MVP真实翻译验收.md#heading-adaptive-layout)及[经验](../../lessons/pdf-binding-and-render-evidence.md#heading-semantic-container)已同步。证据和日志均在tmp/backend-repair/title-adapt-*，缓存复排请使用v2。代码提交98782120，文档另批提交；未push。

## A3双语导出（2026-09-23，已完成）

用户要求commit现有更新并新增可选dual PDF：每页A3横向，左原文、右译文。前轮标题代码98782120/文档d819cffa已提交。本轮`bdt rust-translate --dual`经Rust生成额外`dual.pdf`，保留`translated.pdf`；PDF内容流/Form矢量拼页，按CropBox/旋转等比适配半页并居中。链接坐标/目的地同步变换，右侧同名目标解析为右侧显式目标，保留一套原文目录；发布前自检及请求产物存在门禁生效。未做UI/新并行入口/编辑Export协议。

**最终样张：`tmp/backend-repair/dual-v2/dual.pdf`。** CCS21页，每页420×297mm；190真实缓存命中、0模型请求、31.510秒，190/0、0源冲突/覆盖缺口。左右分别101,080/61,110非空白字符逐页与两输入相同；638链接点击框/目标/坐标、28书签、180命名目标通过，qpdf/保存后自检通过。已目视首页、公式页、表18/19；v1与补充门禁后的v2双语文件SHA相同。

相关Rust281通过/0失败/4ignored，门禁专项1及流读取dual专项2通过；CLI/单入口39pytest通过；Clippy/fmt/Ruff/release/diff-check通过。strict docs仅既有HTTP锚点警告。导出失败不覆盖输入/伪报成功；任意交互批注/表单/标签阅读树未认证。详细[验收](12-MVP真实翻译验收.md#a3-dual-export)及[经验](../../lessons/pdf-binding-and-render-evidence.md#dual-page-geometry)已同步。产物和日志均在tmp/backend-repair/dual-*。功能代码提交2b3493a4，文档另批提交，未push；既有cache保持未跟踪。

## 1.0字号 / 1.5行距与浮动bbox（2026-09-23，已完成）

用户要求行距至少1.5，并追问既有浮动bbox能否支持原字号。三档初测：0.9/1.5为190/0，0.95/1.5为189/1（P09-017），1.0/1.5为188/2（另P14-114）；均190真实缓存命中/0模型请求。0.9版本完整保护审计通过，仅作为比较基线，不能以缩字号代替浮动空间修复。

旧Python已有同栏/跨栏和条件跨页；Rust之前只做最多3轮同栏上下回收，固定横向宽度且不能让相邻已译段让位。本次在原页文字右边界内尝试向右扩展，重新核对全高障碍；还支持与紧邻下方已译段保持顺序、原段间距联排，两段都通过才一起更新。

真实v2：1.0字号/1.5行距、21页190/0，0送译前冲突/覆盖缺口，190缓存/0模型，31.768秒。P09-017实际是图4说明（此前称正文不精确），加宽21.662pt后断行成功、首基线下移0.298pt；P14-114附录标题和P14-115下方正文各下移1.617/6.459pt，两行标题保持栏宽。v1也190/0，但标题侵入右页边距，目视拒绝；v2约束原页文字边界后修正。没有跨页，不改默认字号/行距。

v2保护字符36,327变化0、94公式参考墨迹53,384缺失0、图3像素相同；638双语链接及28书签/180命名目标通过。原矩形链接审计将首页跨两行DOI的联合Rect内邻文误报为错误标签，逐行QuadPoints精查仅含完整URL且目标正确，证据wrapped-link-audit.json；下一轮使用逐行区域审计，不能把旧误报忽略不报。最终v3：21页190/0、32.113秒、0模型请求，两份PDF与v2 SHA一致；逐行链接标签0错误，单语/dual完整审计及qpdf通过。pipeline单元172+集成7项，共179通过/0失败/5ignored，Clippy/fmt/release通过；strict docs仍仅既存HTTP锚点警告。产物tmp/backend-repair/typography-float-v3/{translated.pdf,dual.pdf}。详细[验收](12-MVP真实翻译验收.md#typography-local-float)和[经验](../../lessons/pdf-binding-and-render-evidence.md#local-float-and-leading)已同步。修复代码提交4567c253，文档另批提交，未push。对比图仍在typography-comparison，未更新的1.0标签指修复前结果。本轮检测到用户同步修改AGENTS.md，保持其改动且不纳入本次提交；cache仍未跟踪。

## Text正文改思源宋体（2026-09-23，已完成）

用户明确要求仅Text部分使用思源宋体，其余字体样式不变。目标spec对Text选择衬线，并包含未标记样式StyleId(0)；全局Body字体链/源IR/缓存不改。保持1.0字号/1.5行距及bold/italic/mono/颜色，标题、Abstract、Caption等保留原字体。

首轮v1为187/3：P05-047/P11-024因两端对齐按advance拉伸，衬线实际墨迹超过框边；仅减少新增字距拉伸（不低于自然字距），不缩字。P14-114标题联排受此前“只连一个邻段”限制；扩为同栏连续段逐段纳入，保持各自宽度、原段间距和顺序，全组通过才更新，禁止跨固定障碍。实际第14页4段联合排入。v2为189/1且遇已知CoreML原生stdout日志污染，不能当成功；v3正常。

最终样张`tmp/backend-repair/text-serif-v3/{translated.pdf,dual.pdf}`：21页190/0、0送译前冲突/覆盖缺口，190真实缓存/0模型，32.257秒。字体审计117个Text的23,123个Noto字符仅族名Sans→Serif，粗细/字号/颜色/字符身份不变；73个非Text块字体样式字符计数全部相同。94公式、36,327保护字符、图3、319单语/638双语链接、28书签/180目标及qpdf全部通过。已目视第2/14页，保留布局锚定空白。

修复代码提交493d2734，文档另批提交，未push。相关typeset/pipeline248通过、0失败、5ignored；Clippy/fmt/release通过，strict docs仍有既存HTTP锚点警告。详细[验收](12-MVP真实翻译验收.md#text-serif-font)及[经验](../../lessons/pdf-binding-and-render-evidence.md#semantic-font-and-ink)已同步。开始时AGENTS.md为用户未提交更改，TRANSLATE.md/untitled.md/cache为既有未跟踪文件，均保持且不纳入本次提交。

## 状态与下一步

本次漏译修复、表18/19断行、标题适配、A3双语导出、1.0字号/1.5行距浮动排版及Text宋体切换均完成并通过对应验收。已将经验提炼到上述lessons链接，架构/参考/CLI/issue/验收同步。代码按逻辑提交：`67958e9c`（源公式/正文/子标题）、`898ca829`（闭合坏块有界补译）、`d306583e`（完整覆盖统计）；文档另批提交。运行缓存与产物不提交，未push。

后续产品范围仍待用户安排：中文目录/书签、逐块字体/字号/译文编辑与原子revision重编译。任意公式布局、任意语言/注释格式、RTL ActualText仍未全面认证。CoreML原生stdout偶发污染风险未在本轮处理。旧23页论文R6后重译未完成，本次CCS结论不能套用旧样本。

## 环境与恢复

主树：`/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端`；唯一状态即本文件。无活跃子代理/翻译任务，不恢复旧worker。

```bash
source tmp/backend-repair/codex-r1-integration/env.sh
cargo build --manifest-path engine/Cargo.toml --release -p syncpdf-cli
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools rust-translate \
  ccs2026b-paper3764.pdf --workdir tmp/backend-repair/<新目录> \
  --cached-from tmp/backend-repair/title-adapt-v2 \
  --layout-device coreml --font-scale 1.0 --line-height 1.5 --dual
```

上面仅缓存复排；新真实翻译去掉`--cached-from`。ORT只读库仍在 `../repair-r1-layout/tmp/backend-repair/ortlib`，不能删该树。开发日志 `tmp/backend-repair/inline-{tests,protocol-tests,pytest,clippy,build5,docs}.log`；审计脚本 `inline-audit.py`。

历史：R4–R7从6e61bb19后按逻辑提交。旧 `ccs3764-final` 为154写入/3回退，另40送译前冲突和1覆盖缺口；此前报告误导已纠正。历史样张/失败保留，详见累计验收与issue。不能把旧“3回退”当成整篇仅3段未译。
