# Task: C-1 — PaddleOCR-VL 本地环境与解析质量 spike（不改产品代码）

## Objective

本地部署 PaddleOCR-VL-1.6（CPU 推理，Apple Silicon），对 `测试.pdf` 跑 doc_parser，拿到完整 JSON 结构与质量数据，回答三个问题：
1. `(e.g.` 类正文/公式边界问题在 PaddleOCR-VL 下是否存在？
2. 公式 block 的 bbox 精度与 MinerU 相比如何？
3. CPU 推理速度是否可接受（每页耗时）？

## Context

- 分支 `feature/latex-bbox-layout`。**不改任何产品代码**，产物全部落 `tmp/`。
- 安装（Apple Silicon CPU 路径，官方文档验证）：
  ```bash
  python3 -m pip install paddlepaddle==3.2.1 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
  python3 -m pip install -U "paddleocr[doc-parser]"
  ```
  建议装在独立 venv（如 `tmp/paddle-venv`），避免污染主环境（paddlepaddle 依赖可能与仓库冲突，先 `pip check`）。
- Python API：
  ```python
  from paddlex import create_pipeline
  pipeline = create_pipeline(pipeline="PaddleOCR-VL-1.6", device="cpu")
  output = pipeline.predict(input="测试.pdf")
  pages = list(output)
  for res in pages:
      res.save_to_json(save_path="tmp/paddle-spike")
  ```
- 输出结构（官方文档）：`parsing_res_list[] = {block_bbox, block_label, block_content, block_id, block_order}`（block 级，阅读顺序）；`layout_det_res.boxes[] = {cls_id, label, score, coordinate}`；`block_content` 中公式为 `$...$` LaTeX。
- 对照基线：`tmp/md-test/agent/source/mineru/provider_ir.json`（MinerU 的 block/line/span 树）、`alignment.json`（14 处 span mismatch、`(e.g.` 案例）。
- 若 CPU 太慢（>30s/页）：备选 mlx-vlm-server 加速（`pip install "mlx-vlm>=0.3.11"` + `mlx_vlm.server --port 8111` + `vl_rec_backend="mlx-vlm-server"`）。

## Deliverables

1. `tmp/paddle-spike/` 下：每页 JSON + Markdown 输出。
2. spike 报告 `tmp/paddle-spike/REPORT.md`（主 Agent 起草或 subagent 起草后主 Agent 复核）：
   - `(e.g.` 案例的 PaddleOCR-VL 表现（正文 or 公式 block？括号归属？）——附 JSON 原文摘录；
   - 公式区域：layout_det_res 中 label=formula 的 box 与 MinerU 对应 inline_equation span bbox 的 IoU 对照（≥5 个样本）；
   - 文本 block_content 与 MinerU span 文本的抽样对照（有无 `S i n g l e` 类问题——理论上不应有，PaddleOCR-VL 走图像识别不走原生字符层）；
   - 每页耗时、峰值内存；
   - 结论：C-2 适配层的关键难点（block→span 降维的具体策略建议）。

## Constraints

- 零产品代码改动（`babeldoc/`、`experiments/` 均不动）；
- venv 装在 tmp 下，不污染仓库根；
- 若 paddleocr 安装失败（依赖冲突），如实报告失败原因与替代路径（Docker / mlx-vlm-server），不硬绕。

## Validation

```bash
# 安装验证
tmp/paddle-venv/bin/python -c "import paddle; print(paddle.__version__)"
tmp/paddle-venv/bin/python -c "from paddlex import create_pipeline; print('ok')"
# 跑通测试.pdf（1 页）
tmp/paddle-venv/bin/python -c "
from paddlex import create_pipeline
pipeline = create_pipeline(pipeline='PaddleOCR-VL-1.6', device='cpu')
for res in pipeline.predict(input='测试.pdf'):
    res.save_to_json(save_path='tmp/paddle-spike')
    print('page done')
"
# 可选：2512 p7 对照
```

## Report back

- 安装过程问题与解决；
- REPORT.md 内容（上述四问的答案）；
- 对 C-2 适配层设计的具体建议；
- 任何偏离 brief 的决定及理由。
