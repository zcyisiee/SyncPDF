# Task
W01：bdt serve 最小服务骨架与基础契约

## Objective
在 feat/web-frontend 上实现可运行的 bdt serve；FastAPI health/OpenAPI、安全根目录解析可验证。此任务只做骨架，不实现业务流水线或前端。

## Context
先读 AGENTS.md、.plan/web-frontend/EXECUTION.md、pyproject.toml、babeldoc_tools/__main__.py 及 tests/test_single_entry.py。已有唯一 babeldoc_tools 包是合法工具层，只扩展其子包。当前 .venv 尚未安装 fastapi。docs/frontend/README.md 是历史初稿，与最新 PLAN 有冲突。

## Deliverables
1. pyproject 增加 web extra（FastAPI、uvicorn、后续上传所需 python-multipart）；必要时更新 uv.lock，不改变无关依赖版本。正常 bdt 命令无需 web extra；serve 缺依赖时输出可操作错误。
2. 新建 babeldoc_tools/serve/{__init__,app,cli,store,schemas}.py，适当合并小模块。CLI 选项 --root/--workdir（互斥）、--host 默认127.0.0.1、--port、--open。--workdir 只公开指定文档，不泄露兄弟目录。serve --help 无需 FastAPI 可用。端口0正常，--open 在确认实际监听端口后打开，若复杂可询问主控但勿返回假 URL。
3. app factory 的 GET /api/v1/health 与 /openapi.json。统一错误 envelope {error:{code,message,detail?}}；不允许所有来源 CORS。store 的 document resolver 验证单段 did、目录逃逸、符号链接越界、缺失文档，并为只读旧 workdir 提供入口。不读取或公开系统外部文件。
4. docs/frontend/api.md 冻结公共约定、基础端点与后续端点清单。明确未实现端点；路径按 EXECUTION，无 projects 前缀。确定 target/layout 草稿字段、revision、jobs 动作 run/retranslate/compile/check、质量状态与编译状态分离。此任务不为后续端点写假成功 stub。旧 README 顶部标记历史协议，以 api.md/EXECUTION 为准。
5. tests/test_serve_app.py + tests/test_serve_store.py：health/错误形状、合法及越界/符号链接、workdir限制；更新单入口 help 测试并保留反回归能力。docs README 添加最小运行命令。

## Constraints
只修改上述文件及必要的 CLI wiring、pyproject/uv.lock。禁止修改 translate/run/layout/debug 核心，禁止新增第二个 console entry 或 serve.worker 对外 CLI。禁止做 frontend、上传、jobs、SSE、providers 的具体业务。不要做登录/数据库/云服务。禁止委派、提交或 push；不覆盖主控已有文档更改。只查明确目录，用 rg，所有 shell 查询带有限 timeout；禁止 grep -r 全仓及遍历 .venv/tmp。环境安装失败最多尝试两次，保留错误并报告，勿重建 .venv。

## Validation
可用 uv 在现有 .venv 安装新增 web 依赖。测试依赖如 httpx 缺失可安装，但注明。运行 PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_serve_app.py tests/test_serve_store.py tests/test_single_entry.py --basetemp="tmp/pytest-W01-$(date +%Y%m%d-%H%M%S)"；对改动 .py 跑 .venv/bin/ruff check；git diff --check；bdt serve --help 与启动/health curl 冒烟。依赖或验证遇阻先报告事实，不拓展修改。

## Report back
列出改动、准确验证命令和结果、未实现边界/风险。主控会亲自检查 diff 和跑验证；不得只说完成。用绑定 output 回传报告。最多两轮针对失败修复；无法通过时停止给出最小阻塞原因。
