"""编译后的**译文侧**版面识别：对 mono PDF 再跑一次 provider 识别并落盘。

背景：``agent/layout_geometry.json`` 与源侧 ``agent/source/provider/provider_ir.json``
都源自**源文档**，前端的「译文框」先前只能拿这些原文几何充数（实测同一段落的
``src_box`` 与 ``layout_box`` 逐位相同）。本模块在 build 成功产出 mono PDF **之后**
对译文 PDF 跑一次 provider 识别，把结果落到与源侧镜像对称的位置：

- ``agent/target/provider/provider_ir.json`` —— 规范化后的 block/line/span IR；
- ``agent/target_recognition.json`` —— 一份小清单（被识别的 PDF、内容哈希、页数、
  状态与原因），让服务端与前端能区分「还没识别」与「识别结果为空」。

坐标约定与被识别的 PDF 一致：MinerU 原生的 ``[x0, y0, x1, y1]``、左上原点 y 向下
（见 :mod:`babeldoc.docvision.provider_ir`），**本模块不做任何换算**，由消费方
（serve 端点标注 ``coord_system``、前端 ``pdfToScreen``）各自处理。

失败语义：识别失败（网络/超时/额度）**不阻断 build** —— 写 ``status=failed`` + 原因，
build 结果照常返回。
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
from pathlib import Path

from babeldoc_tools import common

logger = logging.getLogger(__name__)

#: 清单文件名（相对 ``<workdir>/agent``）。
MANIFEST_NAME = "target_recognition.json"

#: 译文侧 provider IR 的中立相对路径（与
#: :func:`babeldoc.docvision.provider_paths.target_provider_artifact_path` 同一位置）。
PROVIDER_IR_RELATIVE = "target/provider/provider_ir.json"

#: 清单里的 ``status``：识别成功 / 前置条件不满足没跑 / 跑了但失败。
STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

#: 目前实现译文侧识别的布局后端。译文侧识别复用源侧 MinerU 的上传/轮询/缓存链路；
#: Paddle 后端在 build 阶段没有可复用的同一套客户端，不在这里另起一份实现。
SUPPORTED_PROVIDER = "mineru"


def manifest_path(workdir) -> Path:
    return common.agent_dir(workdir) / MANIFEST_NAME


def provider_ir_path(workdir) -> Path:
    return common.agent_dir(workdir) / PROVIDER_IR_RELATIVE


def resolve_target_pdf(result: dict) -> Path | None:
    """要识别的译文 PDF：mono 优先、dual 次之（都取自同一次 build 的返回值）。"""
    for key in ("mono_pdf", "dual_pdf"):
        value = result.get(key)
        if isinstance(value, str) and Path(value).is_file():
            return Path(value)
    return None


def run_state_config(workdir, key: str) -> str | None:
    """``agent/run_state.json`` 里 ``config[key]`` 的非空字符串值；缺失 → ``None``。"""
    state = common.read_json(common.agent_dir(workdir) / "run_state.json", default=None)
    if not isinstance(state, dict):
        return None
    config = state.get("config")
    if not isinstance(config, dict):
        return None
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _page_count(path: Path | None) -> int | None:
    if path is None:
        return None
    import pymupdf

    try:
        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001 - 页数只是清单信息，读不出来不影响识别
        return None


def _relative(workdir: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(workdir))
    except ValueError:
        return str(path)


def _write_manifest(workdir: Path, payload: dict) -> Path:
    path = manifest_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _remove_provider_ir(workdir: Path) -> None:
    """删掉上一轮的译文侧 provider IR（本次 build 没有识别成功时不留陈旧产物）。

    产物描述的是"某一次编译后的译文 PDF"；上一个修订的 IR 与本次 PDF 不匹配，留下
    它会让前端拿旧版框叠在新版页面上。宁可报"尚未识别"，不报一个错的。
    """
    with contextlib.suppress(OSError):
        provider_ir_path(workdir).unlink(missing_ok=True)


def preflight(
    workdir, *, pdf: Path | None, enabled: bool | None, provider: str | None
) -> tuple[str, str | None, str | None, bool]:
    """``(status, reason, provider, 是否识别)`` —— 全部前置判据只有这一处。"""
    if pdf is None:
        return STATUS_SKIPPED, "build 没有产出可识别的译文 PDF", provider, False
    if enabled is False:
        return (
            STATUS_SKIPPED,
            "调用方显式关闭译文侧版面识别（--no-target-layout）",
            provider,
            False,
        )
    backend = provider or run_state_config(workdir, "layout")
    if backend is None:
        return (
            STATUS_SKIPPED,
            "agent/run_state.json 没有记录解析布局后端，无法确定识别 provider",
            None,
            False,
        )
    if backend != SUPPORTED_PROVIDER:
        return (
            STATUS_SKIPPED,
            f"布局后端 {backend!r} 没有译文侧版面识别实现（只有 {SUPPORTED_PROVIDER}）",
            backend,
            False,
        )
    if not common.env_default("MINERU_API_TOKEN"):
        return (
            STATUS_SKIPPED,
            "环境变量 MINERU_API_TOKEN 缺失，无法调用 MinerU 识别译文版面",
            backend,
            False,
        )
    return STATUS_OK, None, backend, True


def recognize_target_layout(
    workdir,
    result: dict,
    *,
    enabled: bool | None = None,
    provider: str | None = None,
    translate_config=None,
) -> dict:
    """对 build 产出的译文 PDF 跑一次 provider 识别并把结果落盘。

    ``enabled`` 是显式开关：``None`` = 自动（布局后端为 mineru 且 token 可用时执行），
    ``False`` = 调用方明确关闭（``--no-target-layout``）。任何失败都写清单后返回，
    **不抛异常** —— 译文侧识别不能反过来决定 build 的成败。
    """
    workdir = Path(workdir)
    pdf = resolve_target_pdf(result)
    _remove_provider_ir(workdir)
    status, reason, backend, should_run = preflight(
        workdir, pdf=pdf, enabled=enabled, provider=provider
    )
    if not should_run:
        logger.info("译文侧版面识别跳过：%s", reason)
        payload = {
            "status": status,
            "reason": reason,
            "pdf": _relative(workdir, pdf),
            "pdf_sha256": _sha256_file(pdf),
            "provider": backend,
            "page_count": _page_count(pdf),
            "provider_ir": None,
            "created_at": _utc_now(),
        }
        _write_manifest(workdir, payload)
        return payload

    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    try:
        MinerUDocLayoutModel(
            api_token=common.env_default("MINERU_API_TOKEN"),
            language=run_state_config(workdir, "lang_out"),
        ).recognize_pdf_provider_ir(
            pdf, provider_ir_path(workdir), translate_config=translate_config
        )
    except Exception as exc:  # noqa: BLE001 - 识别失败不阻断 build
        logger.warning("译文侧版面识别失败", exc_info=True)
        payload = {
            "status": STATUS_FAILED,
            "reason": f"{type(exc).__name__}: {exc}",
            "pdf": _relative(workdir, pdf),
            "pdf_sha256": _sha256_file(pdf),
            "provider": backend,
            "page_count": _page_count(pdf),
            "provider_ir": None,
            "created_at": _utc_now(),
        }
        _write_manifest(workdir, payload)
        return payload

    payload = {
        "status": STATUS_OK,
        "reason": None,
        "pdf": _relative(workdir, pdf),
        "pdf_sha256": _sha256_file(pdf),
        "provider": backend,
        "page_count": _page_count(pdf),
        "provider_ir": PROVIDER_IR_RELATIVE,
        "created_at": _utc_now(),
    }
    _write_manifest(workdir, payload)
    return payload
