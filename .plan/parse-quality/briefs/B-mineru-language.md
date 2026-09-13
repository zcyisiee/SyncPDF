# Task: B — MinerU language 参数接入与 A/B 实验

> 本任务分两部分：B-1 参数透传（可委派 subagent）、B-1x A/B 实验执行（主 Agent 自跑，需网络与 API 配额）。

## Objective

MinerU API v4 payload 当前未传 `language`（默认 ch）。英文论文传 `language='en'` 可能改善 OCR/公式边界判定。透传参数 + 参数化缓存 key + A/B 对比数据。

## Context

- 分支 `feature/latex-bbox-layout`。
- 当前调用链：`workflow.py:224` / `markdown_view.py:413` 构造 `MinerUDocLayoutModel(api_token=token)` —— `model_version` 用类默认 `'vlm'`，`language=None`（payload 不带 language 字段）。
- `MinerUDocLayoutModel.__init__` 已有 `language` 参数（`mineru_doclayout.py:30-41`），`_request_upload_urls`（:299-303）已透传 `payload["language"]`。**缺的只是调用点**。
- 缓存：`_layout_cache_path`（:394-401）key = PDF sha256 —— **同 PDF 不同参数会命中旧缓存**，必须先修。
- MinerU API v4 可调参数（官方文档）：`model_version`（pipeline/vlm/MinerU-HTML）、`language`（OCR 语言）、`enable_formula`、`enable_table`、`is_ocr`、`page_ranges`、`no_cache`。服务端 v3.4.4 的 vlm = hybrid 引擎。
- 必读：`babeldoc/docvision/mineru_doclayout.py:28-45, 295-320, 390-410`、`babeldoc/tools/agent/__main__.py`（md-extract 参数定义）、`babeldoc/tools/agent/markdown_view.py:351-360`（extract_markdown 签名）。

## Deliverables

1. `MinerUDocLayoutModel` 缓存 key 参数化（:394-401）：
   - key = `sha256` + `model_version` + `language`（拼进文件名或嵌 JSON 头）；旧缓存文件名格式保持可读（如 `<sha256>.<model>.<lang>.json`）；
   - `--mineru-cache-key` 回放路径不受影响（显式 key 直指文件）。
2. CLI 透传：
   - `__main__.py` md-extract / extract 增加 `--mineru-language` 选项；
   - `markdown_view.extract_markdown` / `workflow.extract` 签名加 `mineru_language=None` 并传给构造；
   - `main.py` 产品 CLI 已有 `--mineru-language`（:76），确认贯通即可。
3. A/B 实验脚本 `experiments/mineru_ab.py`（主 Agent 用）：
   - 对 `测试.pdf` 分别以 language=ch（默认）/ en 调用 MinerU（no_cache），拉取 layout.json 落 tmp；
   - 对比指标：`span_text_samples` mismatch 数、inline_equation span 数、`(e.g.` 类边界案例（content 以 `(` 开头且无 `)`）计数；
   - 输出 JSON 报告落 tmp。

## Constraints

- 默认行为不变（不传 language 时 payload 不带该字段，与现在一致）；
- 不动 `BABELDOC_MINERU_LAYOUT_JSON` 回放协议；
- 缓存目录 `~/.cache/babeldoc/mineru-layout.v1/` 旧文件不迁移（新参数生成新文件名）。

## Validation（subagent 部分）

```bash
python3 -m pytest tests/test_mineru_doclayout_adapter.py -q   # 既有失败不变（环境缺 fixture）
python3 -m pytest tests/ -q
# 回放不破坏（用现有缓存跑一次 md-extract）：
python3 -m babeldoc.tools.agent md-extract "测试.pdf" --workdir tmp/b-verify
# CLI 新选项存在：
python3 -m babeldoc.tools.agent md-extract --help | grep mineru-language
```

## Report back

- 修改/新增文件清单与每处改动目的；
- 验证命令完整输出摘要；
- 发现并修复的缺陷（若有）；
- 任何偏离 brief 的决定及理由。
