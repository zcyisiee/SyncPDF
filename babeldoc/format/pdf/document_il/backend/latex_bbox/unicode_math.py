"""Unicode 数学字符 → LaTeX 数学模式转写。

对应根因 6：译文里的 ``{vN}`` 时常指向 BabelDOC ``StylesAndFormulas`` 按字符
启发式聚出的 ``PdfFormula``，既不是纯文本也没有 MinerU ``inline_equation`` 源
（如引文号 ``[55]``、项目符号 ``•``、单个数学斜体变量 ``𝑛``）。这类公式无法
回退旧渲染路径（占位符已经在译文里、旧矢量路径没有对应字形盒），必须按原生
字符转写成 LaTeX。

规则（任何未知字符 → 返回 ``None``，由调用方降级为 ``fragment`` 裁片段）：

1. ``U+1D400–U+1D7FF`` 数学字母数字：用 NFKC 兼容分解取基础字母/数字，
   字母按数学模式斜体（``𝑛`` → ``n``）；
2. 希腊字母（含数学斜体希腊 ``𝜏``）→ ``\\tau`` 等命令；
3. ASCII 字母串 → ``\\mathrm{...}``；紧跟数学字母（无空格）时合并为下标
   （``𝑛win`` → ``n_{\\mathrm{win}}``）；
4. 已知运算符表（``×`` ``−`` ``≥`` ``→`` ``√`` …）→ 对应 LaTeX 命令，
   ``√`` 会吞掉紧随其后的原子构成 ``\\sqrt{...}``；
5. 上标/下标字符（``²`` ``ₙ``）合并成 ``^{...}`` / ``_{...}``；
6. 其余 ASCII 数字与安全标点原样（``%`` ``&`` ``,`` 等做 LaTeX 转义）。

本模块只做「能无损转写吗」的判断与转写，不做公式语义推断——不确定即返回
``None``，绝不猜测。
"""

from __future__ import annotations

import re
import unicodedata

#: 源解析器未映射字形的占位串（pymupdf ``(cid:N)``），禁止进入正文/数学转写。
CID_PLACEHOLDER = re.compile(r"\(cid:\d+\)")

#: 数学模式里可原样输出的 ASCII 标点。
_SAFE_PUNCT = set("+-=<>(),.;:|/!?'[]{}*")
#: 需要转义的 ASCII 字符（数学模式里也是特殊字符）。
_ESCAPE = {
    "%": r"\%",
    "&": r"\&",
    "#": r"\#",
    "$": r"\$",
    "_": r"\_",
    "~": r"\sim",
    "^": r"\hat{}",
    "\\": r"\backslash",
}

#: 运算符/关系符/箭头/希腊外常用符号。
_OPERATORS = {
    "×": r"\times",
    "÷": r"\div",
    "±": r"\pm",
    "∓": r"\mp",
    "−": "-",
    "·": r"\cdot",
    "⋅": r"\cdot",
    "∙": r"\cdot",
    "∗": r"\ast",
    "∘": r"\circ",
    "≈": r"\approx",
    "≠": r"\neq",
    "≤": r"\le",
    "≥": r"\ge",
    "≡": r"\equiv",
    "∼": r"\sim",
    "≃": r"\simeq",
    "≅": r"\cong",
    "∝": r"\propto",
    "∈": r"\in",
    "∉": r"\notin",
    "∋": r"\ni",
    "⊂": r"\subset",
    "⊆": r"\subseteq",
    "⊃": r"\supset",
    "⊇": r"\supseteq",
    "∪": r"\cup",
    "∩": r"\cap",
    "∅": r"\emptyset",
    "∧": r"\wedge",
    "∨": r"\vee",
    "¬": r"\neg",
    "∀": r"\forall",
    "∃": r"\exists",
    "∂": r"\partial",
    "∇": r"\nabla",
    "∞": r"\infty",
    "∑": r"\sum",
    "∏": r"\prod",
    "∫": r"\int",
    "→": r"\to",
    "←": r"\leftarrow",
    "↑": r"\uparrow",
    "↓": r"\downarrow",
    "⇒": r"\Rightarrow",
    "⇐": r"\Leftarrow",
    "⇔": r"\Leftrightarrow",
    "⊕": r"\oplus",
    "⊗": r"\otimes",
    "⊥": r"\perp",
    "∥": r"\parallel",
    "⟨": r"\langle",
    "⟩": r"\rangle",
    "⋯": r"\cdots",
    "…": r"\ldots",
    "°": r"{}^{\circ}",
    "′": "'",
    "″": "''",
}

#: 希腊字母 → LaTeX 命令（键为 NFKC 归一化后的字符）。
_GREEK = {
    "α": r"\alpha",
    "β": r"\beta",
    "γ": r"\gamma",
    "δ": r"\delta",
    "ε": r"\epsilon",
    "ζ": r"\zeta",
    "η": r"\eta",
    "θ": r"\theta",
    "ι": r"\iota",
    "κ": r"\kappa",
    "λ": r"\lambda",
    "μ": r"\mu",
    "ν": r"\nu",
    "ξ": r"\xi",
    "π": r"\pi",
    "ρ": r"\rho",
    "ς": r"\varsigma",
    "σ": r"\sigma",
    "τ": r"\tau",
    "υ": r"\upsilon",
    "φ": r"\varphi",
    "χ": r"\chi",
    "ψ": r"\psi",
    "ω": r"\omega",
    "Γ": r"\Gamma",
    "Δ": r"\Delta",
    "Θ": r"\Theta",
    "Λ": r"\Lambda",
    "Ξ": r"\Xi",
    "Π": r"\Pi",
    "Σ": r"\Sigma",
    "Υ": r"\Upsilon",
    "Φ": r"\Phi",
    "Ψ": r"\Psi",
    "Ω": r"\Omega",
}

