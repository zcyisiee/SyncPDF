# main-agent：可恢复 DocumentJob 编排器

主 Agent 只负责阶段状态、Provider 注入和验收决策；解析、协议 gate、落盘与重建由
`bdt` 子命令（`babeldoc_tools.registry.invoke` 统一信封）及其背后的能力层完成。

## 固定阶段

`created → parsed → translated → protocol_validated → reviewed → reconstructed → rendered → accepted`

遇到确定性协议错误进入 `blocked_protocol`，翻译服务失败进入
`blocked_translation`，排版无法安全修复进入 `blocked_layout`，需要人审进入
`needs_human_review`。每次调用前读取 `bdt check` 的输出，中断后按产物是否存在续跑
（`bdt parse/translate/apply/build/check/report` 全部幂等可重入），不要从
`state.pkl` 推断唯一状态。

## 编排约束

1. `bdt parse <pdf> --workdir <wd>` 后确认 `anchors.json`、`document.md` 和
   `state.pkl` 已落盘。
2. `bdt translate --workdir <wd>` 通过 provider 注入译文；Provider 不得写入 PDF。
3. `bdt apply` 返回非空 violations 时停止重建，只对指定 id 调用
   `bdt translate --ids <id,...> --feedback "..."`，翻译修复最多两轮。
4. `review_protocol`、`review_fidelity` 和 `review_layout` 的 finding 必须含 id、页和证据。
5. `bdt layout-set` 后必须 `bdt build → bdt check`；排版修复最多两轮。
6. 最终用 `bdt report` 生成 `FINAL_REPORT.md`，将 fallback 和人工豁免写清楚。

Provider、模型、密钥和外部 CLI 均由调用方配置，skill 不硬编码网关。
