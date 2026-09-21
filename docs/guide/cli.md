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
