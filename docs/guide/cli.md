# 运行与验证

所有产品能力从 `bdt` 进入；完整参数以 `bdt --help` 和各子命令帮助为准。下列命令在仓库根目录执行。

## 环境准备

项目 Python 版本约束在 `pyproject.toml`（当前 Python 3.12）。本机使用**全局 conda 环境** `bdt`（`~/miniconda3/envs/bdt`，含 web extra 与 `paddlex`），所有 worktree 共用，不再各自维护 `.venv`：

```bash
# 在任一 worktree 根目录运行本地代码（cwd 优先于 editable 安装目标）：
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools --help
# 从任意目录（走 editable 指向的代码树）：
~/miniconda3/envs/bdt/bin/bdt --help
```

切换 editable 指向的代码树：在目标 worktree 里 `~/miniconda3/envs/bdt/bin/pip install -e '.[web]' 'paddlex==3.7.2'`。全新机器/仓库检出仍可用仓库内虚拟环境：`uv sync --extra web` 后 `PATH="$PWD/.venv/bin:$PATH" bdt --help`。

CLI 单独使用可省略 `--extra web`。MinerU 解析需要服务环境中的 `MINERU_API_TOKEN`，或 `--mineru-json / --mineru-cache-key` 回放缓存；本地布局可选 `--layout paddle`，其运行时检查在 `babeldoc/docvision/paddle_runtime.py`，不能把安装基础依赖当成 Paddle 环境已就绪。默认 bbox 排版依赖 XeLaTeX 和字体；`bdt build --no-latex-bbox` 可明确关闭。

翻译命令必须从 stdin 读提示词，stdout 只写译文，失败返回非零。仓库有 `scripts/agy-translator.sh` 等适配脚本，需要对应 CLI 已安装、登录。可用 `--markdown` 导入已有译文，或 `--prompt-only` 只生成提示词。

## 使用路径

```bash
# 分步翻译；把 paper.pdf 替换为实际输入文件
bdt parse paper.pdf --workdir tmp/paper --layout mineru
bdt translate --workdir tmp/paper --translator scripts/agy-translator.sh
bdt apply --workdir tmp/paper
bdt build --workdir tmp/paper --dual --render 1,2
bdt check --workdir tmp/paper --strict
bdt report --workdir tmp/paper
```

普通阶段命令输出单行 JSON，日志到 stderr。退出码为 `0` 成功、`1` 失败、`2` 参数用法错误；`check` 只有加 `--strict` 才因质量不通过返回 `1`。`--help` 输出帮助文本，`harness-call / model-call` 输出模型文本，`serve` 是长驻服务。

`build` 默认启用 LaTeX bbox，并在首遍真的缩了字号时用本地 PP-DocLayoutV3 按译文版面再排一遍（先向下、再向上扩，详见 [管线参考](../reference/pipeline.md)）；`--no-latex-bbox` / `--no-latex-refine` 分别关闭两者，布局模型缺失时自动跳过。

`build` 与 `run` 还接受一对译文侧版面识别开关：`--target-layout` 强制对译文 PDF 跑一次 MinerU 识别（产出前端「译文框」用的 `agent/target/provider/provider_ir.json`），`--no-target-layout` 明确关闭（不发网络请求，前端译文框提示产物缺失）。不传时默认在解析布局后端为 `mineru` 且 `MINERU_API_TOKEN` 可用时执行；识别失败只记清单不影响 build。

`run` 串联七阶段（包含 `review`）。例如显式选择只做本地质量检查：

```bash
bdt run paper.pdf --workdir tmp/paper-run \
  --translator scripts/agy-translator.sh --skip-ai-review --dual
```

需要 AI 审查时改为 `--reviewer '<符合审查协议的命令>'`。既不配置 reviewer 又不显式跳过，会停在 `review` 并报 `waiting_for_reviewer`。这不是成功完成。

局部重译与排版修改：

```bash
# id 必须来自本次解析的 anchors.json；此处是示例
bdt translate --workdir tmp/paper --ids P01-001 --feedback '统一术语' \
  --translator scripts/agy-translator.sh
bdt apply --workdir tmp/paper
bdt layout-set --workdir tmp/paper \
  --patch '{"paragraphs":{"P01-001":{"font_scale":1.05}}}'
bdt build --workdir tmp/paper
bdt check --workdir tmp/paper --strict
```

已有 `run_state.json` 的任务可以用 `bdt run --workdir <目录> --from <阶段>` 续跑。它会核验被跳过阶段的输入哈希；发生失效时按错误提示回到应重跑的阶段。不要假定手改任何文件后都能从 `build` 继续，详见 [恢复语义](../reference/pipeline.md)。

