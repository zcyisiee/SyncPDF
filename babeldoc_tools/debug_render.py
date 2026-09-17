"""PDF 页渲染 worker：独立子进程，避免跨线程共享 PyMuPDF 文档。

协议（stdin/stdout，父进程 ``debug_server._RenderWorker`` 专用）：

- 请求：一行 JSON ``{"pdf": "<绝对路径>", "page": <1-based>, "dpi": <50-200>}``；
- 响应：一行 JSON 头 ``{"ok": true, "n": <bytes>}`` 或
  ``{"ok": false, "error": "<msg>"}``；``ok`` 为真时紧随其后是 ``n`` 字节的
  PNG 原始数据（不换行）。

worker 内部按路径缓存打开的 ``pymupdf.Document``（同进程内复用是安全的——
worker 单线程逐条处理请求）。任何渲染异常都以 ``ok:false`` 应答，进程不退出。
"""

from __future__ import annotations

import json
import sys


def _render(docs: dict, pdf: str, page: int, dpi: int) -> bytes:
    import pymupdf

    doc = docs.get(pdf)
    if doc is None:
        doc = pymupdf.open(pdf)
        docs[pdf] = doc
    index = int(page) - 1
    if index < 0 or index >= len(doc):
        raise ValueError(f"页码越界: {page}（共 {len(doc)} 页）")
    matrix = pymupdf.Matrix(int(dpi) / 72.0, int(dpi) / 72.0)
    return doc[index].get_pixmap(matrix=matrix, alpha=False).tobytes("png")


def render_worker() -> int:
    """阻塞式请求循环；EOF（父进程退出）后正常结束。"""
    docs: dict = {}
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        line = stdin.readline()
        if not line:
            return 0
        try:
            request = json.loads(line.decode("utf-8"))
            png = _render(
                docs,
                str(request["pdf"]),
                int(request["page"]),
                int(request["dpi"]),
            )
        except Exception as exc:  # noqa: BLE001 - 渲染失败只影响该请求
            stdout.write(
                json.dumps({"ok": False, "error": str(exc)[:300]}).encode("utf-8")
                + b"\n"
            )
            stdout.flush()
            continue
        stdout.write(
            json.dumps({"ok": True, "n": len(png)}).encode("utf-8") + b"\n"
        )
        stdout.write(png)
        stdout.flush()


if __name__ == "__main__":  # pragma: no cover - 由 --debug-render-internal 调用
    sys.exit(render_worker())
