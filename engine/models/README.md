# 模型文件

不入库。`SYNCPDF_MODELS` 环境变量指向包含以下文件的目录；缺失时相关测试 skip。

| 文件 | 来源 | 用途 |
|---|---|---|
| PP-DocLayoutV2.onnx | PaddleX 官方导出 | 布局分析 |
| manifest.json | 本目录生成 | sha256 清单 |