## Rust 后端试用入口

`bdt rust-translate` 调用现有Rust sidecar翻译一份PDF，默认`--translator pi`，也支持`--translator agy --model gemini-3.8-flash-low`。先自行构建引擎，或用`--engine`指向已有可执行文件；隔离worktree验证必须使用各自私有Cargo target，不能误用其它树二进制。本命令不会构建引擎、安装模型或修改提供方配置；对应CLI/模型以及ONNX Runtime/PDFium须预先可用。本轮本机验收环境为`source tmp/paper-iteration/env.sh`（再覆盖为当前树的私有`CARGO_TARGET_DIR`）；历史env含已删除worktree路径，不直接复用。

```bash
# 在本仓库根目录运行；每次使用新的 workdir
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools rust-translate paper.pdf \
  --workdir tmp/rust-paper-001 --pages 1-3 \
  --model deepseek/deepseek-flash --thinking low --layout-device coreml
```

省略 `--pages` 会处理全文；`--source-lang` 默认 `auto`，`--target-lang` 默认 `zh-CN`，`--layout-device` 可选 `auto`、`cpu`、`coreml`。命令将 Rust 事件逐条保存到 `<workdir>/events.jsonl`，引擎日志保存到 `stderr.log`，运行结果保存到 `result.json`；译文可用时还会有 `translated.pdf`。stdout 仍只有一行 JSON，简短阶段和页面进度走 stderr。`result.json` 记录已保存页中的成功块、已排版块、已保存页数、未成功块、送译前冲突块数（`blocked_before_translation`）、覆盖缺口页（`coverage_gap_pages`）、未替换块及产物路径；typeset完成但所在页尚未保存的不计入成功块。`run_finished.ok=false`、引擎非零退出、缺最终事件或缺 PDF 均返回失败，即使已有部分译文 PDF。已有运行日志/产物时拒绝复用目录；请指定新 workdir。输入 PDF 不能是该目录的 `translated.pdf`。

可追加`--glossaries terms.csv`统一术语。CSV必须含`source,target`列，共享loader负责去空白、重复源词后者覆盖及排序；`note`列可空，非空备注目前明确拒绝。词表进入主请求和补救请求，整个规范化词表参与翻译缓存身份；修改词表或从无表改为有表不会误用旧译文。内部JSON sidecar由bdt生成，无需用户维护。提示约束不等于语义已验收，仍需人工核对。

仅调整排版代码后，可用已有真实译文重新编译，无模型请求；未命中或校验失败的块保留原文并列为未完成：

```bash
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools rust-translate paper.pdf \
  --workdir tmp/rust-paper-recompiled --cached-from tmp/rust-paper-001 \
  --layout-device coreml
```

`--cached-from` 只读复制原运行目录的译文数据库到新目录，仍按源文本、语言、协议、学术规则及术语表核对并校验内容；须传入与原运行相同的`--glossaries`（如原运行用了词表），不要改变上述身份后假定旧缓存仍命中。它是缓存重编译入口，尚未提供逐块字体/字号编辑接口。

追加 `--dual` 会同时生成 `translated.pdf` 和 `dual.pdf`。双语 PDF 每页为 **420×297 mm 的 A3 横向**，左侧原文、右侧对应译文；各页按可见裁剪框和旋转方向等比例适配半页并居中，不裁切内容。文字/图形保留为 PDF 矢量内容，可选择文字；链接点击框及本地跳转位置随拼页转换，右侧内部链接仍跳到右侧。书签沿用原文目录。`--pages` 只限定翻译页，双语文件与单语文件一样保留完整页数，未选页右侧仍是原文。

```bash
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools rust-translate paper.pdf \
  --workdir tmp/rust-paper-dual --cached-from tmp/rust-paper-001 \
  --font-scale 1.0 --line-height 1.5 --layout-device coreml --dual
```

双语文件在单语输出校验后由 Rust 生成并自检；请求导出却生成失败或缺文件时不能报告成功。`result.json` 增加 `dual_requested`、`dual_output_exists`；请求双语时 `artifacts` 中包含 `dual.pdf`。部分翻译仍保持原有非零退出状态，双语文件不代表漏译已补齐。不传 `--dual` 时只生成单语文件；已有运行目录不能覆盖，使用新 workdir。

