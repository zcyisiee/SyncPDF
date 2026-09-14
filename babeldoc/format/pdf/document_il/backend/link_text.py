"""Glyph geometry and equivalent reference labels shared by mapping and audit."""

import re
import unicodedata

import pymupdf


def reference_role(name):
    name = str(name or "").lower()
    for prefix, role in (
        ("cite.", "citation"),
        ("figure.", "figure"),
        ("table.", "table"),
        ("equation.", "equation"),
        ("theorem.", "theorem"),
        ("hfootnote.", "footnote"),
        ("footnote.", "footnote"),
        ("appendix.", "appendix"),
    ):
        if name.startswith(prefix):
            return role
    return None


def role_matches(role, text, before, after):
    """Distinguish Fig. 1, footnote 1, Eq. (1), and citation [1]."""
    text_norm = normalize_anchor(text)
    before_norm = normalize_anchor(before)
    after_norm = normalize_anchor(after)

    text_clean = "".join(text_norm.split())
    before_clean = "".join(before_norm.split())
    after_clean = "".join(after_norm.split())

    if role == "citation":
        return bool(
            re.search(r"\[[^\]]*$", before_clean[-40:])
            and "]" in (text_clean + after_clean[:40])
            or text_clean.startswith("[")
            and "]" in text_clean + after_clean[:40]
        )
    prefixes = {
        "figure": (r"(?:Figure|Fig\.?)", r"图"),
        "table": (r"(?:Table|Tab\.?)", r"表"),
        "equation": (r"(?:Equation|Eq\.?)", r"式"),
        "theorem": (r"(?:Theorem)", r"定理"),
        "appendix": (r"(?:Appendix)", r"附录"),
    }
    if role in prefixes:
        en_pref, zh_pref = prefixes[role]
        label_inner = r"(?:[IVXLCDM]+|\d+(?:[\.\-]\d+)*[A-Za-z]?|[A-Za-z](?:[\.\-]\d+)?|[一二三四五六七八九十]+)"
        label_pat = rf"(?:[（(]\s*{label_inner}\s*[）)]?|{label_inner}[）)]?)"
        context = (before_norm[-40:] + text_norm).rstrip()
        en_regex = rf"\b{en_pref}(?:\.|\s+|(?=[（(\[\d]))\s*{label_pat}$"
        zh_regex = rf"{zh_pref}\s*{label_pat}$"
        return bool(
            re.search(en_regex, context, re.IGNORECASE)
            or re.search(zh_regex, context, re.IGNORECASE)
        )
    if role == "footnote":
        # A translated footnote marker may follow Chinese, punctuation, or Latin.
        # Exclude explicit reference syntax; source glyph identity / elevation
        # and occurrence geometry provide its remaining placement evidence.
        return not any(
            role_matches(other, text, before, after)
            for other in ("figure", "table", "citation", "equation")
        )
    return True


def rect_context(raw, rect):
    chars = [
        (char["c"], pymupdf.Rect(char["bbox"]))
        for block in raw.get("blocks", [])
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        for char in span.get("chars", [])
    ]
    indices = [
        i
        for i, (_, box) in enumerate(chars)
        if box.get_area() > 0
        and (box & rect).get_area() / box.get_area() >= 0.3
        and rect.x0 <= (box.x0 + box.x1) / 2 <= rect.x1
    ]
    if not indices:
        return "", ""
    return (
        "".join(c for c, _ in chars[max(0, indices[0] - 40) : indices[0]]),
        "".join(c for c, _ in chars[indices[-1] + 1 : indices[-1] + 41]),
    )


def covered_text(page, rect, raw=None):
    """Read individual glyphs, tolerating annotation padding and short ascenders."""
    raw = raw if raw is not None else page.get_text("rawdict")
    lines = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            chars = []
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    box = pymupdf.Rect(char["bbox"])
                    overlap = box & rect
                    if (
                        not overlap.is_empty
                        and box.get_area() > 0
                        and overlap.get_area() / box.get_area() >= 0.3
                        and rect.x0 <= (box.x0 + box.x1) / 2 <= rect.x1
                    ):
                        chars.append(char["c"])
            if chars:
                lines.append("".join(chars))
    return "\n".join(lines).strip()


def normalize_anchor(text):
    return unicodedata.normalize("NFKC", text).translate(
        str.maketrans({"—": "-", "–": "-", "−": "-", "‑": "-", "‐": "-"})
    )


def anchor_variants(anchor):
    """Equivalent labels for translated reference prefixes and Roman headings.

    Keep the complete identifier (VI-A3, 3.1), never match its final digit alone.
    """
    anchor = "".join(normalize_anchor(anchor).split())
    variants = [anchor]
    for pattern, prefix in (
        (r"(?:Figure|Fig\.?)(.+)", "图"),
        (r"(?:Table|Tab\.?)(.+)", "表"),
        (r"(?:Equation|Eq\.?)(.+)", "式"),
        (r"(?:Section|Sec\.?)(.+)", "第"),
    ):
        match = re.fullmatch(pattern, anchor, re.IGNORECASE)
        if match:
            variants.append(prefix + match[1])
    roman = re.fullmatch(r"([IVXLCDM]+)(-[A-Z](?:\d+)?)?", anchor)
    if roman:
        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        number = sum(
            -values[c]
            if i + 1 < len(roman[1]) and values[c] < values[roman[1][i + 1]]
            else values[c]
            for i, c in enumerate(roman[1])
        )
        if 0 < number < 100:
            digits = "零一二三四五六七八九"
            chinese = (
                digits[number]
                if number < 10
                else (digits[number // 10] if number >= 20 else "")
                + "十"
                + (digits[number % 10] if number % 10 else "")
            )
            variants.append(chinese + (roman[2] or ""))
            variants.append(str(number) + (roman[2] or ""))
    return list(dict.fromkeys(variants))
