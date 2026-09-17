"""PDF 上传：校验 → 建 did → 流式落盘 ``<root>/<did>/source.pdf``（``api.md`` §3.5，W08）。

上传是 W08 引入的**第一个把客户端字节写进服务根目录**的端点。三条硬约束：

1. **目录名全由服务端生成**：客户端只说"这是一个叫 X 的 PDF"，``did`` 由
   :func:`new_upload_did` 拼成 ``up-<slug>-<yyyymmdd-hhmmss>``（``slug`` 只保留文件名的
   ``[a-z0-9-]``），同名冲突在服务端递增 ``-2``/``-3``。客户端给的字符串**不进路径**。
2. **不建假文档**：``did`` 目录下只写 ``source.pdf``（同目录 tmp 文件 + ``os.replace``
   原子落盘），**不**预建 ``agent/`` 骨架 —— "还没有任何产物"就该显示成空文档（W02 列表
   本来就容忍缺产物）。中途失败把刚建的目录一起收掉，不在根目录留空壳。
3. **大文件不进内存**：按 :data:`CHUNK_BYTES` 分块从上传流写盘，一超上限立刻中止
   （``file_too_large``，413）。浏览器把整个文件传进来是 HTTP 语义，但服务进程不把
   200MB 读进内存。

产物形状是 W03 已冻结的：``source.pdf`` 是 workdir 根级白名单文件（``kind=source``），
所以上传完成后 W05 的"原文"预览、W04 的文件库列表立刻可用（都不需要额外登记）。
"""

from __future__ import annotations

import contextlib
import shutil
from datetime import datetime
from datetime import timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Protocol

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.store import DocumentStore

__all__ = [
    "CHUNK_BYTES",
    "DID_PREFIX",
    "DID_TIME_FORMAT",
    "MAX_DID_ATTEMPTS",
    "MAX_UPLOAD_BYTES",
    "PDF_MAGIC",
    "SLUG_MAX",
    "SOURCE_NAME",
    "TMP_NAME",
    "new_upload_did",
    "save_upload",
    "upload_slug",
]

#: 单个上传的大小上限（200MB）。超出 → 413 ``file_too_large``。
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
#: PDF 魔数（读前 5 字节比对）。
PDF_MAGIC = b"%PDF-"
#: 上传生成的 did 前缀（与手工命名（``ccs3764-dyn``）区分开）。
DID_PREFIX = "up-"
#: slug 最长字符数（截断，避免超长文件名撑爆目录名）。
SLUG_MAX = 40
#: 每次从上传流读的字节数（分块写盘，不整文件读进内存）。
CHUNK_BYTES = 1 << 20
#: did 冲突时递增后缀的最大尝试次数（``-2`` … ``-50``）。
MAX_DID_ATTEMPTS = 50
#: did 时间戳格式（UTC，与全站时间约定一致：目录名只用于区分同名上传）。
DID_TIME_FORMAT = "%Y%m%d-%H%M%S"
#: 源 PDF 在 workdir 根下的固定名字（W03 产物白名单里的 ``kind=source``）。
SOURCE_NAME = "source.pdf"
#: 落盘用的临时名（同目录 replace 才是原子的；失败会被收掉）。
TMP_NAME = ".source.pdf.tmp"


class UploadedStream(Protocol):
    """``save_upload`` 需要的上传对象形状（FastAPI/Starlette ``UploadFile`` 满足它）。

    只用三个成员：``filename``（生成 did 用）、``file``（可读的可寻址二进制流，
    Starlette 已把大文件落到临时文件上）、``size``（可选声明大小，用于提前拒超大）。
    """

    filename: str | None
    size: int | None
    file: object


def upload_slug(filename: str) -> str:
    """PDF 文件名 → did 的 slug：小写、非 ``[a-z0-9-]`` 折叠成 ``-``、截断 40 字符。

    只取文件名的**基名**（浏览器给 "a/b.pdf" 也只当 "b"），目录分隔符、``..``、
    空格、中文之类都不进 did；结果为空时回退 ``pdf``（例如 ``论文.pdf``）。
    """
    base = PurePosixPath(filename.replace("\\", "/")).name
    stem = PurePosixPath(base).stem or base
    chars: list[str] = []
    previous_dash = False
    for char in stem.lower():
        if char.isascii() and (char.isalnum() or char == "-"):
            chars.append(char)
            previous_dash = char == "-"
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    slug = "".join(chars).strip("-")[:SLUG_MAX].strip("-")
    return slug or "pdf"


def new_upload_did(filename: str, now: datetime | None = None) -> str:
    """``up-<slug>-<yyyymmdd-hhmmss>``（时间为 UTC，缺省取当前时刻，可注入便于测试）。"""
    stamp = (now or datetime.now(timezone.utc)).strftime(DID_TIME_FORMAT)
    return f"{DID_PREFIX}{upload_slug(filename)}-{stamp}"


