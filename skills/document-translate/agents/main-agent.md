# main-agent：8 子命令编排器

主 Agent 只负责阶段状态、翻译/审查命令注入和验收决策；解析、写回、门禁、落盘与重建
都由 `bdt` 子命令（`babeldoc_tools.registry.invoke` 统一信封）及其背后的能力层完成。

## 固定阶段

`parse → translate → apply → build → check → review（reviewer）→ report`

无 `--reviewer` 时停在 review 并以 `waiting_for_reviewer` 失败（质量门禁不把"没人审查"
当成功）；reviewer 给 `needs_fix` 时进入修复循环；超过修复轮上限（翻译 2 轮 + 排版 2 轮）
停在 `needs_human_review`。每次调用前读取 `bdt check` 的输出，中断后按产物是否存在续跑
（`bdt parse/translate/apply/build/check/report` 全部幂等可重入），不要从 `state.pkl`
推断唯一状态；用 `bdt run --from <阶段>` 续跑时由 `agent/run_state.json` 的输入哈希判定
是否 stale。

## 编排约束

1. `uv run bdt parse <pdf> --workdir <wd>` 后确认 `anchors.json`、`document.md` 和
   `state.pkl` 已落盘。
2. `uv run bdt translate --workdir <wd> --translator "<cmd>"` 由被调命令注入译文
   （stdin 读提示词、stdout 出译文）；被调命令不得写入 PDF。
3. `uv run bdt apply` 返回非空 violations 时停止重建，只对指定 id 调用
   `uv run bdt translate --ids <id,...> --feedback "..."`，翻译修复最多两轮。
4. reviewer（`reviewer-protocol` / `reviewer-fidelity` / `reviewer-layout`）的每条
   finding 必须含 id、kind、页和证据，输出 `verdict` / `findings` 契约。
5. `uv run bdt layout-set` 后必须 `uv run bdt build` → `uv run bdt check`；排版修复最多两轮。
6. 最终用 `uv run bdt report` 生成 `FINAL_REPORT.md`，将 fallback 和人工豁免写清楚。

翻译命令、审查命令、模型、密钥和外部 CLI 均由调用方配置，skill 不硬编码网关。
