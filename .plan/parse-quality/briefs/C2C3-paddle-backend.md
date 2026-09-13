# Task: C-2/C-3 — PaddleOCR-VL 布局后端适配层与 CLI 接入

> 前置：C-1 spike 报告确认可行（`(e.g.` 类边界显著优于 MinerU 或速度可接受）。

## Objective

`--layout paddle` 作为可选第二布局后端：`PaddleDocLayoutModel(DocLayoutModel)` 产出与 MinerU 同构的 YoloResult + ProviderDocument，md-extract/extract 全链路可用。

## Context

- 分支 `feature/latex-bbox-layout`。
- **接口契约**（对齐 `MinerUDocLayoutModel`）：
  - `stride` 属性（MinerU 为 32，返回布局网格步长——PaddleOCR-VL 无此概念，返回兼容值）；
  - `handle_document(pages, mupdf_doc, translate_config, save_debug_image)`：yield `(page, YoloResult)`；
  - `provider_document: ProviderDocument | None` 属性（解析后设置）；
  - 布局标签映射：PaddleOCR 的 label（doc_title/paragraph_title/text/image/table/formula/footnote/...）→ BabelDOC layout_label（参考 `mineru_doclayout.py:96 _map_mineru_block_to_layout_label` 的映射表）。
- ProviderDocument 降维策略（C-1 报告会细化）：
  - text block → 单 line + 单 text span（bbox = block_bbox，content = block_content）；
  - formula block → 单 formula span（bbox = block_bbox，content = block_content 中的 LaTeX）；
  - 阅读顺序用 `block_order`；
  - 坐标系：PaddleOCR 输出像素坐标（图像左上原点）→ ProviderDocument 的 IL 坐标（PDF 点，左下原点）需要换算（页高 - y 与 dpi 缩放，参考 `provider_alignment.py` 的 page_height 处理）。
- 必读：`babeldoc/docvision/mineru_doclayout.py`（全文件，接口契约与 provider IR 落盘）、`babeldoc/docvision/provider_ir.py`（ProviderDocument/ProviderPage/ProviderLine/ProviderSpan）、`babeldoc/docvision/doc_layout.py`（DocLayoutModel 基类）、`babeldoc/tools/agent/markdown_view.py:391-410`（layout 分支）。
- 缓存策略：同 MinerU（PaddleOCR 本地跑，缓存 JSON 结果按 sha256+version keyed，`~/.cache/babeldoc/paddle-layout.v1/`）。

## Deliverables

1. `babeldoc/docvision/paddle_doclayout.py`：
   - `PaddleDocLayoutModel(DocLayoutModel)`：构造参数（pipeline_name 默认 `PaddleOCR-VL-1.6`、device 默认 cpu、model 缓存目录）；
   - `handle_document`：按 translate_config.input_file 跑 paddle pipeline（首次真实推理，之后命中 JSON 缓存）→ 转 YoloResult + ProviderDocument；
   - 标签映射表（PaddleOCR label → BabelDOC layout_label）；
   - 坐标换算（像素 → PDF 点）。
2. `markdown_view.py` / `workflow.py` / `__main__.py`：`--layout` 选项扩展为 `mineru|paddle`（默认 mineru）；分支构造对应模型。
3. 单测 `tests/test_paddle_doclayout.py`：JSON fixture（C-1 的真实输出截取）→ ProviderDocument 转换断言（bbox 换算、标签映射、formula span 生成）。
4. `InlineMathProtector` 兼容性确认：paddle 的 ProviderDocument 走同一 `inline_equation_regions` 路径 → formula span bbox 保护自动生效（A3 修剪护栏同样适用）。

## Constraints

- **默认行为零变化**：不传 `--layout paddle` 时全链路与现在一致；
- MinerU 路径代码不动（只加分支）；
- PaddleOCR 依赖**不进主依赖清单**（pyproject 不加，运行时按需 import + 友好报错"请安装 paddleocr[doc-parser]"）；
- 中文注释风格、ruff 通过。

## Validation

```bash
python3 -m pytest tests/test_paddle_doclayout.py -q
python3 -m pytest tests/ -q
# 全链路（本地推理，MinerU 缓存对照）：
python3 -m babeldoc.tools.agent md-extract "测试.pdf" --workdir tmp/c2-verify --layout paddle
# document.md 与 MinerU 版对照：段落数、标签分布、公式锚点数
python3 -m babeldoc.tools.agent md-extract "测试.pdf" --workdir tmp/c2-verify-mineru
diff <(grep -c 'id=' tmp/c2-verify/agent/document.md) <(grep -c 'id=' tmp/c2-verify-mineru/agent/document.md)
# 翻译全链路 + 渲染：
python3 experiments/markdown_translate.py tmp/c2-verify --output-dir tmp/c2-verify/output
```

## Report back

- 修改/新增文件清单与每处改动目的；
- 验证命令完整输出摘要（双后端 document.md 对照数据）；
- 发现并修复的缺陷（若有）；
- 任何偏离 brief 的决定及理由。
