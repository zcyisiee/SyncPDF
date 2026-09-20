r"""bbox 级 XeLaTeX 编译渲染器（带界缩小 + stamp 缓存）。

每个目标 bbox 建立一张与 bbox 同尺寸的独立 TeX 页面，用产品同款中文字体
（思源黑体/宋体，按路径加载）与 ``xeCJK`` + ``\XeTeXlinebreaklocale "zh"`` +
``PunctStyle=plain`` 排版；以源字号为首选，垂直溢出/overfull hbox 时按有界
步长缩小字号重试，达到最小字号仍失败则整体回退现有渲染路径。

- 单 bbox 编译有超时与独立临时目录，进程失败落入结构化 warning；
- stamp 以 (body, 字号, 尺寸) 为键缓存，同文本同字号不重复编译；
- 支持有界并发编译（每编译独立目录，无临时文件冲突）；
- 禁止跨页流动：每 bbox 独立约束在原页/原列区域内。

本模块同时提供 :class:`BatchStampRenderer`（``renderer_batch``）复用的公共件：
:data:`TEX_COMMON` 导言区、:func:`font_setup_clauses` / :func:`topskip_clause`
与 :func:`_measure_fit`（支持指定 PDF 页号）。
"""

from __future__ import annotations

import contextlib
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
from functools import lru_cache
from pathlib import Path

from babeldoc.debug_recorder import CompileCandidate

logger = logging.getLogger(__name__)

#: 导言区公共部分（不含纸张尺寸与正文）：单段与批编译共用，避免模板漂移。
#: ``xcolor``/``hyperref`` 供融合的角标链接渲染（``\\textcolor``/``\\href``）；
#: hyperref 惯例最后加载，``hidelinks`` 关掉它自己的着色/边框（颜色由快照还原）。
TEX_COMMON = r"""\usepackage{fontspec}
\usepackage{xeCJK}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{graphicx}
\usepackage[english]{babel}
\usepackage{url}
\usepackage{xcolor}
%(fontsetup)s
\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip = 0pt plus 0.3em
\xeCJKsetup{PunctStyle=plain}
\hyphenpenalty=50
\tolerance=1500
\emergencystretch=1em
%% Keep baseline spacing unless tall inline content needs collision-avoiding glue.
\lineskiplimit=0pt
\lineskip=1pt
\pagestyle{empty}
\usepackage[hidelinks]{hyperref}
"""

TEX_HEADER = (
    r"""\documentclass{article}
\usepackage[paperwidth=%(w).4fbp,paperheight=%(h).4fbp,margin=0pt]{geometry}
"""
    + TEX_COMMON
    + r"""\setlength{\parindent}{%(parindent).4fbp}
\setlength{\parskip}{0pt}
%(topskip)s
\begin{document}
\fontsize{%(fs).4fbp}{%(lead).4fbp}\selectfont
%(hangindent)s%(body)s
\end{document}
"""
)

#: 模板/排版指令版本：进入持久化 stamp 缓存 key，改模板必须同步递增。
#: p6: Restore TeX interline collision avoidance for tall formulas/fragments.
TEMPLATE_VERSION = "latex-bbox-2026-09-p6"

