# PP-DocLayout-V3 模型文件

模型不入Git。`SYNCPDF_MODELS` 可指定模型目录；生产翻译校验模型SHA-256，缺失或未知权重明确报错。

| 文件 | SHA-256 | 用途 |
|---|---|---|
| `inference_bbox.onnx` | `fe3bc78476c982401caf389a8e8e928cb94cc0dbb89be73a363838f19fcaf271` | 优先；PP-DocLayout-V3 bbox导出，适合原生矩形页面与CoreML |
| `pp_doc_layoutv3.onnx` | 源码`stages/layout_model.rs`所列V3白名单 | 兼容已有V3完整图；部分导出图不支持CoreML，Auto会显式退回CPU |

bbox权重来源：PaddlePaddle/PP-DocLayoutV3_onnx，revision `46bbdf188bb0a772c08aed74882ce7e51a8f1ea6`，与旧后端`paddle_model_lock.py`共用锁定值。输入BGR/800×800、`im_shape`/`scale_factor`约定与完整图相同，仅省去未使用的mask输出。

默认bundle目录缺bbox时，可只读复用本机`~/.cache/babeldoc/paddle-models/inference_bbox.onnx`，仍须通过同一hash校验。显式模型目录不会偷偷改用其它目录。当前不会自动下载、转换或覆盖模型。

`SYNCPDF_LAYOUT_DEVICE=auto|cpu|coreml`：Auto在macOS优先CoreML（CPUAndGPU），不可用时记录回退；显式coreml失败即报错。CoreML含CPU分区，配置名称不等于所有算子跑GPU。编译缓存位于请求cache目录下`layout-coreml/<model_sha>`；`SYNCPDF_LAYOUT_PROFILE`可指定开发验收profile路径。