def save_upload(store: DocumentStore, upload: UploadedStream) -> str:
    """校验并落盘一个上传：返回新建的 ``did``（``source.pdf`` 已在盘上）。

    校验顺序（先便宜、先不改盘）：serve 模式（``--workdir`` 拒绝）→ 文件名非空 → 声明大小
    （若上传层给了）→ ``%PDF-`` 魔数 → 建 did 目录 → 分块写盘（写的过程中再按真实字节数
    卡上限）→ 原子 rename。

    失败一律不留痕：刚建的 did 目录与半成品 tmp 都收掉（``ToolError`` 原样抛出，
    由 app 层转成 ``{"error": {code, message, detail}}``）。
    """
    filename = (upload.filename or "").strip()
    if not filename:
        raise ToolError(
            "invalid_pdf",
            "上传缺少文件名：请用 multipart/form-data 的 file 字段带上原始文件名",
            reason="missing_filename",
        )
    if store.mode != "root":
        # ``--workdir`` 模式只公开那一个 workdir（兄弟目录不可见、不可解析）：在它旁边建
        # 的 did 根本进不了可见范围，上传会变成"写了个看不见的目录"。如实拒绝。
        raise ToolError(
            "upload_not_supported",
            "当前 serve 以 --workdir 启动：只公开一个 workdir，上传需要 --root 模式",
            mode=store.mode,
        )
    declared = getattr(upload, "size", None)
    if isinstance(declared, int) and declared > MAX_UPLOAD_BYTES:
        raise _too_large(declared)
    magic = _read_magic(upload)
    if magic != PDF_MAGIC:
        raise ToolError(
            "invalid_pdf",
            f"上传内容不是 PDF：前 {len(PDF_MAGIC)} 字节不是 {PDF_MAGIC.decode('ascii')}",
            reason="not_pdf",
            magic=magic.hex(),
        )
    did, directory = _allocate_directory(store, new_upload_did(filename))
    tmp = directory / TMP_NAME
    target = directory / SOURCE_NAME
    try:
        written = 0
        with tmp.open("wb") as handle:
            while True:
                chunk = upload.file.read(CHUNK_BYTES)  # type: ignore[union-attr]
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise _too_large(written)
                handle.write(chunk)
        tmp.replace(target)
    except BaseException:
        # 半成品与刚建的 did 目录一起收掉：宁可什么都没有，也不留一个空壳"文档"。
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return did


def _too_large(size: int) -> ToolError:
    """超出上限的错误（detail 带上限与实际大小，前端好给"文件过大"提示）。"""
    return ToolError(
        "file_too_large",
        f"上传超过上限 {MAX_UPLOAD_BYTES} 字节（本文件 {size} 字节）",
        limit_bytes=MAX_UPLOAD_BYTES,
        size_bytes=size,
    )


def _read_magic(upload: UploadedStream) -> bytes:
    """读上传流开头 :data:`PDF_MAGIC` 长度的字节，读完把游标复位（后面还要整段写盘）。"""
    stream = upload.file
    try:
        stream.seek(0)  # type: ignore[attr-defined]
        head = stream.read(len(PDF_MAGIC))  # type: ignore[attr-defined]
    except (OSError, ValueError):
        return b""
    finally:
        with contextlib.suppress(OSError, ValueError, AttributeError):
            stream.seek(0)  # type: ignore[attr-defined]
    return head or b""


def _allocate_directory(store: DocumentStore, base: str) -> tuple[str, Path]:
    """在服务根目录下建一个**新**的空 did 目录：``(did, path)``。

    同名冲突（并发上传、上一次留下的目录、同名符号链接）递增 ``-2``/``-3`` 换一个，
    最多 :data:`MAX_DID_ATTEMPTS` 次。``mkdir`` 用 ``exist_ok=False``：竞态下要么我们
    建成功，要么别人建好了（``FileExistsError`` → 换名），**不会**写进别人/别处的目录。
    """
    for index in range(1, MAX_DID_ATTEMPTS + 1):
        did = base if index == 1 else f"{base}-{index}"
        if _is_taken(store, did):
            continue
        directory = store.root / did
        try:
            directory.mkdir()
        except FileExistsError:
            continue  # 竞态：别的上传（或一个同名文件/符号链接）刚占了它
        return did, directory
    raise ToolError(
        "upload_conflict",
        f"无法为 {base} 分配新目录：连续 {MAX_DID_ATTEMPTS} 个名字都已被占用",
        did=base,
        attempts=MAX_DID_ATTEMPTS,
    )


def _is_taken(store: DocumentStore, did: str) -> bool:
    """``did`` 是否已被占用（不能往里写）。

    - 解析成功 = 已有同名目录（不管里面有没有产物）→ 占用；
    - ``path_escape`` = 同名符号链接指向服务根目录外 → 占用（换下一个候选名，
      **不**穿过去写）；
    - ``document_not_found`` = 名字空缺（或同名文件/悬空链接，建目录时会
      ``FileExistsError``）→ 不算占用；
    - 其它（``invalid_document_id`` / ``root_missing``）原样抛出：那是我们自己的生成
      逻辑或根目录出了问题，不能掩盖成"重名"。
    """
    try:
        store.resolve(did)
    except ToolError as exc:
        if exc.code == "document_not_found":
            return False
        if exc.code == "path_escape":
            return True
        raise
    return True