#: 有界缩小：每步 ×0.95，最多 12 步（≈0.54×），字号绝对下限 4pt。
#: 长度单位统一用 TeX ``bp``（= 1/72in = PDF 用户单位）：父页面 bbox/fit
#: 都用 PDF bp，若用 ``pt``（1/72.27in）会差 0.37% 并被 show_pdf_page 拉伸。
_SHRINK_FACTOR = 0.95
_MAX_SHRINK_STEPS = 12
_MIN_FONT_SIZE = 4.0
#: 行距系数：产品 Typesetting 的 CJK 默认 line_skip(1.5)，用于没有源行距时。
DEFAULT_LEAD_RATIO = 1.5
_DEFAULT_LEAD_RATIO = DEFAULT_LEAD_RATIO
#: 源行距推导的可行区间（相对源字号）：clamp(baseline_pitch, 1.3fs, 1.6fs)。
_LEAD_RATIO_MIN = 1.3
_LEAD_RATIO_MAX = 1.6
#: 字号缩小前先试的行距系数（相对推导出的源行距）。
_LEAD_TRIAL_RATIOS = (1.1, 0.9)
#: 字体 ascender 探测失败时的保守上限（首行墨迹不会被裁）。
_DEFAULT_ASCENT_RATIO = 1.15
#: ``\topskip`` 额外余量（em）：行内数学的上标会把墨迹抬到字体 ascender 之上。
_TOPSKIP_HEADROOM_EM = 0.1
#: 墨迹/overfull 判定容差（pt），吸收 geometry 舍入（垂直方向使用）。
_FIT_TOLERANCE = 0.5
#: 水平方向（宽度）墨迹越界与 overfull hbox 共用的容差（pt）：TeX/PyMuPDF
#: 的宽度口径都是 advance 盒（含字形右侧轴承），轻微超宽时可见墨迹几乎总在
#: 页内；垂直判定仍用 ``_FIT_TOLERANCE``。
_WIDTH_TOLERANCE = 2.5
#: 进程内 stamp 缓存上限（LRU 近似：超限清空）。
_CACHE_LIMIT = 512


def font_signature(capability) -> str:
    """字体签名：字体文件变了缓存贴片必须失效（不允许串用旧字形）。"""
    parts: list[str] = []
    for group in (
        capability.latin_serif_fonts or {},
        capability.latin_sans_fonts or {},
        capability.cjk_serif_fonts or {},
        capability.cjk_sans_fonts or {},
    ):
        parts.extend(f"{key}={value}" for key, value in sorted(group.items()))
    # 可选字体族（段落级 font_family）：族文件变了同样要失效。
    for family_id, files in sorted((capability.cjk_family_fonts or {}).items()):
        parts.append(f"family={family_id}")
        parts.extend(f"{key}={value}" for key, value in sorted(files.items()))
    if capability.font_path:
        parts.append(f"explicit={capability.font_path}")
    if capability.bold_font_path:
        parts.append(f"explicit-bold={capability.bold_font_path}")
    payload = "|".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - 非密码学用途


def cache_namespace(capability) -> str:
    """持久化 stamp 缓存的命名空间：模板版本 + 字体签名。"""
    return f"{TEMPLATE_VERSION}|{font_signature(capability)}"


def derive_lead(font_size: float, baseline_pitch: float | None) -> float:
    """由源行间距推导行距：``clamp(baseline_pitch, 1.3fs, 1.6fs)``。

    没有源行距（旧产物/无几何）时退回产品 CJK 默认系数 1.5。
    """
    if not baseline_pitch or baseline_pitch <= 0 or font_size <= 0:
        return font_size * DEFAULT_LEAD_RATIO
    return min(
        max(float(baseline_pitch), font_size * _LEAD_RATIO_MIN),
        font_size * _LEAD_RATIO_MAX,
    )


@lru_cache(maxsize=8)
def _font_ascent_ratio(font_path: str) -> float:
    """字体 ascender（em）：``\topskip`` 按真实字面上开，失败时取保守上限。"""
    try:
        import pymupdf

        value = float(pymupdf.Font(fontfile=font_path).ascender)
        return value if value > 0 else _DEFAULT_ASCENT_RATIO
    except Exception:  # noqa: BLE001 - 字体不可读时用保守值
        return _DEFAULT_ASCENT_RATIO


def regular_font_paths(
    capability, serif: bool, font_family: str | None = None
) -> list[str]:
    """实际会用到的正体字体路径（算 ``\topskip`` 的 ascender）。"""
    paths: list[str] = []
    latin = capability.latin_fonts(serif)
    if latin and latin.get("regular"):
        paths.append(latin["regular"])
    if capability.font_explicit:
        if capability.font_path:
            paths.append(capability.font_path)
        return paths
    family = _family_cjk_fonts(capability, font_family)
    if family is not None:
        paths.append(family["regular"])
        return paths
    cjk = capability.cjk_fonts(serif)
    if cjk and cjk.get("regular"):
        paths.append(cjk["regular"])
    elif capability.font_path:
        paths.append(capability.font_path)
    return paths


