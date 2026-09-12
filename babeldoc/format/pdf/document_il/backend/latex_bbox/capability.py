"""LaTeX bbox 排版能力探测。

探测 XeLaTeX 可执行文件、必需宏包、中文字体与 PyMuPDF 是否可用。
能力缺失**只产生明确 warning 并回退现有渲染路径**，绝不抛异常阻断翻译
（对应架构原则 1）。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

logger = logging.getLogger(__name__)

#: xelatex 必需宏包（tex 文件 \usepackage 依赖）。
REQUIRED_PACKAGES = (
    "xeCJK",
    "geometry",
    "fontspec",
    "amsmath",
    "amssymb",
    "graphicx",
    "babel",
    "url",
)

#: 常见安装路径（macOS TeX Live、Linux 发行版）。
_COMMON_XELATEX_PATHS = (
    "/Library/TeX/texbin/xelatex",
    "/usr/local/bin/xelatex",
    "/usr/bin/xelatex",
)

#: BabelDOC 字体缓存目录（assets 释放位置）。
_FONT_CACHE_DIR = Path.home() / ".cache" / "babeldoc" / "fonts"

#: 主字体候选：{primary_font_family: (regular, bold)}。
_CJK_FONT_CANDIDATES = {
    "sans-serif": (
        "SourceHanSansCN-Regular.ttf",
        "SourceHanSansCN-Bold.ttf",
    ),
    None: (
        "SourceHanSansCN-Regular.ttf",
        "SourceHanSansCN-Bold.ttf",
    ),
    "serif": (
        "SourceHanSerifCN-Regular.ttf",
        "SourceHanSerifCN-Bold.ttf",
    ),
    "script": (
        "SourceHanSerifCN-Regular.ttf",
        "SourceHanSerifCN-Bold.ttf",
    ),
}

#: 中文正文字体（与产品 CN_FONT_FAMILY 一致）：serif / sans 两套。
_CJK_FAMILY_FILES = {
    "serif": (
        "SourceHanSerifCN-Regular.ttf",
        "SourceHanSerifCN-Bold.ttf",
    ),
    "sans": (
        "SourceHanSansCN-Regular.ttf",
        "SourceHanSansCN-Bold.ttf",
    ),
}

#: 拉丁字体（与产品 CN_FONT_FAMILY.normal 一致）：serif=Noto Serif，sans=Noto Sans。
_LATIN_FAMILY_FILES = {
    "serif": {
        "regular": "NotoSerif-Regular.ttf",
        "bold": "NotoSerif-Bold.ttf",
        "italic": "NotoSerif-Italic.ttf",
        "bolditalic": "NotoSerif-BoldItalic.ttf",
    },
    "sans": {
        "regular": "NotoSans-Regular.ttf",
        "bold": "NotoSans-Bold.ttf",
        "italic": "NotoSans-Italic.ttf",
        "bolditalic": "NotoSans-BoldItalic.ttf",
    },
}


@dataclass(slots=True)
class LatexCapability:
    """LaTeX bbox 排版能力探测结果。"""

    available: bool = False
    xelatex_path: str | None = None
    font_path: str | None = None
    bold_font_path: str | None = None
    missing_packages: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    pymupdf_ok: bool = True
    #: 拉丁字体四体（regular/bold/italic/bolditalic）；探测不到时为 None，
    #: 模板退回 Latin Modern（只 warning，不阻断）。
    latin_serif_fonts: dict[str, str] | None = None
    latin_sans_fonts: dict[str, str] | None = None
    #: 中文正文两套（serif/sans，各含 regular/bold）。
    cjk_serif_fonts: dict[str, str] | None = None
    cjk_sans_fonts: dict[str, str] | None = None
    #: 非致命提示（例如拉丁字体缺失）。
    notes: list[str] = field(default_factory=list)
    #: 中文字体是否由调用方显式指定（显式时只设中文主字体，不用候选族）。
    font_explicit: bool = False

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "xelatex_path": self.xelatex_path,
            "font_path": self.font_path,
            "bold_font_path": self.bold_font_path,
            "missing_packages": list(self.missing_packages),
            "reasons": list(self.reasons),
            "pymupdf_ok": self.pymupdf_ok,
            "latin_serif_fonts": self.latin_serif_fonts,
            "latin_sans_fonts": self.latin_sans_fonts,
            "cjk_serif_fonts": self.cjk_serif_fonts,
            "cjk_sans_fonts": self.cjk_sans_fonts,
            "notes": list(self.notes),
            "font_explicit": self.font_explicit,
        }

    def latin_fonts(self, serif: bool) -> dict[str, str] | None:
        """按 serif 标志取拉丁四体（缺失返回 None）。"""
        return self.latin_serif_fonts if serif else self.latin_sans_fonts

    def cjk_fonts(self, serif: bool) -> dict[str, str] | None:
        """按 serif 标志取中文 regular/bold（缺失返回 None）。"""
        return self.cjk_serif_fonts if serif else self.cjk_sans_fonts


def _find_xelatex(explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).is_file() else None
    found = shutil.which("xelatex")
    if found:
        return found
    for candidate in _COMMON_XELATEX_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def _find_cjk_font(explicit: str | None, primary_font_family: str | None):
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            return None, None
        bold = _guess_bold_sibling(path)
        return str(path), (str(bold) if bold else None)
    regular_name, bold_name = _CJK_FONT_CANDIDATES.get(
        primary_font_family, _CJK_FONT_CANDIDATES[None]
    )
    regular = _FONT_CACHE_DIR / regular_name
    if not regular.is_file():
        return None, None
    bold = _FONT_CACHE_DIR / bold_name
    return str(regular), (str(bold) if bold.is_file() else None)


def _guess_bold_sibling(path: Path) -> Path | None:
    bold = path.with_name(path.name.replace("Regular", "Bold"))
    return bold if bold.is_file() else None


def _font_dirs(explicit_font_path: str | None) -> list[Path]:
    """字体查找目录：显式字体所在目录优先，再看 BabelDOC 字体缓存目录。"""
    dirs: list[Path] = []
    if explicit_font_path:
        parent = Path(explicit_font_path).parent
        if parent.is_dir():
            dirs.append(parent)
    if _FONT_CACHE_DIR not in dirs:
        dirs.append(_FONT_CACHE_DIR)
    return dirs


def _find_face(dirs: list[Path], file_name: str) -> Path | None:
    for directory in dirs:
        candidate = directory / file_name
        if candidate.is_file():
            return candidate
    return None


def _latin_face_files(dirs: list[Path], styles: dict[str, str]) -> dict[str, str] | None:
    """四个字形都齐才算命中（缺一则整组退回默认）。"""
    found: dict[str, str] = {}
    for key, file_name in styles.items():
        path = _find_face(dirs, file_name)
        if path is None:
            return None
        found[key] = str(path)
    return found


def _cjk_face_files(dirs: list[Path], names: tuple[str, str]) -> dict[str, str] | None:
    regular, bold = (_find_face(dirs, name) for name in names)
    if regular is None:
        return None
    result = {"regular": str(regular)}
    if bold is not None:
        result["bold"] = str(bold)
    return result


def _missing_packages(xelatex_path: str) -> list[str]:
    kpsewhich = Path(xelatex_path).parent / "kpsewhich"
    if not kpsewhich.is_file():
        # 无法验证宏包时保守认为缺失（例如极简发行版）。
        return list(REQUIRED_PACKAGES)
    missing = []
    for package in REQUIRED_PACKAGES:
        try:
            proc = subprocess.run(  # noqa: S603 - 固定 kpsewhich 参数，无外部输入
                [str(kpsewhich), f"{package}.sty"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return list(REQUIRED_PACKAGES)
        if proc.returncode != 0 or not proc.stdout.strip():
            missing.append(package)
    return missing


def probe_latex_capability(
    xelatex_path: str | None = None,
    font_path: str | None = None,
    primary_font_family: str | None = None,
) -> LatexCapability:
    """探测 XeLaTeX/宏包/字体能力。永不抛异常，只返回结构化结果。"""
    capability = LatexCapability()
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        capability.pymupdf_ok = False
        capability.reasons.append("pymupdf 不可用")

    capability.xelatex_path = _find_xelatex(xelatex_path)
    if capability.xelatex_path is None:
        capability.reasons.append("未找到 xelatex 可执行文件")

    capability.font_path, capability.bold_font_path = _find_cjk_font(
        font_path, primary_font_family
    )
    capability.font_explicit = bool(font_path) and capability.font_path is not None
    if capability.font_path is None:
        capability.reasons.append(
            f"未找到中文字体（查找 {_FONT_CACHE_DIR}，可用 --latex-cjk-font-path 指定）"
        )

    # 拉丁/中文两套字体：与产品一致（拉丁 Noto Serif/Sans，中文 Source Han
    # Serif/Sans CN）。拉丁字体缺失只 warning（模板退回 Latin Modern）。
    font_dirs = _font_dirs(font_path)
    capability.latin_serif_fonts = _latin_face_files(
        font_dirs, _LATIN_FAMILY_FILES["serif"]
    )
    capability.latin_sans_fonts = _latin_face_files(
        font_dirs, _LATIN_FAMILY_FILES["sans"]
    )
    capability.cjk_serif_fonts = _cjk_face_files(font_dirs, _CJK_FAMILY_FILES["serif"])
    capability.cjk_sans_fonts = _cjk_face_files(font_dirs, _CJK_FAMILY_FILES["sans"])
    if capability.latin_serif_fonts is None and capability.latin_sans_fonts is None:
        capability.notes.append(
            f"未找到拉丁字体（查找 {', '.join(str(d) for d in font_dirs)}），"
            "拉丁字形退回 Latin Modern"
        )
        logger.warning("LaTeX bbox 未找到产品拉丁字体，拉丁字形退回 Latin Modern")

    if capability.xelatex_path is not None:
        capability.missing_packages = _missing_packages(capability.xelatex_path)
        if capability.missing_packages:
            capability.reasons.append(
                f"缺少 LaTeX 宏包: {', '.join(capability.missing_packages)}"
            )

    capability.available = not (
        capability.reasons
        or capability.missing_packages
        or not capability.pymupdf_ok
        or capability.xelatex_path is None
        or capability.font_path is None
    )
    if not capability.available:
        logger.warning(
            "LaTeX bbox 排版能力不可用，回退现有渲染路径: %s",
            "; ".join(capability.reasons) or "未知原因",
        )
    return capability
