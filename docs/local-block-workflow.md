# 本地草稿、单块编译和导出

服务仍通过 `bdt serve --root <目录>` 启动。

- 保存译文或拖动 bbox 只修改草稿并增加 revision，不自动启动编译。
- 保存后点击“编译此块”，服务只构造一个 StampRequest；从原始字体大小开始尝试适配，并复用 LaTeX stamp 缓存。其他块不调用渲染器。
- 单块贴片、页面和预览保存为内容寻址资产。只有当前 revision 的未取消任务能够发布；失败保留上次成功页面。
- “导出最新 PDF”比较块的已编译输入和当前草稿，仅补编译变化的块。全部成功后发布新的导出资产。失败不会使旧资产成为当前导出；旧版本通过“下载上次成功版本”明确下载。
- 旋转页面或 bbox 与相邻块相交时，局部任务明确失败并说明需要扩大范围，不暗中回退整篇编译。

## API

- `POST /api/v1/documents/{did}/blocks/{block_id}/compile`，请求 `{"base_revision": N}`。
- `POST /api/v1/documents/{did}/export`，请求 `{"base_revision": N}`。
- 两个写接口返回异步 job，使用现有 `/jobs/{job_id}` 查询或取消。
- `GET /api/v1/documents/{did}/exports/latest` 只返回当前成功导出；`?allow_previous=true` 明确允许上次成功版本。
- `GET /api/v1/documents/{did}/assets/{sha256}` 只允许下载该文档已发布的页面、预览和导出资产。
- `GET /api/v1/documents/{did}/events/stream?persistent=true&after_seq=N` 使用 SQLite 持久化的文档事件游标。自动重连支持数字 `Last-Event-ID`。该游标与旧 debug run 的序号不同，不应混用。

服务启动的翻译进程只在识别完整 block、验证锚点顺序后事务提交译文和进度事件。已保存的人工译文保持优先。页面预览由单独的串行线程队列处理，不阻塞模型 stdout 的持续读取。首次翻译还没有完整译文 PDF 时，可直接以 prepared PDF 为页面基线。

## 迁移和清理

`bdt serve --root <目录> --migrate` 在启动前导入当前状态：源 PDF、当前段落、草稿、可用解析快照、prepared PDF 和可验证的当前 PDF。快照只选择当前必要文件，不迁移 debug 历史。重复内容会报告已有 document_id；缺少源文件、损坏产物及不完整解析会在报告中明确列出。

报告写入 `<目录>/tmp/migration-report.json`。快照中的 PDF 路径改为相对路径；局部渲染可从 SQLite 引用的资产恢复，不依赖原解析文件的绝对位置。原工作目录仍保留，供现有 CLI 和历史记录功能兼容使用。

`bdt serve --root <目录> --cleanup` 清理超过一天的 `tmp/` 和 `cache/` 文件。存在活动任务时拒绝清理；不遍历或删除 `assets/`，也不跟随符号链接。它不是资产垃圾回收命令。

## 兼容范围

旧 `/jobs` 全量编译接口、历史 debug 事件和文件式产物读取仍保留；旧全量编译及候选生成路径仍使用隔离工作目录。本改动替换了服务端单块编译、流式页面预览和最新导出路径，并未删除所有旧流水线与文件式读取代码。

## 验证

自动化测试覆盖单块渲染次数、其他段落保留、失败回滚、revision 冲突、缓存输入复用、dirty 导出、API 资产白名单、持久事件续读、迁移快照恢复及安全清理。

另外在本地真实论文产物上验证了 XeLaTeX 首次渲染、重复命中 stamp 缓存、删除迁移副本中的原解析文件后从资产恢复渲染，以及没有既有译文 PDF 时生成首页预览。测试与手工验证产物均保留在仓库 `tmp/`。