def _family_cjk_fonts(capability, font_family: str | None) -> dict[str, str] | None:
    """请求的段落级字体族（未命中/未探测到/显式字体优先时返回 None）。"""
    if capability.font_explicit:
        return None
    return capability.cjk_family(font_family)


def font_setup_clauses(
    capability, serif: bool, font_family: str | None = None
) -> str:
    """字体声明：与产品一致（拉丁 Noto Serif/Sans，中文 Source Han Serif/Sans）。

    显式 ``--latex-cjk-font-path`` 优先（用户指定则只设中文主字体，拉丁
    仍尽力用产品字体）；段落级 ``font_family`` 命中注册表且已探测到时用该族
    的中文主字体（拉丁仍按 ``serif`` 选）；拉丁字体缺失时不声明 → 退回
    Latin Modern。
    """
    clauses: list[str] = []
    latin = capability.latin_fonts(serif)
    if latin:
        clauses.append(_face_clause("setmainfont", latin))
    if capability.font_explicit and capability.font_path:
        explicit = {"regular": capability.font_path}
        if capability.bold_font_path:
            explicit["bold"] = capability.bold_font_path
        clauses.append(_face_clause("setCJKmainfont", explicit))
        return "\n".join(clauses)
    family = _family_cjk_fonts(capability, font_family)
    if family is not None:
        clauses.append(_face_clause("setCJKmainfont", family))
        return "\n".join(clauses)
    cjk = capability.cjk_fonts(serif) or capability.cjk_fonts(not serif)
    if cjk is None and capability.font_path:
        fallback = {"regular": capability.font_path}
        if capability.bold_font_path:
            fallback["bold"] = capability.bold_font_path
        cjk = fallback
    if cjk:
        clauses.append(_face_clause("setCJKmainfont", cjk))
    return "\n".join(clauses)


def topskip_clause(
    capability,
    serif: bool,
    font_size: float,
    topskip: float | None,
    font_family: str | None = None,
) -> str:
    """``\topskip`` 声明：源首行字顶余量 + 字体 ascender（不裁剪首行墨迹）。"""
    if topskip is None:
        return ""
    ascent_ratio = max(
        (
            _font_ascent_ratio(path)
            for path in regular_font_paths(capability, serif, font_family)
        ),
        default=_DEFAULT_ASCENT_RATIO,
    )
    value = max(0.0, float(topskip)) + (ascent_ratio + _TOPSKIP_HEADROOM_EM) * font_size
    return f"\\setlength{{\\topskip}}{{{value:.4f}bp}}\n"


def _face_clause(command: str, files: dict[str, str]) -> str:
    """用路径加载字体的一行 fontspec 声明（四体可选）。"""
    regular = Path(files["regular"])
    options = [
        f"Path={regular.parent}/",
        "Extension=.ttf",
        f"UprightFont={regular.stem}",
    ]
    for key, option in (
        ("bold", "BoldFont"),
        ("italic", "ItalicFont"),
        ("bolditalic", "BoldItalicFont"),
    ):
        value = files.get(key)
        if value:
            options.append(f"{option}={Path(value).stem}")
    return "\\{command}[{options}]{{{regular.stem}}}".format(
        command=command, options=",".join(options), regular=regular
    )


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
    #: 源行距（bp）。None → ``font_size * DEFAULT_LEAD_RATIO``。
    lead: float | None = None
    #: 首行缩进（bp，正=缩进、负=悬挂 → 转成 hangindent）。
    first_line_dx: float = 0.0
    #: 首行字顶到 box 顶的距离（bp），写入 ``\topskip``；None=无源几何，不设。
    ascent_top: float | None = None
    #: 段落主字体是否衬线（决定拉丁/中文用 Noto Serif/Source Han Serif 还是 Sans）。
    serif: bool = True
    #: 段落级中文字体族 id（``font_families.FONT_FAMILY_IDS``）；None = 按
    #: ``serif`` 用产品默认族，行为与历史版本完全一致。
    font_family: str | None = None
    #: 诊断专用：上一次失败编译的候选 id（扩框/回退重试的父节点）。
    #: 只进证据链，不进 ``cache_key``、不影响任何排版语义。
    debug_parent: str | None = None

    @property
    def indentation(self) -> tuple[float, float]:
        r"""→ (parindent, hangindent)，保留首行相对其余行的偏移。

        正 dx（首行缩进）→ ``\parindent=dx``；
        负 dx（悬挂：首行在段落左缘、其余行缩进）→ ``\hangindent=|dx|`` 且
        ``\hangafter=1``（只缩首行之后的行），``\parindent=0``。
        """
        dx = float(self.first_line_dx or 0.0)
        if dx >= 0.0:
            return dx, 0.0
        return 0.0, abs(dx)

    @property
    def cache_key(self) -> tuple:
        return (
            self.body,
            round(self.width, 2),
            round(self.height, 2),
            round(self.font_size, 2),
            round(float(self.lead), 3) if self.lead else None,
            round(float(self.first_line_dx or 0.0), 3),
            None if self.ascent_top is None else round(float(self.ascent_top), 3),
            bool(self.serif),
            self.font_family,
        )


