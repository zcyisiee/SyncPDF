# 项目文档

本仓库把 PDF 翻译、版面重建、进度查看和局部修订放在同一条工作流中。先读根目录 `ARCHITECTURE.md` 了解当前系统，再按问题进入下面的专题文档。Agent 从根目录 `AGENTS.md` 开始。

| 文档 | 内容边界 | 什么时候读 |
|---|---|---|
| [运行与验证](guide/cli.md) | 安装、命令、排查、检查出口 | 第一次运行或交付改动 |
| [子代理委派规范](guide/delegation.md) | 完整委派约束、harness 用法、brief 与验收 | 主控委派前及叶子开始任务前 |
| [管线与数据](reference/pipeline.md) | 当前阶段、协议、恢复与存储责任 | 修改解析、翻译、重建或持久化 |
| [HTTP 与工作台](reference/http-api.md) | 当前接口、进度游标、草稿/编译/导出语义 | 修改服务或前端 |
| [在线部署设计](design/online-translation.md) | 目标、约束、候选与未决事项 | 讨论下一步演进；不能据此假定已有功能 |

## 维护规则

- 当前事实只在架构地图与参考文档维护；目标和未落实决策只在设计文档维护。代码变化时，同一个改动中更新对应说明。
- 文档记录责任边界、关键路径和失败语义。参数全集看 `bdt <子命令> --help`，HTTP 字段看 `/openapi.json` 与路由/模型，避免手抄大份 schema。
- 保持目录精简。先合并到现有主题，确有独立读者和长期维护责任再讨论拆篇；不为每次任务新增设计稿或验收长文。
- 实验数据、截图、测试日志存本仓库 `tmp/`；旧 docs 已移除，历史依据从 Git 历史查看，不维护第二套归档知识库。
- 验证命令见 [检查出口](guide/cli.md)。文档构建只能检查结构/链接；架构结论仍要追到代码与行为测试。

这套结构采用 [Harness engineering](https://openai.com/index/harness-engineering/) 的短入口、仓库知识与反馈检查思路；全局地图参考 [ARCHITECTURE.md 建议](https://matklad.github.io/2021/02/06/ARCHITECTURE.md.html)。本项目按实际规模保留少量文件。
