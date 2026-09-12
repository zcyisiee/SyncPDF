r"""bbox 级 XeLaTeX 编译渲染器（带界缩小 + stamp 缓存）。

每个目标 bbox 建立一张与 bbox 同尺寸的独立 TeX 页面，用产品同款中文字体
（思源黑体/宋体，按路径加载）与 ``xeCJK`` + ``\XeTeXlinebreaklocale "zh"`` +
``PunctStyle=plain`` 排版；以源字号为首选，垂直溢出/overfull hbox 时按有界
步长缩小字号重试，达到最小字号仍失败则整体回退现有渲染路径。

- 单 bbox 编译有超时与独立临时目录，进程失败落入结构化 warning；
- stamp 以 (body, 字号, 尺寸) 为键缓存，同文本同字号不重复编译；
- 支持有界并发编译（每编译独立目录，无临时文件冲突）；
- 禁止跨页流动：每 bbox 独立约束在原页/原列区域内。
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

logger = logging.getLogger(__name__)

TEX_HEADER = r"""\documentclass{article}
\usepackage[paperwidth=%(w).4fbp,paperheight=%(h).4fbp,margin=0pt]{geometry}
\usepackage{fontspec}
\usepackage{xeCJK}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{graphicx}
\setCJKmainfont[Path=%(fontdir)s/,Extension=.ttf,UprightFont=%(cjkbase)s%(boldfont)s]{%(cjkbase)s}
\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip = 0pt plus 1pt
\xeCJKsetup{PunctStyle=plain}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\begin{document}
\fontsize{%(fs).4fbp}{%(lead).4fbp}\selectfont
%(body)s
\end{document}
"""

#: 有界缩小：每步 ×0.95，最多 12 步（≈0.54×），字号绝对下限 4pt。
#: 长度单位统一用 TeX ``bp``（= 1/72in = PDF 用户单位）：父页面 bbox/fit
#: 都用 PDF bp，若用 ``pt``（1/72.27in）会差 0.37% 并被 show_pdf_page 拉伸。
_SHRINK_FACTOR = 0.95
_MAX_SHRINK_STEPS = 12
_MIN_FONT_SIZE = 4.0
#: 行距系数：与产品 Typesetting 的 CJK 默认 line_skip(1.5) 一致。
DEFAULT_LEAD_RATIO = 1.5
_DEFAULT_LEAD_RATIO = DEFAULT_LEAD_RATIO
#: 墨迹/overfull 判定容差（pt），吸收 geometry 舍入。
_FIT_TOLERANCE = 0.5
#: 进程内 stamp 缓存上限（LRU 近似：超限清空）。
_CACHE_LIMIT = 512


@dataclass(slots=True)
class StampRequest:
    """一次 bbox 编译请求。"""

    key: str
    body: str
    width: float
    height: float
    font_size: float
    #: 期望可见文本（去标记、去公式的纯文本），用于检测垂直裁剪丢字。
    expected_text: str = ""

    @property
    def cache_key(self) -> tuple:
        return (
            self.body,
            round(self.width, 2),
            round(self.height, 2),
            round(self.font_size, 2),
            _DEFAULT_LEAD_RATIO,
        )


@dataclass(slots=True)
class StampResult:
    """一次 bbox 编译结果（含有界缩小的全部尝试）。"""

    key: str
    ok: bool = False
    pdf_path: str | None = None
    font_size: float | None = None
    scale: float | None = None
    seconds: float = 0.0
    compile_attempts: int = 0
    reason: str = ""
    log_excerpt: list[str] = field(default_factory=list)


#: 同形数学字形折叠表（仅用于比较）：XeLaTeX 数学字体经 PyMuPDF 抽取时，
#: 部分字形映射到形状相同但码点不同的符号（如 ``\Delta`` 落成 INCREMENT）。
_GLYPH_ALIASES = str.maketrans(
    {
        "\u2206": "\u0394",  # ∆ INCREMENT → Δ GREEK CAPITAL DELTA
        "\u2212": "-",  # − MINUS SIGN
        "\u2010": "-",  # ‐ HYPHEN
        "\u2011": "-",  # ‑ NON-BREAKING HYPHEN
        "\u2044": "/",  # ⁄ FRACTION SLASH
        "\u2215": "/",  # ∕ DIVISION SLASH
        "\u2217": "*",  # ∗ ASTERISK OPERATOR
        "\u22c5": "*",  # ⋅ DOT OPERATOR
        "\u00b7": "*",  # · MIDDLE DOT
        "\u2236": ":",  # ∶ RATIO
    }
)


def normalize_rendered_text(text: str) -> str:
    """渲染文本的比较键：NFKC + 同形字形折叠 + 去全部空白。

    XeLaTeX 渲染出的字形与原生字符可能字面不同（``𝑛`` 与 ``n``、
    ``$\times$`` 与 ``×``），NFKC 把兼容等价形式折叠后再比较，避免把
    「渲染差异」误判成丢字。
    """
    normalized = unicodedata.normalize("NFKC", text or "").translate(_GLYPH_ALIASES)
    return re.sub(r"\s+", "", normalized)


def text_layer_matches(expected: str, extracted: str) -> bool:
    """文本层是否完整包含期望文本（验收与 fit 判定共用口径）。

    归一化（NFKC + 同形字形折叠 + 去空白）后：完全相等 → True；否则要求
    期望文本是抽取文本的**子序列**（大小写不敏感），即「可以多出渲染插入的
    字符（断词连字符、额外字形），但一个都不能少」。
    """
    left = normalize_rendered_text(expected).casefold()
    right = normalize_rendered_text(extracted).casefold()
    if left == right:
        return True
    return _is_subsequence(left, right)


def _is_subsequence(expected: str, extracted: str) -> bool:
    """``expected`` 是否是 ``extracted`` 的子序列（容忍渲染插入的字符）。

    断词连字符、LaTeX 额外字形都会在文本层多出字符；丢字则是**缺少**
    期望字符。子序列判定正好「容忍多、不容忍少」，并且大小写不敏感
    （MinerU LaTeX 与原生字符偶有大小写差异，不是丢字）。
    """
    index = 0
    for ch in extracted:
        if index < len(expected) and ch == expected[index]:
            index += 1
    return index == len(expected)


def _measure_fit(
    pdf_path: Path,
    width: float,
    height: float,
    expected_text: str = "",
) -> tuple[bool, str, int]:
    """打开编译产物检查墨迹边界、可抽取文本与内容完整性。

    ``expected_text`` 非空时额外做「无丢字」校验：PyMuPDF 的文本抽取会裁剪到
    页面边界，排版溢出页面底部的行会静默消失（TeX 的 overfull vbox 不一定是
    错误）。这里比对归一化后的长度与结尾字符，防止贴片静默吃掉译文。
    返回 (fits, reason, text_chars)。
    """
    import pymupdf

    doc = pymupdf.open(pdf_path)
    try:
        page = doc[0]
        extracted = normalize_rendered_text(page.get_text())
        text_chars = len(extracted)
        ink = pymupdf.Rect()
        for block in page.get_text("blocks"):
            ink |= pymupdf.Rect(block[:4])
        if text_chars == 0:
            return False, "no-extractable-text", 0
        if not ink.is_empty and width > 0 and height > 0:
            if ink.x1 > width + _FIT_TOLERANCE or ink.x0 < -_FIT_TOLERANCE:
                return False, "horizontal-overflow", text_chars
            if ink.y1 > height + _FIT_TOLERANCE or ink.y0 < -_FIT_TOLERANCE:
                return False, "vertical-overflow", text_chars
        if expected_text:
            expected = normalize_rendered_text(expected_text)
            if expected:
                # 全文归一化比较：渲染结果必须逐字符等于期望可见文本
                # （fragment 位图区域已在调用方从 expected 中排除）。
                if extracted != expected:
                    # 长度骤减 / 结尾缺失是最典型的静默裁剪信号。
                    if len(extracted) < len(expected) * 0.98:
                        return False, "text-clipped", text_chars
                    if expected[-3:] not in extracted:
                        return False, "text-clipped-tail", text_chars
                    # 归一化全文比较（大小写不敏感）：容忍渲染插入，
                    # 不接受任何期望字符缺失。
                    if not _is_subsequence(expected.casefold(), extracted.casefold()):
                        return False, "text-mismatch", text_chars
        return True, "ok", text_chars
    finally:
        doc.close()


class BboxStampRenderer:
    """bbox 编译器：编译 + 有界缩小 + 缓存 + 有界并发。"""

    def __init__(self, capability, timeout_seconds: float = 45.0, max_workers: int = 2):
        self._capability = capability
        self._timeout = max(5.0, float(timeout_seconds))
        self._max_workers = max(1, int(max_workers))
        self._cache: dict[tuple, StampResult] = {}
        self._lock = threading.Lock()
        self._cache_hits = 0
        self._compile_seconds = 0.0

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    @property
    def compile_seconds(self) -> float:
        return self._compile_seconds

    def build_tex(
        self, body: str, width: float, height: float, font_size: float
    ) -> str:
        font_path = Path(self._capability.font_path)
        cjk_base = font_path.stem
        bold = self._capability.bold_font_path
        bold_clause = f",BoldFont={Path(bold).stem}" if bold else ""
        return TEX_HEADER % {
            "w": width,
            "h": height,
            "fontdir": font_path.parent,
            "cjkbase": cjk_base,
            "boldfont": bold_clause,
            "fs": font_size,
            "lead": font_size * _DEFAULT_LEAD_RATIO,
            "body": body,
        }

    def render_one(self, request: StampRequest, workdir: Path) -> StampResult:
        """编译单个请求（含缓存与有界缩小）。调用方负责 workdir 生命周期。"""
        with self._lock:
            cached = self._cache.get(request.cache_key)
        if cached is not None:
            self._cache_hits += 1
            return cached

        result = self._render_uncached(request, workdir)
        with self._lock:
            if len(self._cache) >= _CACHE_LIMIT:
                self._cache.clear()
            self._cache[request.cache_key] = result
        return result

    def render_many(self, requests: list[tuple[StampRequest, Path]]) -> dict[str, StampResult]:
        """并发编译一批请求；每个请求使用独立临时目录，互不冲突。"""
        if not requests:
            return {}
        if len(requests) == 1 or self._max_workers <= 1:
            return {
                request.key: self.render_one(request, workdir)
                for request, workdir in requests
            }
        results: dict[str, StampResult] = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(self.render_one, request, workdir): request.key
                for request, workdir in requests
            }
            for future, key in futures.items():
                results[key] = future.result()
        return results

    def _render_uncached(self, request: StampRequest, workdir: Path) -> StampResult:
        started = time.perf_counter()
        result = StampResult(key=request.key)
        font_size = request.font_size
        reasons: list[str] = []
        logs: list[str] = []
        # 目录名带 request.key：内容相同的不同段落各自独立编译目录，
        # 并发批编译时不会互相覆盖（缓存命中仍按内容键去重）。
        stem = hashlib.sha1(  # noqa: S324 - 非密码学用途，仅作临时文件名
            f"{request.key}|{request.cache_key}".encode()
        ).hexdigest()[:16]
        safe_key = re.sub(r"[^0-9A-Za-z_-]", "_", str(request.key))[:24]
        stem = f"{safe_key}-{stem}"
        try:
            for step in range(_MAX_SHRINK_STEPS + 1):
                result.compile_attempts = step + 1
                attempt_dir = workdir / f"{stem}_s{step}"
                attempt_dir.mkdir(parents=True, exist_ok=True)
                tex = self.build_tex(request.body, request.width, request.height, font_size)
                outcome = self._compile_tex(tex, attempt_dir, stem, font_size)
                logs.extend(outcome["errors"][:3])
                if not outcome["compiled"]:
                    # TeX 错误不重试缩小：直接失败回退。
                    result.reason = outcome["reason"] or "compile-failed"
                    result.log_excerpt = logs
                    return result
                fits, fit_reason, _ = _measure_fit(
                    attempt_dir / f"{stem}.pdf", request.width, request.height,
                    request.expected_text,
                )
                if outcome["overfull_hbox"]:
                    fits, fit_reason = False, "overfull-hbox"
                if outcome["overfull_vbox"]:
                    fits, fit_reason = False, "overfull-vbox"
                if fits:
                    result.ok = True
                    result.pdf_path = str(attempt_dir / f"{stem}.pdf")
                    result.font_size = font_size
                    result.scale = font_size / request.font_size
                    result.reason = "ok"
                    result.log_excerpt = logs
                    return result
                reasons.append(f"s{step}:{fit_reason}")
                if fit_reason in ("text-mismatch", "no-extractable-text"):
                    # 内容不一致/无文本不是尺寸问题，缩小字号无法修复。
                    result.reason = ";".join(reasons)
                    result.log_excerpt = logs
                    return result
                next_size = max(font_size * _SHRINK_FACTOR, _MIN_FONT_SIZE)
                if next_size >= font_size:
                    break
                font_size = next_size
            result.reason = ";".join(reasons) or "shrink-exhausted"
            result.log_excerpt = logs
            return result
        finally:
            result.seconds = round(time.perf_counter() - started, 3)
            self._compile_seconds += result.seconds

    def _compile_tex(self, tex: str, workdir: Path, stem: str, font_size: float) -> dict:
        tex_path = workdir / f"{stem}.tex"
        tex_path.write_text(tex, encoding="utf-8")
        try:
            proc = subprocess.run(  # noqa: S603 - 可执行文件已由能力探测校验
                [
                    self._capability.xelatex_path,
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    f"-output-directory={workdir}",
                    str(tex_path),
                ],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("LaTeX bbox 编译超时（%.1fs）：丢弃该段落", self._timeout)
            return {"compiled": False, "reason": "timeout", "overfull_hbox": 0, "overfull_vbox": 0, "errors": [f"timeout@{font_size:.1f}pt"]}
        except OSError as exc:
            logger.warning("LaTeX bbox 编译进程失败: %s", exc)
            return {"compiled": False, "reason": f"oserror:{exc}", "overfull_hbox": 0, "overfull_vbox": 0, "errors": []}

        log = proc.stdout or ""
        errors = re.findall(r"^! .*$", log, flags=re.M)
        overfull = len(re.findall(r"Overfull \\hbox", log))
        overfull_vbox = len(re.findall(r"Overfull \\vbox", log))
        pdf_path = workdir / f"{stem}.pdf"
        compiled = proc.returncode == 0 and pdf_path.exists()
        return {
            "compiled": compiled,
            "reason": "" if compiled else ";".join(errors[:3]) or f"exit={proc.returncode}",
            "overfull_hbox": overfull,
            "overfull_vbox": overfull_vbox,
            "errors": errors,
        }