@dataclass(slots=True)
class StampResult:
    """一次 bbox 编译结果（含有界缩小的全部尝试）。"""

    key: str
    ok: bool = False
    pdf_path: str | None = None
    font_size: float | None = None
    scale: float | None = None
    #: 实际使用的行距（bp），供决策报告与验收对账。
    lead: float | None = None
    seconds: float = 0.0
    compile_attempts: int = 0
    reason: str = ""
    log_excerpt: list[str] = field(default_factory=list)
    #: 诊断专用：本结果对应的候选证据引用（``candidate_id``/``request_id``/
    #: ``batch_id``/``pdf_page_index``）。缓存命中时由持久缓存元数据还原；
    #: 无证据（旧缓存/非 debug 运行）时为空 dict，绝不推断。
    debug_ref: dict = field(default_factory=dict)


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
    page_index: int = 0,
) -> tuple[bool, str, int]:
    """打开编译产物检查墨迹边界、可抽取文本与内容完整性。

    ``expected_text`` 非空时额外做「无丢字」校验：PyMuPDF 的文本抽取会裁剪到
    页面边界，排版溢出页面底部的行会静默消失（TeX 的 overfull vbox 不一定是
    错误）。这里比对归一化后的长度与结尾字符，防止贴片静默吃掉译文。

    ``page_index``：批编译产物是多页文档，测量哪一页（单段产物固定第 0 页）。
    返回 (fits, reason, text_chars)。
    """
    import pymupdf

    doc = pymupdf.open(pdf_path)
    try:
        if page_index < 0 or page_index >= len(doc):
            return False, "page-missing", 0
        return _measure_page_fit(
            doc[page_index], width, height, expected_text
        )
    finally:
        doc.close()


def _measure_page_fit(
    page,
    width: float,
    height: float,
    expected_text: str = "",
) -> tuple[bool, str, int]:
    """Measure a page that is already open.

    Batch rendering used to write every candidate page to a temporary PDF and
    reopen it before measuring.  A block can contain several variants per
    paragraph, so that turned into hundreds of unnecessary PDF open/save
    operations.  Keeping the block document open and measuring its pages
    directly avoids that I/O while preserving the exact checks used by
    :func:`_measure_fit`.
    """
    import pymupdf

    try:
        extracted = normalize_rendered_text(page.get_text())
        text_chars = len(extracted)
        ink = pymupdf.Rect()
        for block in page.get_text("blocks"):
            ink |= pymupdf.Rect(block[:4])
        if text_chars == 0:
            return False, "no-extractable-text", 0
        if not ink.is_empty and width > 0 and height > 0:
            # 水平方向用更宽的容差：宽度口径是 advance 盒（含右侧轴承），
            # 轻微超宽（≤ _WIDTH_TOLERANCE）时可见墨迹几乎总在页内。
            if ink.x1 > width + _WIDTH_TOLERANCE or ink.x0 < -_WIDTH_TOLERANCE:
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
    except Exception:  # noqa: BLE001 - malformed page counts as a failed fit
        logger.debug("测量 LaTeX 页面失败", exc_info=True)
        return False, "page-measure-failed", 0


