# 停服信号要先到服务，再由服务收子进程

**结论**：服务托管长时间运行的子进程（这里是 Rust 引擎）时，停服信号要**先只给服务主进程**，由服务先记下"这是停服，不是失败"，再亲手终止子进程。另外，服务必须保证优雅关闭会在有限时间内走到 lifespan：SSE 这类长连接永远不会自己结束。

**适用边界**：适用于 systemd 托管、进程内有 runner 线程拉起子进程的服务（`bdt cloud serve`）。只跑一次、没有停服语义的 CLI 进程不适用。

**根因与证据**（2026-09-25，zcy 实测 2604 运行中 `systemctl restart`，翻译被记成失败）：

1. `KillMode=control-group` 会同时给 cgroup 里所有进程发 SIGTERM。引擎先退出，runner 看到的是非零退出码和不完整的产物，按"翻译没有完成"记为 failed；`Runner.stop` 设置的停服标记来不及生效。
2. 改成只给主进程发 SIGTERM 之后，uvicorn 又会无限等待打开的 SSE 连接（日志停在 "Waiting for connections to close"），lifespan 里的 `runner.stop` 始终不执行，最后只能靠 `TimeoutStopSec` 强杀。

修复：uvicorn 设 `timeout_graceful_shutdown=3`（`babeldoc_tools/cloud/cli.py`）；unit 设 `KillMode=mixed`，主进程退出后剩余进程统一 SIGKILL。启动恢复把 `running` 的翻译排回队首。回归：`tests/cloud/test_jobs.py::test_service_stop_mid_run_requeues_instead_of_failing`。这个测试起真实子进程服务，保持一条 SSE 连接，运行中发 SIGTERM，断言服务按时退出、引擎被收掉、重启后任务跑完，且全程没有 failed 事件；旧代码上它会超时失败。

**以后如何做**：
- 停服/重启语义用**真实进程 + 真实信号**测，不要只在进程内调 `stop()`；进程内测试绕过了信号的投递顺序和服务器的优雅关闭。
- 任何长连接（SSE、WebSocket、流式下载）都要配优雅关闭上限，否则 shutdown hook 可能永远不执行。
- systemd `KillMode` 按"谁负责收子进程"来选：服务自己收，用 `mixed`；`control-group` 只适合子进程死活与业务状态无关的情况。
