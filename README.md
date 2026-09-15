# BabelDOC 中文增强版

本项目基于上游 [BabelDOC](https://github.com/funstory-ai/BabelDOC)，用于把英文论文翻译成中文 PDF，并生成原文与译文对照版。

## 相对上游的改动

### 超链接保留与对应

上游流程在译文长度变化、跨行或文本被改写后，可能把链接放到错误的文字上。本项目增加了以下处理：

- 解析阶段保存每个链接的源页、矩形、动作、目标页和覆盖字符。
- 重建阶段按优先级定位译文中的对应区域：LaTeX `bdoclink` 标记、存活字符、段落内文字锚点、段落比例映射。
- 链接矩形直接更新到现有 PDF 注释，保留 `/A`、`/Dest`、URI、内部跳转、远程文件动作和边框属性。
- 链接跨行时拆成多个注释，并保留同一个逻辑链接 ID。
- 更新矩形时清理过期 `QuadPoints`，避免点击区域仍停留在旧位置。
- mono 和 dual PDF 都会复制内部目标坐标；跨页目标按目标页尺寸映射。

### 链接审计

`babeldoc/tools/agent/link_audit.py` 会逐条比较源 PDF 和译文 PDF，检查：

- 链接数量和动作是否保留。
- 内部目标页和坐标是否一致。
- 引文、图、表、公式、脚注编号是否覆盖正确文字。
- 外部 URI 是否保留。外部网站只检查地址，不访问网站。
- 区分 `missing`、`wrong_label`、`wrong_role`、`source_invalid` 和 `external_unchecked`。

CLI 入口是 `bdt check`（三合一：结构审查 + 排版 lint + 链接审计），审计结果落在
`agent/link_audit.json`。

### Markdown 翻译管线

`skills/document-translate/` 提供可恢复的 `bdt` 子命令管线：

```text
bdt parse → bdt translate → bdt apply → bdt build → bdt check → bdt report
```

翻译输入使用带段落 ID、样式锚点和公式锚点的 Markdown。写回阶段会检查 ID、锚点顺序、公式占位符和段落完整性。翻译与审查都通过可替换的子进程命令注入（stdin 收提示词、stdout 出结果）。

### 解析和排版检查

- 解析后检查版面字符覆盖率、目录条目、保护内容和行内公式匹配率。
- 参考文献、作者信息、页脚、图内文字和表格内部文字默认保留原文。
- 参考文献后的 `Appendix`，以及有明确字母编号序列的附录标题可以恢复翻译；孤立标题不会自动恢复。
- XeLaTeX bbox 排版默认开启，失败的段落自动回退到普通排版，并记录回退原因。
- `bdt check` 检查字号收缩、文本层兼容字形、溢出和重叠。

## 安装

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 MinerU API token。

```bash
git clone https://github.com/zcyisiee/ieeTranslater.git
cd ieeTranslater
uv sync
export MINERU_API_TOKEN="你的 token"
```

翻译模型 CLI 需要单独安装并登录。使用 `agy` 时确认命令可用：

```bash
agy models
```

## 使用方法

工具入口只有一个：`bdt`（安装后即在 PATH；等价于 `python -m babeldoc_tools`）。
8 个子命令：`parse` / `translate` / `apply` / `build` / `check` / `layout-set` /
`report` / `run`。stdout 恒为单行 JSON，日志走 stderr，退出码 0 = 成功、1 = 失败。

### 单一流程（推荐）

一条命令串起整条链路（parse → translate → apply → build → check → reviewer →
report）。翻译与审查都是"被调命令从 stdin 读提示词、把结果写到 stdout"的子进程：

```bash
uv sync
export MINERU_API_TOKEN=...   # 或用 --mineru-json 回放已缓存布局
uv run bdt run paper.pdf \
  --workdir tmp/paper \
  --translator "scripts/agy-translator.sh" \
  --reviewer "scripts/agy-reviewer.sh" \
  --dual
```

`--translator` / `--reviewer` 缺省时也可用环境变量 `BDT_TRANSLATOR` /（审查必须显式给
`--reviewer`）。没有可调用的模型命令、只想离线走通链路时，用 `--markdown self` 拿
`agent/document.md` 当译文（不调用任何模型）：

```bash
uv run bdt run paper.pdf --workdir tmp/paper --mineru-json "$CACHE" --markdown self --dual
```

### 分阶段版本

每个阶段结果写入 `--workdir`（建议放 `tmp/`），全部幂等可重入：

```bash
WD=tmp/my-paper
PDF="/absolute/path/paper.pdf"

# 1. 解析：PDF → 连续 Markdown（带段落 id 与行内锚点）+ IR 状态
uv run bdt parse "$PDF" --workdir "$WD" --layout mineru
#    离线回放：--mineru-json <缓存 layout.json>  或  --mineru-cache-key <sha256>

# 2. 翻译：被调命令从 stdin 读提示词、把译文写到 stdout（模型/档位由它自己决定）
uv run bdt translate --workdir "$WD" --translator "scripts/agy-translator.sh" --timeout 3600
#    导入已有译文：--markdown <已有译文.md>（不调命令）
#    只取提示词：  --prompt-only（写 agent/prompt.md）

# 3. 写回：校验 id/锚点/占位符/段落完整性后写回 IR
uv run bdt apply --workdir "$WD"

# 4. 重建：应用排版覆盖生成 mono/dual PDF + dump 几何
uv run bdt build --workdir "$WD" --dual

# 5. 三合一质量门禁：结构审查 + 排版 lint + 链接审计
uv run bdt check --workdir "$WD"

# 6. 导出报告
uv run bdt report --workdir "$WD"
```

### Agent 自己翻译

不想让 `bdt` 调外部模型、而是让 Agent 自己产出译文时，用 `--prompt-only` 取出提示词，
再把自己的 Markdown 交给 `--markdown` 导入：

```bash
# 只写提示词到 agent/prompt.md，不调用任何命令
uv run bdt translate --workdir "$WD" --prompt-only
# Agent 按 agent/prompt.md 产出译文（结构/锚点/id 必须原样保留），写入文件后导入
uv run bdt translate --workdir "$WD" --markdown my-translated.md
uv run bdt apply --workdir "$WD"
```

### 迭代闭环

`bdt run` 把每阶段的完成标记与关键输入 sha256 记进 `agent/run_state.json`
（run 私有状态，其他工具不读），并记录 `quality`（check verdict、reviewer 结论、
修复轮计数）。质量门禁：

- `check` 步用 `--strict` 语义：verdict 非 pass（含子项 `not_available`）时仍跑完
  reviewer 与 report，但整体退出码 1。check 的确定性 blocker 优先，reviewer 的
  `pass` 不能覆盖它。
- 没有 `--reviewer` 时以 `waiting_for_reviewer` 结束（exit 1）——没人审查不算成功；
  审查提示词写在 `agent/review_prompt.md`。
- `--reviewer` 返回 `needs_fix` 时，findings（每条带 `id`/`kind`/`evidence`/`action`）
  被映射成 `actions` JSON（`bdt translate --ids` / `bdt layout-set`）；执行后用
  `bdt run --from apply` 续跑。单任务最多 2 个翻译修复轮 + 2 个排版修复轮，超限停在
  `needs_human_review` 并不再调用模型。run 不做自动修复。

因此一轮迭代是：读 `check`/reviewer 结论 → 执行修复 → `--from apply` 续跑。

```bash
# 看结论：verdict + blockers/warnings + reviewer 结论（只读，exit 0）
uv run bdt check --workdir "$WD"
# CI / 门禁用：verdict 非 pass 时 exit 1
uv run bdt check --workdir "$WD" --strict

# reviewer 给 needs_fix 时，按 findings 的 kind 分两路修复：
#  · 翻译缺陷（kind=retranslate）→ 按 id 重译并合并
uv run bdt translate --workdir "$WD" --translator "scripts/agy-translator.sh" \
  --ids P05-002,P05-003 --feedback "补全句末成分，占位符数量保持一致"
#  · 排版缺陷（kind=layout）→ 写段落级覆盖
uv run bdt layout-set --workdir "$WD" \
  --patch '{"paragraphs": {"P05-012": {"scale_cap": 0.9}}}' --reason "重叠 23%"

# 修复后从 apply 起续跑（上游哈希不符会报 stale_upstream 并给出 suggested_from）
uv run bdt run --workdir "$WD" --from apply --dual
```

> 实测：`2512.08296v3.pdf` 自译（`--markdown self`）场景 `check` 可能
> `needs_fix`（P1 排版 / 链接 `wrong_role`），`run` 因此以 exit 1 结束——这是质量
> 门禁的正确行为，不是 bug。此时按上面的闭环修复即可，不要放宽判定口径。

### 常用参数

- `--layout mineru|paddle`：布局后端；`mineru`（默认，云端 API）或 `paddle`
  （本地 PP-DocLayoutV3）。没有 token 时可传 `--mineru-json` / `--mineru-cache-key`
  回放本地布局缓存。
- `--mineru-json <path>` / `--mineru-cache-key <sha256>`：离线回放已缓存的
  MinerU 布局（`~/.cache/babeldoc/mineru-layout.v1/<key>.json`），不联网。
- `--pages`：只处理指定页，如 `1,2,5-7`。
- `--dual`（`build` / `run`）：同时生成 mono 和 dual PDF。
- `--render 1,2`（`build` / `run`）：重建后渲染指定页 PNG（视觉审查）。
- `--strict`（仅 `check`）：verdict 非 pass（含子项 `not_available`）时 exit 1。
- `--ids` / `--feedback`（`translate` / `run`）：按段落 id 重译并合并。
- `--markdown`（`translate` / `run`）：导入已有译文；`run ... --markdown self` 表示用
  `agent/document.md` 自译（不调用模型，适合离线验证链路）。
- `--prompt-only`（`translate` / `run`）：只写出提示词，不调用任何命令。
- `--translator` / `--reviewer`：翻译/审查命令（stdin 读提示词、stdout 出结果）；
  翻译缺省读 `BDT_TRANSLATOR`，审查必须显式给 `--reviewer`。
- `--from {parse,translate,apply,build,check,review,report}`（仅 `bdt run`）：
  从指定阶段续跑。若被跳过阶段的输入哈希与 `agent/run_state.json` 记录不符
  （上游被改动），会以 `stale_upstream` 报错并在 `suggested_from` 里给出该重跑的
  最早阶段，不会静默沿用旧产物。

### 特性开关与默认值

`bdt` 是唯一入口，所有开关都是子命令旗标。三类无开关的默认行为先列清楚：

1. **LaTeX bbox 排版默认开**（`--no-latex-bbox` 关闭）：正文段在 MinerU bbox 内用
   XeLaTeX 两端对齐重排，缺 XeLaTeX/字体时自动回退普通排版并在报告留痕。
2. **批编译与 stamp 缓存恒开**：LaTeX bbox 的批编译与 `<workdir>/<pdf名>/latex_cache/`
   内容寻址缓存没有任何开关（key = 模板版本 + 字体签名 + 请求内容）；二次回放近乎零编译。
3. **行内公式保护恒开**：MinerU 识别出的 `inline_equation` 无条件转成 formula 布局区
   （翻译模型看到 `{vN}` 占位符），渲染侧 LaTeX bbox 融合也自动采用 span 自带公式
   LaTeX，同样无开关：
   - 翻译前：`InlineMathProtector` 在 `bdt parse`（markdown_view）与 `high_level`
     两个入口无条件运行；
   - 渲染时：`mineru` 级公式融合精确盒匹配 + 三道一致性闸门，不过就降级
     simple_math/fragment。

完整特性清单（单列 `bdt` 旗标）：

| 特性 | 旗标 | 默认 | 说明 |
|---|---|---|---|
| 布局后端 | `--layout mineru/paddle` | `mineru` | paddle = 本地 PP-DocLayoutV3/PaddleOCR-VL（MLX/CoreML，全 GPU，无需 token） |
| MinerU OCR 文本回填 | `--mineru-ocr-text` | **关** | 等长 text span 保守字符回填；**会改变模型输入**，实验性 |
| LaTeX bbox 排版 | `--no-latex-bbox` | **开** | 缺 XeLaTeX/字体自动回退旧渲染并在报告 `fallbacks` 留痕；关闭时输出与旧渲染逐字节一致 |
| LaTeX bbox 模式 | `--latex-bbox-mode full/repair` | `full` | `repair` = 复现旧行为（只修溢出，不整段重排） |
| dual 双语 PDF | `--dual` | 关 | 拼宽左原文右译文 |
| 水印 | `--watermark` | 关 | — |
| 布局覆盖率门禁 | `--layout-coverage-threshold` | `0.005` | 原生字符未被任何区域覆盖的占比上限，超限解析失败（防静默漏译） |
| 页码子集 | `--pages 1,2 或 1-3` | 全文 | 解析前裁剪 |
| MinerU 结果回放 | `--mineru-json` / `--mineru-cache-key` | 无 | 离线回放已缓存布局，不联网 |
| 页渲染 | `--render` | 无 | 重建后渲染指定页 PNG（视觉审查） |
| 无 PDF 统计 | `--no-stats` | 附统计 | 关闭 build 返回的页数/目录/链接统计 |

两条容易混淆的边界：OCR 文本回填（opt-in）与公式 LaTeX（默认）是独立通道——前者动
模型输入（改了要重翻译），后者只在渲染侧（升级不需要重翻译）；`latex_bbox` 默认开但
尽力而为，能力探测失败自动回退，不会让任务失败。


## 输出文件

工作目录结构通常如下：

```text
agent/
  document.md          # 带段落 ID 的原文 Markdown
  translated.md        # 模型返回的译文（或 --markdown 导入）
  translated.jsonl     # 写回后的段落
  apply_report.json    # 锚点和 ID 检查结果
  review_verdict.json  # 结构审查结果（check）
  link_audit.json      # 链接逐条审计结果（check）
  layout_lint.json     # 排版检查结果（check）
  layout_geometry.json # 排版几何信息（build）
  agent_review.json    # reviewer 结论（run 的 review 阶段）
  run_state.json       # run 私有阶段状态与输入哈希
FINAL_REPORT.md        # 汇总报告（bdt report；默认落 workdir 根，可用 --output-dir 改）
output/
  *.zh.mono.pdf
  *.zh.dual.pdf
```

## 测试

运行链接相关测试：

```bash
uv run python -m pytest tests/test_link_remap.py tests/test_link_audit.py tests/test_link_correspondence.py -q
```

运行全部测试：

```bash
uv run python -m pytest tests/ -q
```

测试过程中的 PDF、PNG、JSON 和模型日志统一放在 `tmp/`，该目录已加入 `.gitignore`。

## 已知限制

- 外部 URI 只验证注释动作和地址是否保留，不验证网站当前是否可访问。
- `wrong_label` 或 `wrong_role` 表示对应关系需要人工复核；链接数量一致不能证明语义对应正确。
- 译文明显变长时，自动排版可能缩小字号；请查看 `layout_lint.json` 和渲染图片。
- 表格内部文字、图内文字和参考文献条目默认不翻译。