#: ``Overfull \hbox`` 行及其括号内的超宽量说明（如 ``3.05pt too wide``）。
_OVERFULL_HBOX_LINE = re.compile(r"Overfull \\hbox(?: \(([^)]*)\))?")
_OVERFULL_HBOX_WIDE = re.compile(r"([0-9]+(?:\.[0-9]+)?)pt too wide")


def overfull_hbox_exceeds(line: str, tolerance: float) -> bool:
    """单行 ``Overfull \\hbox`` 是否超宽超过 ``tolerance``。

    不是 overfull hbox 行 → False；解析不出 pt 值（badness 形式、无括号）→
    保守 True。批渲染器（``renderer_batch._attribute_log``）按行归属时共用。
    """
    match = _OVERFULL_HBOX_LINE.search(line)
    if match is None:
        return False
    detail = match.group(1)
    if detail is None:
        return True
    amount = _OVERFULL_HBOX_WIDE.fullmatch(detail)
    return amount is None or float(amount.group(1)) > tolerance


def _count_overfull_hbox(log: str, tolerance: float) -> int:
    """统计超宽量超过 ``tolerance`` 的 ``Overfull \\hbox`` 行数。

    TeX 报 ``Overfull \\hbox (3.05pt too wide) in paragraph ...``；宽度口径是
    advance 盒，轻微超宽（≤ tolerance）不再算失败。解析不出 pt 值的行
    （badness 形式、无括号）保守计数。
    """
    return sum(
        1 for line in log.split("\n") if overfull_hbox_exceeds(line, tolerance)
    )


