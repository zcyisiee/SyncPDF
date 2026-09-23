# PP-DocLayout-V3 Apple GPU 工程验收

2026-09-23，主控已review session候选并合入`8a843766`，亲自完成主树接线、严格CoreML运行及内容保护核对。**工程门禁通过，真实LLM翻译质量尚未验收。**

## 实际路径

- 使用本地锁定V3 bbox ONNX，SHA-256 `fe3bc78476c982401caf389a8e8e928cb94cc0dbb89be73a363838f19fcaf271`；未修改本地conda或权重。Python探针与旧V3图在p1/p3 CPU框逐值相同。
- Rust实际动态链接ONNX Runtime 1.23.2，通过CoreML MLProgram、CPUAndGPU运行。这里使用CoreML，不是PyTorch MPS。主控计算计划记录1029条Apple M5 Pro GPU assignment；白页profile16个CoreML/121个CPU执行事件。
- 主树默认macOS Auto优先CoreML；严格coreml失败直接报告，Auto失败有CPU回退事件。旧vendor图CoreML不兼容，不能只注册EP便宣称加速。模型与区域缓存包含权重hash；显式模型目录不静默换成本机其它模型。

## 主控全23页结果

证据：`tmp/backend-repair/codex-r1-integration/` 与 `tmp/backend-repair/layout-final-review/`。使用相同fake译文，仅比较工程行为与性能。

| 检查 | 结果 |
|---|---|
| CPU基线 | `r3-retention-fixed-all` 34.91秒wall |
| 严格CoreML初次 | `v3-coreml-all` 28.80秒wall；模型推理中位98.819ms/页；首推理361.588ms |
| 复用CoreML编译目录 | `v3-coreml-warm-all` 27.49秒wall；仍有明显初始化耗时，未宣称暖启动已优化 |
| GPU执行 | 全23次推理profile368 CoreML/2783 CPU节点事件；无CPU回退 |
| CPU/GPU输出一致性 | 全页字符序列相同，最大原点差0.00039673pt |
| 最终源内容保护 | 83910保留字符：位置、字号、颜色0变化；qpdf exit0 |
| 流式快照 | 23份快照，未ready页CJK污染0 |
| 翻译工程状态 | 50成功译块、49overflow、63原子回退、40保护重叠、1旋转块；请求1/补救0/缓存0；RunFinished false、CLI1正确表示部分结果 |

主控workspace648通过/0失败/7 ignored；q/Q后另作305项pdf+pipeline、8项真实部分删除与strict Clippy/release，通过。详见10报告。

用户要求优先快速MVP；编译启动性能继续优化暂缓，下一步使用真实模型交付可查看译文，保留固定字号、原文保护与未成功块提示。
