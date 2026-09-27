# 经验教训

存放任务完成后，经用户或主 Agent 核实、提炼的可复用经验。这里不是任务日志归档，也不替代架构、参考文档或 task state。

## 升格规则

- 主控在任务收尾时总结；按主题合并已有条目，确需独立篇目时使用简洁主题名。
- 一篇只写：**结论、适用/不适用边界、根因与证据、以后如何做/避免什么**，链接源任务及回归测试。
- 区分已验证结论、失败尝试与仍待验证的假设；未实现方案不得写成已完成能力。运行日志和截图仍留 `tmp/`，不复制成知识库流水。
- 若结论改变长期约束或当前实现说明，同时更新 `AGENTS.md`、`ARCHITECTURE.md` 或对应参考文档；此处只保留理由与证据链接。
- task state 保留完成摘要和升格链接，不将整个任务 state 原样搬入此目录。

- [可编辑数据必须与校验处于同一表示空间](edit-roundtrip-space.md)：事件译文 HTML 与手改校验空间不一致导致含原子段手改必回退。
- [停服信号要先到服务，再由服务收子进程](service-stop-and-child-processes.md)：`KillMode=control-group` 让引擎先死、SSE 长连接挡住优雅关闭，运行中的翻译在重启时被记成失败。
- [PDF 绑定与渲染必须分别举证](pdf-binding-and-render-evidence.md)：已完成的绑定审查与真实画面反例，涵盖不可变输入、过期绑定、CID/GID和主树复验。
- [两端对齐：断行评分与绘制必须用同一组 glue](justify-glue-single-model.md)：评分与绘制两套分配规则加上过宽伸缩量，让 DP 接受 11pt 空格和 0.84em 字距。

当前 Rust 后端整体修复尚未完成，阶段完成不等于全篇质量通过；过程状态见 [task-state.md](../reports/2026-09-22-rust-electron-rewrite/task-state.md)。