class BboxStampRenderer:
    """bbox 编译器：编译 + 有界缩小 + 缓存 + 有界并发。"""

    def __init__(
        self,
        capability,
        timeout_seconds: float = 45.0,
        max_workers: int = 2,
        cache=None,
        debug_recorder=None,
    ):
        self._capability = capability
        self._timeout = max(5.0, float(timeout_seconds))
        self._max_workers = max(1, int(max_workers))
        #: 可选的跨进程缓存（:class:`StampCache`）；None 时只做进程内缓存。
        self._persistent = cache
        #: 可选的诊断 recorder；None 时零额外 IO、行为与非 debug 完全一致。
        self._debug_recorder = debug_recorder
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
        self,
        body: str,
        width: float,
        height: float,
        font_size: float,
        *,
        lead: float | None = None,
        parindent: float = 0.0,
        hangindent: float = 0.0,
        topskip: float | None = None,
        serif: bool = True,
        font_family: str | None = None,
    ) -> str:
        effective_lead = float(lead) if lead else font_size * _DEFAULT_LEAD_RATIO
        hang_clause = ""
        if hangindent:
            # ``\hangindent``/``\hangafter`` 只在正文里生效（导言区赋值会被
            # ``\begin{document}`` 重置），因此放在 ``\selectfont`` 之后。
            hang_clause = (
                f"\\setlength{{\\hangindent}}{{{hangindent:.4f}bp}}\\hangafter=1\n"
            )
        # ``\topskip`` = 源首行字顶余量 + 字体 ascender：首行墨迹不从 box 顶溢出
        # （TeX 把首行 baseline 放在 ``max(\topskip, 行高)``）。
        return TEX_HEADER % {
            "w": width,
            "h": height,
            "fontsetup": self._font_setup(serif, font_family),
            "fs": font_size,
            "lead": effective_lead,
            "parindent": parindent,
            "hangindent": hang_clause,
            "topskip": topskip_clause(
                self._capability, serif, font_size, topskip, font_family
            ),
            "body": body,
        }

    def _regular_font_paths(
        self, serif: bool, font_family: str | None = None
    ) -> list[str]:
        """实际会用到的正体字体路径（算 ``\topskip`` 的 ascender）。"""
        return regular_font_paths(self._capability, serif, font_family)

    def _font_setup(self, serif: bool, font_family: str | None = None) -> str:
        """字体声明（委托 :func:`font_setup_clauses`，与批编译共用）。"""
        return font_setup_clauses(self._capability, serif, font_family)

    def render_one(self, request: StampRequest, workdir: Path) -> StampResult:
        """编译单个请求（含缓存与有界缩小）。调用方负责 workdir 生命周期。"""
        request_id = self._record_requests([request])
        with self._lock:
            cached = self._cache.get(request.cache_key)
        if cached is not None:
            self._cache_hits += 1
            self._record_reuse(request, cached, "process_cache", request_id)
            return cached
        if self._persistent is not None:
            from_disk = self._persistent.get(request)
            if from_disk is not None:
                self._cache_hits += 1
                with self._lock:
                    self._cache[request.cache_key] = from_disk
                self._record_reuse(request, from_disk, "persistent_cache", request_id)
                return from_disk

        result = self._render_uncached(request, workdir, request_id=request_id)
        if result.ok and self._persistent is not None:
            result = self._persistent.put(request, result) or result
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

    def _render_uncached(
        self, request: StampRequest, workdir: Path, *, request_id: str | None = None
    ) -> StampResult:
        started = time.perf_counter()
        result = StampResult(key=request.key)
        base_lead = (
            float(request.lead)
            if request.lead
            else request.font_size * _DEFAULT_LEAD_RATIO
        )
        parindent, hangindent = request.indentation
        # 尝试阶梯（P3-4）：先源字号 + 源行距，再在 [0.9, 1.1]×源行距 调两步，
        # 最后才进有界字号缩小（行距随字号等比缩放）。
        ladder: list[tuple[float, float]] = [(request.font_size, base_lead)]
        ladder.extend(
            (request.font_size, base_lead * ratio) for ratio in _LEAD_TRIAL_RATIOS
        )
        for step in range(1, _MAX_SHRINK_STEPS + 1):
            size = max(
                request.font_size * (_SHRINK_FACTOR**step), _MIN_FONT_SIZE
            )
            ladder.append((size, base_lead * (size / request.font_size)))
            if size <= _MIN_FONT_SIZE:
                break
        reasons: list[str] = []
        logs: list[str] = []
        # 目录名带 request.key：内容相同的不同段落各自独立编译目录，
        # 并发批编译时不会互相覆盖（缓存命中仍按内容键去重）。
        stem = hashlib.sha1(  # noqa: S324 - 非密码学用途，仅作临时文件名
            f"{request.key}|{request.cache_key}".encode()
        ).hexdigest()[:16]
        safe_key = re.sub(r"[^0-9A-Za-z_-]", "_", str(request.key))[:24]
        stem = f"{safe_key}-{stem}"
        # 诊断：候选阶梯顺序就是编译顺序，逐步记录（父链 = 前一档候选）。
        parent_id = request.debug_parent
        try:
            for step, (font_size, lead) in enumerate(ladder):
                result.compile_attempts = step + 1
                attempt_dir = workdir / f"{stem}_s{step}"
                attempt_dir.mkdir(parents=True, exist_ok=True)
                candidate_id = (
                    self._debug_recorder.new_id("candidate")
                    if self._debug_recorder
                    else None
                )
                tex = self.build_tex(
                    request.body,
                    request.width,
                    request.height,
                    font_size,
                    lead=lead,
                    parindent=parindent,
                    hangindent=hangindent,
                    topskip=request.ascent_top,
                    serif=request.serif,
                    font_family=request.font_family,
                )
                outcome = self._compile_tex(
                    tex, attempt_dir, stem, font_size, debug_candidate=candidate_id
                )
                logs.extend(outcome["errors"][:3])
                if not outcome["compiled"]:
                    # TeX 错误不重试缩小：直接失败回退。
                    result.reason = outcome["reason"] or "compile-failed"
                    result.log_excerpt = logs
                    parent_id = self._record_candidate(
                        request,
                        request_id=request_id,
                        candidate_id=candidate_id,
                        priority=step,
                        font_size=font_size,
                        lead=lead,
                        parent_id=parent_id,
                        tex=tex,
                        attempt_dir=attempt_dir,
                        stem=stem,
                        status="failed",
                        reason=result.reason,
                    )
                    result.debug_ref = {
                        "candidate_id": parent_id,
                        "request_id": request_id,
                    }
                    return result
                fits, fit_reason, _ = _measure_fit(
                    attempt_dir / f"{stem}.pdf",
                    request.width,
                    request.height,
                    request.expected_text,
                )
                if outcome["overfull_hbox"]:
                    fits, fit_reason = False, "overfull-hbox"
                if outcome["overfull_vbox"]:
                    fits, fit_reason = False, "overfull-vbox"
                parent_id = self._record_candidate(
                    request,
                    request_id=request_id,
                    candidate_id=candidate_id,
                    priority=step,
                    font_size=font_size,
                    lead=lead,
                    parent_id=parent_id,
                    tex=tex,
                    attempt_dir=attempt_dir,
                    stem=stem,
                    status="ok" if fits else "failed",
                    reason=fit_reason,
                    selected=bool(fits),
                    fit={"fits": bool(fits), "reason": fit_reason},
                )
                if fits:
                    result.ok = True
                    result.pdf_path = str(attempt_dir / f"{stem}.pdf")
                    result.font_size = font_size
                    result.scale = font_size / request.font_size
                    result.lead = lead
                    result.reason = "ok"
                    result.log_excerpt = logs
                    result.debug_ref = {
                        "candidate_id": parent_id,
                        "request_id": request_id,
                    }
                    self._record_selected(
                        request, parent_id, step, request_id=request_id
                    )
                    return result
                reasons.append(f"s{step}:{fit_reason}")
                if fit_reason in ("text-mismatch", "no-extractable-text"):
                    # 内容不一致/无文本不是尺寸问题，缩小字号无法修复。
                    result.reason = ";".join(reasons)
                    result.log_excerpt = logs
                    result.debug_ref = {
                        "candidate_id": parent_id,
                        "request_id": request_id,
                    }
                    return result
            result.reason = ";".join(reasons) or "shrink-exhausted"
            result.log_excerpt = logs
            result.debug_ref = {
                "candidate_id": parent_id,
                "request_id": request_id,
            }
            return result
        finally:
            result.seconds = round(time.perf_counter() - started, 3)
            self._compile_seconds += result.seconds

    def _compile_tex(
        self,
        tex: str,
        workdir: Path,
        stem: str,
        font_size: float,
        *,
        debug_candidate: str | None = None,
    ) -> dict:
        tex_path = workdir / f"{stem}.tex"
        tex_path.write_text(tex, encoding="utf-8")
        argv = [
            self._capability.xelatex_path,
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={workdir}",
            str(tex_path),
        ]
        recorder = self._debug_recorder
        capture = (
            recorder.process(
                "build",
                "xelatex",
                argv,
                timeout=self._timeout,
                candidate=debug_candidate,
            )
            if recorder
            else contextlib.nullcontext()
        )
        try:
            with capture as evidence:
                proc = subprocess.run(  # noqa: S603 - 可执行文件已由能力探测校验
                    argv,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                )
                if evidence is not None:
                    evidence["result"] = proc
        except subprocess.TimeoutExpired:
            logger.warning("LaTeX bbox 编译超时（%.1fs）：丢弃该段落", self._timeout)
            return {"compiled": False, "reason": "timeout", "overfull_hbox": 0, "overfull_vbox": 0, "errors": [f"timeout@{font_size:.1f}pt"]}
        except OSError as exc:
            logger.warning("LaTeX bbox 编译进程失败: %s", exc)
            return {"compiled": False, "reason": f"oserror:{exc}", "overfull_hbox": 0, "overfull_vbox": 0, "errors": []}

        log = proc.stdout or ""
        errors = re.findall(r"^! .*$", log, flags=re.M)
        # 只统计超宽量超过 _WIDTH_TOLERANCE 的 overfull hbox；vbox 出现即失败。
        overfull = _count_overfull_hbox(log, _WIDTH_TOLERANCE)
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

    # ------------------------------------------------------------------
    # 诊断采集（recorder 为 None 时全部为 no-op；不改变编译行为）
    # ------------------------------------------------------------------
    def _record_requests(self, requests: list[StampRequest]) -> str | None:
        """``compile_requests`` 事件：进入渲染器的原始请求清单（含去重前别名）。"""
        recorder = self._debug_recorder
        if not recorder:
            return None
        request_id = recorder.new_id("request")
        recorder.record_event(
            "build",
            "compile_requests",
            {
                "request_id": request_id,
                "renderer": "single",
                "requests": [
                    {
                        "key": request.key,
                        "width": round(float(request.width), 3),
                        "height": round(float(request.height), 3),
                        "font_size": round(float(request.font_size), 3),
                        "lead": (
                            round(float(request.lead), 3) if request.lead else None
                        ),
                        "expected_chars": len(request.expected_text or ""),
                    }
                    for request in requests
                ],
            },
        )
        return request_id

    def _record_reuse(
        self,
        request: StampRequest,
        result: StampResult,
        kind: str,
        request_id: str | None = None,
    ) -> None:
        """``compile_reuse`` 事件：缓存/去重命中，本次真实编译调用数恒为 0。"""
        recorder = self._debug_recorder
        if not recorder:
            return
        debug_ref = getattr(result, "debug_ref", None) or {}
        recorder.record_event(
            "build",
            "compile_reuse",
            {
                "kind": kind,
                "key": request.key,
                "request_id": request_id,
                "candidate_id": debug_ref.get("candidate_id"),
                "actual_compile_calls": 0,
                "pdf_path": result.pdf_path,
                "font_size": result.font_size,
                "scale": result.scale,
                "lead": result.lead,
            },
        )

    def _record_selected(
        self,
        request: StampRequest,
        candidate_id: str | None,
        priority: int,
        *,
        request_id: str | None = None,
    ) -> None:
        """``candidate_selected`` 事件：最终采用的候选（优先级 + 证据 id）。"""
        recorder = self._debug_recorder
        if not recorder or not candidate_id:
            return
        recorder.record_event(
            "build",
            "candidate_selected",
            {
                "id": candidate_id,
                "key": request.key,
                "priority": priority,
                "request_id": request_id,
            },
        )

    def _record_candidate(
        self,
        request: StampRequest,
        *,
        request_id: str | None,
        candidate_id: str | None,
        priority: int,
        font_size: float,
        lead: float,
        parent_id: str | None,
        tex: str,
        attempt_dir: Path,
        stem: str,
        status: str,
        reason: str | None,
        selected: bool = False,
        fit: dict | None = None,
    ) -> str | None:
        """归档候选的 TeX/PDF/日志并发布 ``candidate_evaluated`` 事件。

        返回该候选 id（供父链传递）；recorder 关闭时原样返回 ``parent_id``。
        缺失产物（如超时无 PDF）如实记 ``None``，不伪造。
        """
        recorder = self._debug_recorder
        if not recorder or not candidate_id:
            return parent_id
        safe_key = re.sub(r"[^0-9A-Za-z_-]", "_", str(request.key))[:24]
        artifacts = {
            "tex": recorder.archive_text(
                "build", f"compile/{safe_key}/{candidate_id}.tex", tex
            )
        }
        for suffix in ("pdf", "log"):
            path = attempt_dir / f"{stem}.{suffix}"
            artifacts[suffix] = (
                recorder.archive_file(
                    "build", f"compile/{safe_key}/{candidate_id}.{suffix}", path
                )
                if path.is_file()
                else None
            )
        record = CompileCandidate(
            id=candidate_id,
            paragraph_id=str(request.key),
            request_id=request_id or "",
            renderer="single",
            round=1,
            priority=priority,
            width=round(float(request.width), 3),
            height=round(float(request.height), 3),
            font_size_initial=round(float(request.font_size), 3),
            font_size=round(float(font_size), 3),
            lead=round(float(lead), 3),
            parent_id=parent_id,
            font={"serif": bool(request.serif), "font_family": request.font_family},
            status=status,
            fit=fit or {},
            selected=selected,
            reason=reason,
            artifacts=artifacts,
        )
        payload = record.to_dict()
        snapshot = recorder.write_snapshot(
            "build", f"candidates/{candidate_id}", payload
        )
        recorder.record_event(
            "build", "candidate_evaluated", {**payload, "snapshot": snapshot}
        )
        return candidate_id