全局调整译文字号与相对行距：追加`--font-scale 0.9 --line-height 1.3`。前者将每个译文样式字号乘0.9，保持标题/正文/小字的相对层级；后者指定**基线间距=缩放后的段落主字号×1.3**，是无量纲倍数，不是pt或额外空隙。源公式保留原尺寸；若其上下标/分式会撞相邻行，仅增加该处必需的行距。默认font-scale=1、line-height不覆盖（沿用源行距/字号比例）；只改字号时行距也按原比例联动。两值须为有限正数，实际设置记录在`result.json`的`typography`中。源IR、译文缓存键和保护原文不改；不会自动缩小字号；显式`--line-height`下若仍有排不下的段，翻译结束后用已得译文整篇降一档行距（每步0.1，下限1.2）重排，回退段减少才采用，事件`line_height_lowered`记录实际行距，全文行距保持一致，不再调用模型。页保存前最多3轮按已接受译文墨迹回收上下净空、利用至右侧实际障碍前的空隙；必要时把同栏连续已译段整栈重排，保持顺序、字号/行距，段间距按原段距加行距放宽量，只在本页正文范围内移动，全栈都通过才移动，并且不能跨越固定内容；相邻段墨迹过近也按此整栈重排。横向受原页文字边界限制；不跨页、不扩大表格单元格，也不再次调用布局模型。选字按区域角色：正文（含Caption/Abstract/List 等一切非标题区域）用思源宋体（Noto Serif CJK SC），文档/段落标题用黑体（Noto Sans CJK SC）；保留粗斜体、颜色和相对字号，代码等等宽 run 用内置 JetBrains Mono（CJK 字形回退黑体）。CCS样本已验证`--font-scale 1.0 --line-height 1.5`全文190块完整写入，见[浮动排版验收](../reports/2026-09-22-rust-electron-rewrite/12-MVP真实翻译验收.md#typography-local-float)。

## Web 工作台

```bash
# 构建前端（需要 Node 与 pnpm）；不构建时 serve 仍提供 API
(cd web && pnpm install && pnpm build)
bdt serve --port 8787
# 显式指定文档根或只公开单个 workdir：
bdt serve --root /path/to/library --port 8787
bdt serve --workdir tmp/paper
```

不传 `--root`/`--workdir` 时使用**共享文档库** `~/.sp`（没有则创建）：上传记录、草稿、任务历史与 `app.db` 都在那里，跨 worktree / 跨仓库检出共用同一份，不再随每个 worktree 的 `tmp/` 各建一套。显式使用端口 8787 才能访问 `http://127.0.0.1:8787`；不传 `--port` 时端口自动分配，以启动 JSON 中的 `url` 为准。`--root` 支持上传，`--workdir` 只暴露一个文档。`--preview-workers N`（1..`min(16, cpu_count-2)`，缺省取上限）设置流式翻译预览的并行编译数（贴片渲染全部并行，同页块只在提交页状态时串行；xelatex 是独立子进程，上限随核数走）；单次任务也可以在提交 job 时用 `preview_workers` 字段覆盖。前端开发运行 `cd web && pnpm dev`，代理端口通过 `BDT_SERVE_PORT` 指定。

serve 的**启动环境会被 job 子进程继承**：MinerU 布局需要 `MINERU_API_TOKEN`（或 `--mineru-token`），如果它只写在 `~/.zshrc` 里，用 `nohup`/systemd 之类的非交互方式启动就会丢掉它——此时 parse 会在 `debug_stage` 之前抛 `mineru_token_missing`，任务 1 秒内失败且 **run 归档里一条事件都没有**（事件流因此显示「这个 run 还没有事件」，那是失败的后果，不是事件流坏了）。要确认服务进程是否真的带上了 token：

```bash
ps eww "$(pgrep -f 'bdt serve')" | tr ' ' '\n' | grep -c MINERU_API_TOKEN   # 0 = 没带上
```

同一症状的另一类根因是**子进程 import 失败**：serve 从源码树启动而包未安装（或 editable 安装失效）时，job 子进程报 `No module named babeldoc_tools` 秒退，`debug/runs` 从未创建，事件流显示「该文档没有 run 归档」。子进程的 `PYTHONPATH` 由 serve 自动注入（指向 serve 正在运行的源码树），不再依赖启动时的 cwd；遇到该报错时先看 job 的 `error_message`（已附 stderr 末行），再确认 serve 用的解释器能 `python -c "import babeldoc_tools"`。

在交互式 shell 里启动（或先 `source ~/.zshrc`）即可继承。用本地已有布局缓存回放（`mineru_json` / `mineru_cache_key`）也能绕开，但只对**内容哈希已缓存**的 PDF 有效。

上传后提交翻译任务；编辑先保存草稿，再显式编译段落或导出。保存草稿当前不自动全量编译。更多语义见 [HTTP 参考](../reference/http-api.md)。服务无内置用户认证，当前定位为本机/受控单机工作台。

`bdt serve --root <目录> --migrate` 会先导入旧 workdir 再启动服务；`--cleanup` 会先清理过期可丢弃文件再启动服务，都不是执行后立即退出的独立管理命令。清理范围见 [管线与数据](../reference/pipeline.md)。

<a id="云端版"></a>

## 云端版

`bdt cloud` 与工作台是两套服务，数据根默认 `~/.bdt-cloud`，接口见 [云端版接口](../reference/cloud-api.md)。

```bash
bdt cloud invite --name 张三 --quota 5          # 打印 {"ok":true,"data":{"code":"YJ-…",…}}
bdt cloud serve --port 8790                      # 只有 /api；引擎默认仓库内 engine/target/release/syncpdf-cli
# 本地开发：前端 dev server 把 /api 代理到 8790
(cd cloud-web && pnpm install && pnpm dev)
```

服务进程的环境会传给引擎：需要引擎运行库路径（`LD_LIBRARY_PATH`、`PDFIUM_DYNAMIC_LIB_PATH` 等，见 `engine/vendor/README.md`；vendor 不入库，按 `engine/vendor/sync.sh` 在目标机器准备 fonts/models/pdfium）和翻译通道的前提。每个可选模型固定走自己的通道（模型表见 [云端版接口](../reference/cloud-api.md#上传缓存与额度)），要开放哪个模型就备齐哪个通道：`agy` 通道（Gemini 3.8 Flash 官方）要求服务用户下已认证的 `agy`（`~/.local/bin/agy`、`~/.gemini`），并且**必须走代理**（`HTTPS_PROXY` 等）；`pi` 通道的 DeepSeek 需要 `DEEPSEEK_API_KEY`，其它 provider 需在 pi 配置里可用。`bdt cloud serve --translator fake:*` 会让所有模型改走同一个通道，只用于测试。

前端验收：`cd cloud-web && pnpm typecheck && pnpm build && pnpm e2e`。e2e 启动真实 `bdt cloud serve` 和假引擎 `e2e/fake-engine.py`，不调用模型。`cloud-web/pnpm-workspace.yaml` 让它独立于仓库其它 pnpm 包安装。

### 部署（zcy：106.55.34.117）

当前线上：`http://106.55.34.117:1515/`。nginx 在 80 上托管着其它站点（`default_server`），云端版只占 1515，不改动已有站点。

| 组成 | 位置 |
|---|---|
| 代码 | `~/bdt-cloud/repo`：`git archive` 导出的受控副本（非 git 检出）；`.venv`、`engine/target`、`engine/vendor` 只在服务器上 |
| 数据 | `~/.bdt-cloud`（`app.db`、sources、translations、preview、dual） |
| 前端 | `cloud-web/dist` → `/var/www/synctranslate` |
| 服务 | `/etc/systemd/system/bdt-cloud.service`，监听 `127.0.0.1:8790` |
| 反代 | nginx 站点 `listen 1515`：`/` 静态文件（`try_files $uri /index.html`，`/assets/` 长缓存）；`/api/` → 8790，`client_max_body_size 55m`、`proxy_request_buffering off`、`proxy_buffering off`（SSE）、`proxy_read_timeout 1h`、`X-Real-IP`（登录限速按它计） |

systemd 单元要点：

```ini
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/bdt-cloud/repo
EnvironmentFile=/home/ubuntu/bdt-cloud/engine.env
EnvironmentFile=/home/ubuntu/bdt-cloud/secrets.systemd.env
EnvironmentFile=/home/ubuntu/bdt-cloud/proxy.systemd.env
ExecStart=/home/ubuntu/bdt-cloud/repo/.venv/bin/python -m babeldoc_tools cloud serve --root /home/ubuntu/.bdt-cloud --host 127.0.0.1 --port 8790
KillMode=mixed
TimeoutStopSec=20
Restart=on-failure
MemoryMax=3G
```

- **`KillMode=mixed` 不能改回 `control-group`**：control-group 在停服时同时给引擎发 SIGTERM，引擎先死，运行中的翻译会被记为失败。mixed 只给主进程发 SIGTERM；服务最多等 SSE 连接 3 秒，再在 lifespan 里终止引擎，把翻译留作 running，重启后排回队首；主进程退出后，剩余进程统一 SIGKILL。回归测试：`tests/cloud/test_jobs.py::test_service_stop_mid_run_requeues_instead_of_failing`。
- `EnvironmentFile` 每行只接受一个 `KEY=value`，不展开 `$VAR`，也不认 `export `；`export a=1 b=2` 这种一行多赋值会被读成一个值。从 shell 脚本生成时不要用文本替换，而是让 shell 求值后导出：`(umask 077; env -i bash -c 'set -a; . ./proxy.env; env' | grep -iE '^(http|https|all|no)_proxy=' > proxy.systemd.env)`。含密钥的文件权限 600，检查时只打印键名：`sed -E 's/=.*/=…/' <file>`，并确认值里没有空格。agy 缺 `https_proxy` 时会直连 Google 并卡到超时（agy 日志 `~/.gemini/antigravity-cli/log/cli-*.log` 里是 `dial tcp …:443: i/o timeout`）。

更新代码（在本机仓库根目录执行）。`--delete` 会删掉服务器上不在副本里的文件，所以必须先检查副本非空，并排除只在服务器上的目录：

```bash
set -eo pipefail
ST=tmp/deploy-stage/repo; rm -rf tmp/deploy-stage && mkdir -p $ST
git archive HEAD babeldoc babeldoc_tools engine scripts skills LICENSE pyproject.toml README.md TRANSLATE.md | tar -x -C $ST
git describe --tags --always > $ST/REVISION
test "$(find $ST -type f | wc -l)" -gt 500 && test -f $ST/babeldoc_tools/cloud/app.py
rsync -a --delete --exclude /.venv --exclude /engine/target --exclude /engine/vendor --exclude /uv.lock $ST/ zcy:bdt-cloud/repo/
ssh zcy 'sudo systemctl restart bdt-cloud'   # 引擎源码有改动时先在服务器 cargo build --release
(cd cloud-web && pnpm build) && rsync -a --delete cloud-web/dist/ zcy:/var/www/synctranslate/
```

引擎二进制的 sha256 是缓存键的一部分：重新构建引擎后，已有译文不会再被命中。

## 验证与交付

不要把下面的本地 pytest 与真实模型翻译验收混为一谈。功能改动运行相关测试；完整回归必须让测试找到可用的 `bdt`（本机是 conda env `bdt`，检出内是 `.venv`；测试通过 `python -m babeldoc_tools` 调用，不依赖具体路径）：

```bash
PY=~/miniconda3/envs/bdt/bin/python   # 或 .venv/bin/python
$PY -m pytest --basetemp="tmp/pytest-$(date +%Y%m%d-%H%M%S)"
# 仅入口守卫：
$PY -m pytest tests/test_single_entry.py \
  --basetemp="tmp/pytest-entry-$(date +%Y%m%d-%H%M%S)"
# 对改动的 Python 文件运行；不要向 Ruff 传 Markdown 文件
~/miniconda3/envs/bdt/bin/ruff check <files>
$PY -m mkdocs build --strict --site-dir "tmp/docs-$(date +%Y%m%d-%H%M%S)"
git diff --check
```

每次使用新目录，保留上次证据；真实 PDF 翻译、渲染图、检查报告和日志也放在仓库 `tmp/`。前端行为变化时，按 `web/package.json` 运行 `pnpm typecheck`、`pnpm test` 和适用的浏览器验收。文档改动不需要为段落文字新增镜像测试。

交付说明写清改动、验证命令、通过/失败/跳过及证据位置。已有失败应与改动前版本对照，不通过删除测试或放宽门禁掩盖。工程验收通过上述本地命令完成；涉及子代理时还须遵循 [委派与主控验收规范](delegation.md)。

## 排查入口

| 症状 | 先查 |
|---|---|
| 解析失败或漏段 | JSON 错误、布局后端配置、`agent/anchors.json`、覆盖率报告；`parse.py` 与 `markdown_view.py` |
| 译文锚点/公式异常 | `agent/translated.md`、`apply_report.json` 的修复/回退记录；`markdown_view.py` 与 `protocol.py` |
| 排版或链接问题 | `layout_geometry.json`、`latex_bbox_report.json`、`layout_lint.json`、`link_audit.json`；重新渲染相关页复核 |
| Web 显示旧 PDF | 草稿 revision、编译/预览/导出 revision 与 job 结果；不要只看文件存在 |
| 任务失败或停在审查 | `agent/run_state.json`、`agent/agent_review.json`、job 错误及 `debug/runs/` |
| 事件流显示「没有 run 归档」 | job 的 `error_message`：子进程秒退（import 失败 / token 缺失）时归档从未创建，是失败后果不是事件流坏了 |

`bdt debug --workdir <目录>` 用于查看诊断；阶段运行可加 `--debug --debug-no-open` 保存证据。诊断归档会占额外磁盘，当前清理命令不会遍历所有归档。