#: 上标字符 → 指数内容。
_SUPERSCRIPTS = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "⁺": "+", "⁻": "-", "⁼": "=", "⁽": "(", "⁾": ")",
    "ⁿ": "n", "ⁱ": "i",
}
#: 下标字符 → 下标内容。
_SUBSCRIPTS = {
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
    "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
    "₊": "+", "₋": "-", "₌": "=", "₍": "(", "₎": ")",
    "ₐ": "a", "ₑ": "e", "ₒ": "o", "ₓ": "x", "ₕ": "h",
    "ₖ": "k", "ₗ": "l", "ₘ": "m", "ₙ": "n", "ₚ": "p",
    "ₛ": "s", "ₜ": "t",
}

#: 数学字母数字块（NFKC 兼容分解后是 ASCII 字母/数字）。
_MATH_ALNUM_RANGE = (0x1D400, 0x1D7FF)

_ATOM = re.compile(r"[0-9A-Za-z]+")


def _math_base(ch: str) -> str | None:
    """数学字母数字字符 → 基础 ASCII 字母/数字；不是该块时返回 None。"""
    if not (_MATH_ALNUM_RANGE[0] <= ord(ch) <= _MATH_ALNUM_RANGE[1]):
        return None
    base = unicodedata.normalize("NFKC", ch)
    if len(base) == 1 and base.isascii() and base.isalnum():
        return base
    return None


def _normalized(ch: str) -> str:
    """NFKC 归一化（数学斜体希腊 → 普通希腊；兼容字符 → 标准字符）。"""
    return unicodedata.normalize("NFKC", ch)


def _take_atom(text: str, start: int) -> tuple[str, int]:
    """从 ``start`` 起取一个原子（字母数字串或花括号组），返回 (LaTeX, 新下标)。"""
    match = _ATOM.match(text, start)
    if match:
        return match.group(0), match.end()
    return "", start


def _merge_scripts(
    text: str, start: int, table: dict[str, str], marker: str
) -> tuple[str, int]:
    """合并连续的上标/下标字符为 ``^{...}``。"""
    collected: list[str] = []
    index = start
    while index < len(text) and text[index] in table:
        collected.append(table[text[index]])
        index += 1
    return marker + "{" + "".join(collected) + "}", index


def math_latex(native: str) -> str | None:
    """把原生字符转写成 LaTeX 数学模式片段（不含 ``$``）。

    含未知字符、``(cid:N)`` 占位串、或转写结果为空时返回 ``None``
    （调用方降级为 fragment）。
    """
    if CID_PLACEHOLDER.search(native):
        return None
    out: list[str] = []
    index = 0
    prev_math_atom = False
    while index < len(native):
        ch = native[index]
        if ch.isspace():
            out.append(" ")
            prev_math_atom = False
            index += 1
            continue
        if ch == "√":
            atom, index = _take_atom(native, index + 1)
            out.append(r"\sqrt{" + atom + "} ")
            prev_math_atom = False
            continue
        if ch in _SUPERSCRIPTS:
            merged, index = _merge_scripts(native, index, _SUPERSCRIPTS, "^")
            out.append(merged)
            prev_math_atom = False
            continue
        if ch in _SUBSCRIPTS:
            merged, index = _merge_scripts(native, index, _SUBSCRIPTS, "_")
            out.append(merged)
            prev_math_atom = False
            continue
        base = _math_base(ch)
        if base is not None:
            out.append(base)
            prev_math_atom = base.isalpha()
            index += 1
            continue
        if ch.isascii():
            if ch.isalpha():
                match = _ATOM.match(native, index)
                word = match.group(0)
                if prev_math_atom:
                    # 紧跟数学字母的 ASCII 字母串是下标：𝑛win → n_{\mathrm{win}}
                    out.append(r"_{\mathrm{" + word + "}}")
                else:
                    out.append(r"\mathrm{" + word + "}")
                prev_math_atom = False
                index = match.end()
                continue
            if ch.isdigit():
                out.append(ch)
                prev_math_atom = False
                index += 1
                continue
            if ch in _ESCAPE:
                out.append(_ESCAPE[ch])
                prev_math_atom = False
                index += 1
                continue
            if ch in _SAFE_PUNCT:
                out.append(ch)
                prev_math_atom = False
                index += 1
                continue
            return None
        normalized = _normalized(ch)
        if normalized in _GREEK:
            # 命令后补空格：``\Deltab`` 会被 TeX 当成单个控制序列。
            out.append(_GREEK[normalized] + " ")
            prev_math_atom = True
            index += 1
            continue
        if ch in _OPERATORS:
            out.append(_OPERATORS[ch] + " ")
            prev_math_atom = False
            index += 1
            continue
        if normalized in _OPERATORS:
            out.append(_OPERATORS[normalized] + " ")
            prev_math_atom = False
            index += 1
            continue
        return None
    # 命令后的分隔空格在数学模式里无视觉效果，这里收敛成单个空格。
    body = re.sub(r"\s+", " ", "".join(out)).strip()
    body = body.replace(" {", " {")
    return body or None
