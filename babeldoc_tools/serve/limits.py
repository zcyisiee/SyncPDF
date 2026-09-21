"""服务端并发上限常量。

单独成模块是为了打破 import 环：``schemas`` 要在**类定义期**拿到上限写进
pydantic ``Field(le=...)``，而 ``stream_preview`` 经 ``block_compile`` →
``views`` → ``compile`` → ``versions`` 又回头 import ``schemas``。这里只放
纯常量、不 import 任何本包模块，两边都能安全依赖。
"""

from __future__ import annotations

import os

#: 并行预览编译 worker 数上限。xelatex 是 CPU 密集的**独立子进程**（不受 GIL
#: 约束），所以上限该跟着机器核数走，而不是写死 8——在 18 核机器上写死 8 会白扔
#: 一半以上的算力。留两核给合成/IO 与主线程，并夹到 [1, 16] 防止在大机器上把
#: xelatex 进程铺得过密（每个进程有独立的内存与文件 IO 开销）。
MAX_PREVIEW_WORKERS = max(1, min(16, (os.cpu_count() or 8) - 2))
