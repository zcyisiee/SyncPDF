# 当前架构

本文描述仓库现有实现，核对日期：2026-09-19。在线部署目标和未决定的存储方案见 [在线翻译设计](docs/design/online-translation.md)；运行与验证见 [CLI 指南](docs/guide/cli.md)。

## 1. 目的

把 PDF 解析为带段落身份、样式和公式锚点的可译文本，调用模型翻译，再重建译文 PDF，并允许用户查看进度、修改局部译文和重新编译。输入是 PDF、语言与布局配置、翻译/审查提供方；输出包括单语 PDF、可选双语 PDF、工作目录和质量报告。保真程度由检查与人工复核判断。

当前产品由本地 CLI 和单机 Web 工作台组成。Web 已有上传、实时事件、草稿、段落编译与导出；尚无仓库内的多用户认证、跨机器任务队列或远端对象存储实现。

## 2. 入口

唯一安装命令是 `bdt`，由 [pyproject.toml](pyproject.toml) 注册到 [babeldoc_tools/__main__.py](babeldoc_tools/__main__.py)。

- `bdt run`：完整编排；`parse / translate / apply / build / check / layout-set / report`：分步操作。
- `bdt serve`：启动 FastAPI 服务与可选的 `web/dist` 静态站点；默认 `127.0.0.1`、端口 `0`（绑定后返回真实端口）。HTTP 前缀 `/api/v1`，接口 schema 由 `/openapi.json` 提供。根目录枚举会跳过测试/调试工作目录，并按源 PDF 内容去重、保留最近版本。
- `bdt debug` 与阶段命令的 `--debug`：诊断归档及查看器。
- `bdt harness-call / model-call`：模型适配子命令，读 stdin 提示词、写 stdout 文本；仍属于同一个入口。

普通阶段命令输出 JSON 信封，日志走 stderr；`--help`、模型适配和长驻服务各有自己的输出语义，不能一概按 JSON 解析。

## 3. 代码地图

| 责任 | 从哪里读 |
|---|---|
| 参数、阶段编排、续跑、外部命令协议 | `babeldoc_tools/__main__.py`、`run.py`、`common.py` |
| 阶段门面 | `babeldoc_tools/{parse,translate,layout,review,report}.py` |
| Markdown 与 IR 转换、写回、重建、协议检查 | `babeldoc/tools/agent/{markdown_view,workflow,protocol}.py`；这里是内部库 |
| 原生 PDF 解析、文档中间表示、排版、PDF 生成 | `babeldoc/format/pdf/new_parser/`、`document_il/{frontend,midend,backend}/` |
| 布局提供方与字符对齐 | `babeldoc/docvision/`、`document_il/utils/provider_alignment.py` |
| HTTP 路由、任务、草稿、全量编译与候选 | `babeldoc_tools/serve/{app,runner,jobs,draft,compile,candidates}.py`、`routers/` |
| 元数据、资产、局部/批量编译、迁移与清理 | `babeldoc_tools/serve/{database,asset_store,block_compile,migrate,cleanup}.py` |
| 工作台、API 消费、预览与事件订阅 | `web/src/{screens,components,lib,api}/` |
| 模型调用与提示词 | `babeldoc_tools/harnesses.py`、`serve/models.py`、`skills/document-translate/agents/`、`scripts/` |
| 质量检查与回归证据 | `tests/`、`babeldoc/tools/agent/{quality_checks,layout_geometry,link_audit}.py`、仓库 `tmp/` |

核心引擎负责 PDF/IR，工具层负责工作目录与编排，服务层再包裹工具层及局部编译器；浏览器通过 HTTP 消费它们。现有外部边界是 MinerU 云 API 或本地 Paddle 布局后端、翻译提供方，以及默认启用的 XeLaTeX bbox 渲染。部分字体、模型和缓存还在用户缓存目录，不全在文档目录内。

布局适配器负责统一到未旋转页面坐标；MinerU 同步转换布局框与 provider IR 的 block/line/span，原始缓存不改写。共享布局门禁只统计非空白原生字符，空白数量单独保留供审计。具体坐标与覆盖率口径见 [管线参考](docs/reference/pipeline.md)。

## 4. 三条主要路径

### 完整翻译

```text
bdt run → parse → translate → apply → build → check → review → report
             ↓          ↓         ↓        ↓                    ↓
        agent/解析产物  translated.md  IR  output/*.pdf    FINAL_REPORT.md
```

`parse_document` 经 `markdown_view.extract_markdown` 生成带锚点的 Markdown 和 `state.pkl`。`translate_document` 调用外部命令或导入已有 Markdown；`apply_translation` 将通过协议检查的文本写回 IR；`layout.build_pdf` 经 `workflow.reconstruct` 排版并生成 PDF。`run.py::STAGES` 管理七阶段和输入哈希；AI 审查可显式跳过，本地质量检查仍保留。详细停止语义见 [管线参考](docs/reference/pipeline.md)。

### 上传与实时进度

```text
浏览器 → POST /documents → uploads → workdir/source.pdf + app.db + assets/
       → POST /documents/{did}/jobs → JobRunner → bdt run 子进程
       ← SSE / 任务查询 ← 诊断事件、持久事件与任务状态
```

上传按内容哈希去重，上传本身不自动解析。任务执行阶段会保存可观察状态；翻译流可经 `ServeStreamPreview` 生成预览：完成的翻译块立即提交到一个并行编译池（`--preview-workers`/job 字段 `preview_workers`，1..8，缺省 8），同一页的块路由到同一 worker 串行编译（页 patch 状态不丢），不同页并行。流式路径只写 immutable baseline 上的块 patch 和当前页 asset；页面全部块完成后发布一次页事件，翻译结束再由页 asset 合成完整预览写入 `local_previews`。LaTeX 能力探测与 `state.pkl` 反序列化按进程缓存。持久 SSE 读数据库事件，旧 SSE 读单次 run 的诊断归档，二者游标不同。

### 局部修改与交付

草稿保存带 `base_revision`，写入 SQLite 并保留兼容 JSON。**保存不自动编译**：当前 `CompileService.schedule` 只取消旧计时器。用户可显式编译单段（`BlockCompiler`，更新页面/预览资产），或 shift 多选多段走 `POST /blocks/compile` 批量编译（一个 job；同页串行、跨页并行的线程池，每个受影响页只合成一次，各块草稿覆盖互不影响），再导出当前 revision。局部发布会检查 revision 和任务状态，避免过期结果覆盖新编辑。

贴片渲染的 fit 判定对水平方向使用 2.5pt 容差（垂直 0.5pt）：TeX/PyMuPDF 的宽度口径是 advance 盒，轻微超宽不触发缩字号。贴片仍被缩字或溢出时自动**浮动**：`block_compile._float_if_shrunk` 用 PP-DocLayoutV3 对编译后的译文页重识别版面（`PaddleLayoutRegions`，缺 `BDT_PADDLE_DEVICE` 环境时 auto，CoreML 运行期失败自动降级 CPU），按 同栏下/上扩 → 跨栏横向扩 → 跨页整框迁移 找净空并重渲染；跨页迁移的贴片在 patch 里记 `page` 落点页，`compose_page_asset` 负责擦 home 页脚印、把外来贴片盖到落点页。

`GET /paragraphs` 每段带解析状态派生的 `style` 摘要（字号/衬线/加粗/斜体/字体名）；草稿 `layout` 新增 `bold/italic/serif` 布尔覆盖并在局部编译注入 LaTeX（`font_scale`/`line_skip` 同路径生效）。前端段落面板提供三态下拉，多选时提供批量编译面板。

原文识别叠加层经 `serve/recognition.py` 读取已持久化的 provider IR，展示原始 block/span 框（含行内公式）和全文实际 label 清单；旧 parse 段落快照仍供兼容与段落选择使用。span 是只读预览实体，不参与草稿/编译身份，也不叠加到译文页。接口字段与回退语义见 [HTTP 参考](docs/reference/http-api.md#识别框与-label-筛选)。

旧的 `action=compile` 全量路径仍存在：在隔离目录物化草稿，调用 `bdt run --from apply`，发布 PDF 并归档版本。AI 重译另走候选路径，采用候选才写草稿。全量版本归档与局部导出是两套并存机制，见 [HTTP 参考](docs/reference/http-api.md)。

### 数据落点

| 位置 | 现有职责 |
|---|---|
| `<workdir>/agent/`、`output/` | CLI 的解析状态、文本、检查结果与 PDF |
| `<store_base>/app.db` | SQLite（WAL）：文档、草稿、任务快照/事件、段落、页面、资产引用、导出 |
| `<store_base>/assets/<哈希前缀>/` | 按 SHA-256 寻址的本地文件；数据库不保存 PDF 二进制 |
| `<store_base>/.bdt-serve/`、`<workdir>/.bdt-serve/` | 服务配置、兼容任务/草稿文件、候选、全量编译状态及版本 |
| `<workdir>/debug/runs/` | 按运行归档的诊断事件、快照与产物副本 |

`store_base` 在 root 模式是服务根目录，在 workdir 模式是所选工作目录。SQLite 和文件目前并存；不是已经完成数据库替换，也不能只备份其中一边。

## 5. 不可随意破坏的边界

| 边界 | 代码与检查出口 |
|---|---|
| 新能力仍由 `bdt` 暴露，无第二个 CLI/工具包 | `pyproject.toml`、`tests/test_single_entry.py` |
| 段落身份与样式/公式占位符受协议保护；修复与原文回退必须可观察 | `markdown_view.py`、`protocol.py`、`tests/test_markdown_format.py`、`test_agent_protocol.py` |
| 续跑检查阶段依赖与记录哈希；过期输入不能静默复用 | `run.py::STAGE_INPUTS`、`tests/test_run_pipeline.py` |
| 编译成功、预览可用、质量通过是不同状态；失败不得把旧产物标成当前 revision | `compile.py`、`block_compile.py`、`tests/test_serve_compile.py`、`test_serve_local_export.py` |
| LaTeX 编译后精修框只替换贴片矩形；擦除范围与源行几何量测（含首行 ascent，向上扩因此才有效）仍按原框 | `latex_bbox/overlay.py`（`latex_bbox_box_overrides`）、`layout_refine.py`、`tests/test_latex_bbox.py` |
| HTTP 文件访问经文档范围解析、产物白名单或资产归属校验 | `store.py`、`artifacts.py`、`routers/artifacts.py`、`tests/test_serve_artifacts.py` |
| 测试证据写入仓库 `tmp/`，不得纳入版本控制 | `.gitignore`、`AGENTS.md` |

## 6. 已知缺口

- 双轨存储与两套编译/事件协议仍并存；统一迁移和旧路径退役时间未定。旧注释和部分测试还保留自动防抖编译的预期，判断行为应追到执行函数。
- `bdt serve --cleanup` 仅清理服务根下过期的 `tmp/`、`cache/` 文件；没有对全部 workdir、debug、历史版本和资产的容量预算/自动淘汰闭环。
- 尚未量化真实云服务器的磁盘峰值、并发与恢复目标。数据库和远端资产后端选型保持待定；不得从本机实现推断公网部署已就绪。
