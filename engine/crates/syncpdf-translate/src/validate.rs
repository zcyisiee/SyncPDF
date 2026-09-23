//! 译文结构与内容完整性校验器。
//!
//! 多 id 拒（`duplicated_text_slots` / `unknown_paragraph`）、占位符多重集相等而
//! 顺序自由。样式只校验引用可解析，不强制片段数量/非空/源位置；不检查目标语比例。
//!
//! 所有检查都是确定性的纯函数，不联网、不看日志、不改输入。

use std::collections::BTreeSet;

use crate::unit::{parse_unit_html, ParsedUnit, Unit};
use syncpdf_core::{AtomId, StyleId};

/// 译文 / 原文字符数比的默认上限（§5.3）。
pub const DEFAULT_EXPANSION_LIMIT: f32 = 3.0;

/// 低于这个字符数的源段落不做长度膨胀检查（短段噪声太大）。
const MIN_SOURCE_CHARS_FOR_EXPANSION: usize = 8;

/// 邻段泄漏检测的滑窗长度与步长（字符）。
const CONTEXT_WINDOW: usize = 16;
const CONTEXT_STEP: usize = 8;

/// 病理重复：周期长度上限、最小覆盖字符数、最小重复次数。
const REPEAT_MAX_PERIOD: usize = 16;
const REPEAT_MIN_SPAN: usize = 12;
const REPEAT_MIN_TIMES: u32 = 4;

/// 校验违规码。`code()` 是 snake_case 字面量，直接进 `issue` 事件与报告。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Violation {
    /// 非法标签 / 未闭合 / 结构（含 `<br>` 数）与源文不符。
    InvalidMarkup,
    /// `{{KEEP_n}}` 多重集不等。
    PlaceholderCount,
    /// 出现源文没有的占位符。
    UnknownPlaceholder,
    /// `ATOM_HINTS` 的文本被抄进译文。
    AtomHintLeakage,
    /// 源文里的 URL / 邮箱字面量在译文里丢了或被改了。
    LinkLabelChanged,
    /// 出现源文没有的 `data-style`（无法解析到字体/字号元数据）。
    UnknownStyle,
    /// 含 U+202A–202E 等双向嵌入/覆盖控制符。
    UnsafeBidiControl,
    /// FSI/LRI/RLI 与 PDI 不配对。
    UnbalancedBidiIsolate,
    /// 同一段 id 在回包里出现两次。
    DuplicatedTextSlots,
    /// `CONTEXT_BEFORE` / `CONTEXT_AFTER` 的文本进了译文。
    NeighborContextLeakage,
    /// 重复 n-gram 超阈值（模型退化）。
    PathologicalTextRepetition,
    /// 数字等受保护字面量数量变化。
    ProtectedLiteralCount,
    /// 译文 / 原文长度比超过 `expansion_limit`。
    ExcessiveTargetExpansion,
    /// 目标语言非日文却残留假名。
    ResidualJapanese,
    /// 回包里的段 id 不是本单元的 id（段外 id）。
    UnknownParagraph,
}

impl Violation {
    /// snake_case 错误码（与 hjfy 一致）。
    pub fn code(&self) -> &'static str {
        match self {
            Violation::InvalidMarkup => "invalid_markup",
            Violation::PlaceholderCount => "placeholder_count",
            Violation::UnknownPlaceholder => "unknown_placeholder",
            Violation::AtomHintLeakage => "atom_hint_leakage",
            Violation::LinkLabelChanged => "link_label_changed",
            Violation::UnknownStyle => "unknown_style",
            Violation::UnsafeBidiControl => "unsafe_bidi_control",
            Violation::UnbalancedBidiIsolate => "unbalanced_bidi_isolate",
            Violation::DuplicatedTextSlots => "duplicated_text_slots",
            Violation::NeighborContextLeakage => "neighbor_context_leakage",
            Violation::PathologicalTextRepetition => "pathological_text_repetition",
            Violation::ProtectedLiteralCount => "protected_literal_count",
            Violation::ExcessiveTargetExpansion => "excessive_target_expansion",
            Violation::ResidualJapanese => "residual_japanese",
            Violation::UnknownParagraph => "unknown_paragraph",
        }
    }

    /// 当前全部违规码，供上游枚举统计。
    pub fn all() -> &'static [Violation] {
        use Violation::*;
        &[
            InvalidMarkup,
            PlaceholderCount,
            UnknownPlaceholder,
            AtomHintLeakage,
            LinkLabelChanged,
            UnknownStyle,
            UnsafeBidiControl,
            UnbalancedBidiIsolate,
            DuplicatedTextSlots,
            NeighborContextLeakage,
            PathologicalTextRepetition,
            ProtectedLiteralCount,
            ExcessiveTargetExpansion,
            ResidualJapanese,
            UnknownParagraph,
        ]
    }
}

impl std::fmt::Display for Violation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.code())
    }
}

/// 校验一个块所需的全部上下文。
#[derive(Debug, Clone)]
pub struct ValidateCtx<'a> {
    pub source: &'a Unit,
    pub target_lang: &'a str,
    pub context_before: &'a str,
    pub context_after: &'a str,
    pub atom_hints: &'a [String],
    pub expansion_limit: f32,
}

impl<'a> ValidateCtx<'a> {
    /// 最小上下文：无邻段、无原子提示、默认膨胀上限。
    pub fn new(source: &'a Unit, target_lang: &'a str) -> Self {
        Self {
            source,
            target_lang,
            context_before: "",
            context_after: "",
            atom_hints: &[],
            expansion_limit: DEFAULT_EXPANSION_LIMIT,
        }
    }
}

/// 校验模型回包。通过则返回解析结果，否则返回全部违规码（去重、按枚举序）。
pub fn validate(
    ctx: &ValidateCtx<'_>,
    translated_html: &str,
) -> Result<ParsedUnit, Vec<Violation>> {
    let mut v: BTreeSet<Violation> = BTreeSet::new();

    // ── 0. 先看有几个 `<p id>`：多 id / 段外 id 在解析前就要判掉 ──────────
    let ids = scan_paragraph_ids(translated_html);
    let want = ctx.source.id.to_string();
    if ids.is_empty() {
        return Err(vec![Violation::InvalidMarkup]);
    }
    if has_duplicate(&ids) {
        v.insert(Violation::DuplicatedTextSlots);
    }
    if ids.iter().any(|i| *i != want) {
        v.insert(Violation::UnknownParagraph);
    }
    if ids.len() > 1 {
        // 多块回包无法当作单元解析，先把结构问题报回去。
        if v.is_empty() {
            v.insert(Violation::DuplicatedTextSlots);
        }
        return Err(v.into_iter().collect());
    }

    // ── 1. 受限 HTML 解析 ────────────────────────────────────────────────
    let Ok(target) = parse_unit_html(translated_html) else {
        v.insert(Violation::InvalidMarkup);
        return Err(v.into_iter().collect());
    };
    // 源文必定合法（`build_unit` 自产），解析不出来就是上游给了坏 Unit。
    let Ok(source) = parse_unit_html(&ctx.source.html) else {
        return Err(vec![Violation::InvalidMarkup]);
    };
    if target.id != source.id {
        v.insert(Violation::UnknownParagraph);
    }

    let src_text = source.text();
    let tgt_text = target.text();

    // ── 2. 占位符（多重集相等，顺序自由）──────────────────────────────────
    let mut src_atoms = source.atom_ids();
    let mut tgt_atoms = target.atom_ids();
    src_atoms.sort_unstable();
    tgt_atoms.sort_unstable();
    if src_atoms != tgt_atoms {
        v.insert(Violation::PlaceholderCount);
    }
    let src_atom_set: BTreeSet<AtomId> = src_atoms.iter().copied().collect();
    if tgt_atoms.iter().any(|a| !src_atom_set.contains(a)) {
        v.insert(Violation::UnknownPlaceholder);
    }

    // ── 3. 原子提示泄漏 ─────────────────────────────────────────────────
    for hint in ctx.atom_hints {
        let h = hint.trim();
        if h.chars().count() >= 3 && tgt_text.contains(h) && !src_text.contains(h) {
            v.insert(Violation::AtomHintLeakage);
            break;
        }
    }

    // ── 4. 链接 / 邮箱字面量必须逐字保留 ─────────────────────────────────
    if link_literals(&src_text)
        .iter()
        .any(|l| !tgt_text.contains(l.as_str()))
    {
        v.insert(Violation::LinkLabelChanged);
    }

    // ── 5. 样式引用必须可解析；模型可重排、拆合、复用或省略已知样式 ──────
    let src_style_set: BTreeSet<StyleId> = source.style_ids().into_iter().collect();
    if target
        .style_ids()
        .iter()
        .any(|s| !src_style_set.contains(s))
    {
        v.insert(Violation::UnknownStyle);
    }

    // ── 7. 双向控制符 ───────────────────────────────────────────────────
    if tgt_text.chars().any(is_unsafe_bidi) {
        v.insert(Violation::UnsafeBidiControl);
    }
    if !bidi_isolates_balanced(&tgt_text) {
        v.insert(Violation::UnbalancedBidiIsolate);
    }

    // ── 8. 硬换行数量（协议要求相等；无独立码，归 invalid_markup）────────
    if source.break_count() != target.break_count() {
        v.insert(Violation::InvalidMarkup);
    }

    // ── 9. 邻段上下文泄漏 ───────────────────────────────────────────────
    if leaks_context(ctx.context_before, &src_text, &tgt_text)
        || leaks_context(ctx.context_after, &src_text, &tgt_text)
    {
        v.insert(Violation::NeighborContextLeakage);
    }

    // ── 10. 病理重复 ────────────────────────────────────────────────────
    let tgt_rep = max_immediate_repeat(&tgt_text);
    if tgt_rep >= REPEAT_MIN_TIMES && tgt_rep > max_immediate_repeat(&src_text) {
        v.insert(Violation::PathologicalTextRepetition);
    }

    // ── 11. 受保护字面量（数字）数量 ─────────────────────────────────────
    let mut src_lit = number_literals(&src_text);
    let mut tgt_lit = number_literals(&tgt_text);
    src_lit.sort();
    tgt_lit.sort();
    if src_lit != tgt_lit {
        v.insert(Violation::ProtectedLiteralCount);
    }

    // ── 12. 膨胀比 ──────────────────────────────────────────────────────
    let src_chars = src_text.chars().count();
    let tgt_chars = tgt_text.chars().count();
    if src_chars >= MIN_SOURCE_CHARS_FOR_EXPANSION
        && ctx.expansion_limit > 0.0
        && tgt_chars as f32 > src_chars as f32 * ctx.expansion_limit
    {
        v.insert(Violation::ExcessiveTargetExpansion);
    }

    // ── 14. 残留假名 ────────────────────────────────────────────────────
    if primary_lang(ctx.target_lang) != "ja" && tgt_text.chars().any(is_kana) {
        v.insert(Violation::ResidualJapanese);
    }

    if v.is_empty() {
        Ok(target)
    } else {
        Err(v.into_iter().collect())
    }
}

// ───────────────────────────── 辅助判定 ─────────────────────────────────

/// 扫出回包里所有 `<p id="…">` 的 id 字面量（不校验合法性）。
fn scan_paragraph_ids(html: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = html;
    while let Some(i) = rest.find("<p") {
        rest = &rest[i + 2..];
        let Some(gt) = rest.find('>') else { break };
        let tag = &rest[..gt];
        if let Some(id) = tag
            .trim_start()
            .strip_prefix("id=\"")
            .and_then(|a| a.split('"').next())
        {
            out.push(id.to_string());
        }
        rest = &rest[gt + 1..];
    }
    out
}

fn has_duplicate(ids: &[String]) -> bool {
    let set: BTreeSet<&String> = ids.iter().collect();
    set.len() != ids.len()
}

/// 源文里必须逐字保留的字面量：URL 与邮箱。
fn link_literals(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    for raw in text.split_whitespace() {
        let tok = raw.trim_matches(|c: char| {
            matches!(
                c,
                '.' | ',' | ';' | ':' | ')' | '(' | '[' | ']' | '{' | '}' | '"' | '\''
            )
        });
        if tok.len() < 5 {
            continue;
        }
        let is_url =
            tok.starts_with("http://") || tok.starts_with("https://") || tok.starts_with("www.");
        let is_mail = {
            let mut parts = tok.splitn(2, '@');
            match (parts.next(), parts.next()) {
                (Some(a), Some(b)) => !a.is_empty() && b.contains('.') && !b.contains('@'),
                _ => false,
            }
        };
        if is_url || is_mail {
            out.push(tok.to_string());
        }
    }
    out
}

/// U+202A–202E：LRE/RLE/PDF/LRO/RLO，受限协议一律禁止。
fn is_unsafe_bidi(c: char) -> bool {
    matches!(c, '\u{202A}'..='\u{202E}')
}

/// FSI/LRI/RLI 必须与 PDI 一一配对。
fn bidi_isolates_balanced(text: &str) -> bool {
    let mut depth = 0i32;
    for c in text.chars() {
        match c {
            '\u{2066}' | '\u{2067}' | '\u{2068}' => depth += 1,
            '\u{2069}' => {
                depth -= 1;
                if depth < 0 {
                    return false;
                }
            }
            _ => {}
        }
    }
    depth == 0
}

fn is_kana(c: char) -> bool {
    // 平假名与片假名本体；不含长音符 ー(U+30FC) 与中点 ・(U+30FB)。
    matches!(c, '\u{3041}'..='\u{3096}' | '\u{309D}'..='\u{309F}' | '\u{30A1}'..='\u{30FA}' | '\u{30FD}'..='\u{30FF}')
}

/// BCP-47 的主语言子标签（小写）。
fn primary_lang(lang: &str) -> String {
    lang.split(['-', '_'])
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase()
}

/// 邻段上下文是否漏进了译文：用滑窗在译文里找上下文片段，且该片段不在源文里。
fn leaks_context(context: &str, src_text: &str, tgt_text: &str) -> bool {
    let ctx: Vec<char> = context.trim().chars().collect();
    if ctx.len() < CONTEXT_WINDOW {
        return false;
    }
    let mut i = 0;
    while i + CONTEXT_WINDOW <= ctx.len() {
        let w: String = ctx[i..i + CONTEXT_WINDOW].iter().collect();
        if w.trim().chars().count() >= CONTEXT_WINDOW / 2
            && tgt_text.contains(&w)
            && !src_text.contains(&w)
        {
            return true;
        }
        i += CONTEXT_STEP;
    }
    false
}

/// 最长「连续自我重复」的重复次数，用于识别模型退化输出。
///
/// 只统计覆盖至少 `REPEAT_MIN_SPAN` 个字符的重复，避免把 `aa`、`——` 之类误判。
fn max_immediate_repeat(text: &str) -> u32 {
    let c: Vec<char> = text.chars().collect();
    let n = c.len();
    let mut best = 0u32;
    for i in 0..n {
        for p in 1..=REPEAT_MAX_PERIOD.min(n.saturating_sub(i)) {
            let mut k = 1usize;
            while i + (k + 1) * p <= n && c[i..i + p] == c[i + k * p..i + (k + 1) * p] {
                k += 1;
            }
            if k * p >= REPEAT_MIN_SPAN {
                best = best.max(k as u32);
            }
        }
    }
    best
}

/// 受保护字面量：数字串（允许内部 `.`/`,` 分隔）。
fn number_literals(text: &str) -> Vec<String> {
    let c: Vec<char> = text.chars().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < c.len() {
        if !c[i].is_ascii_digit() {
            i += 1;
            continue;
        }
        let start = i;
        while i < c.len() {
            if c[i].is_ascii_digit() {
                i += 1;
            } else if matches!(c[i], '.' | ',') && i + 1 < c.len() && c[i + 1].is_ascii_digit() {
                i += 2;
            } else {
                break;
            }
        }
        out.push(c[start..i].iter().collect());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::unit::build_unit;
    use crate::unit::tests::paragraph_of;

    /// 造一个纯文本源单元（无 span、无硬换行）。
    fn src_unit(id: &str, text: &str) -> Unit {
        let mut p = paragraph_of(id, &[]);
        p.text = text.into();
        build_unit(&p, |_| None)
    }

    fn ctx<'a>(u: &'a Unit, lang: &'a str) -> ValidateCtx<'a> {
        ValidateCtx::new(u, lang)
    }

    fn codes(r: Result<ParsedUnit, Vec<Violation>>) -> Vec<&'static str> {
        r.err()
            .unwrap_or_default()
            .iter()
            .map(|v| v.code())
            .collect()
    }

    #[test]
    fn all_codes_are_unique_snake_case() {
        let all = Violation::all();
        assert_eq!(all.len(), 15);
        let set: BTreeSet<&str> = all.iter().map(|v| v.code()).collect();
        assert_eq!(set.len(), all.len());
        for c in set {
            assert!(
                c.chars().all(|ch| ch.is_ascii_lowercase() || ch == '_'),
                "{c} is not snake_case"
            );
        }
    }

    // ── invalid_markup ──────────────────────────────────────────────────
    #[test]
    fn invalid_markup() {
        let u = src_unit("P01-001", "Deep learning models work well");
        assert!(validate(&ctx(&u, "en"), &u.html).is_ok());
        assert!(codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">Deep <b>learning</b> models work well</p>"#
        ))
        .contains(&"invalid_markup"));
    }

    #[test]
    fn invalid_markup_on_break_count_change() {
        let u = Unit {
            id: "P01-001".parse().unwrap(),
            html: r#"<p id="P01-001">alpha<br>beta gamma delta</p>"#.into(),
            styles: 0,
            atoms: vec![],
            breaks: 1,
        };
        assert!(validate(&ctx(&u, "en"), &u.html).is_ok());
        assert!(codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">alpha beta gamma delta</p>"#
        ))
        .contains(&"invalid_markup"));
    }

    // ── placeholder_count / unknown_placeholder ─────────────────────────
    #[test]
    fn placeholder_count() {
        let u = src_unit("P01-001", "The value {{KEEP_1}} equals {{KEEP_2}} here");
        assert!(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">Here {{KEEP_2}} equals the value {{KEEP_1}}</p>"#
        )
        .is_ok());
        assert!(codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">The value {{KEEP_1}} equals here</p>"#
        ))
        .contains(&"placeholder_count"));
    }

    #[test]
    fn unknown_placeholder() {
        let u = src_unit("P01-001", "The value {{KEEP_1}} is known here");
        assert!(!codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">The value {{KEEP_1}} is known here</p>"#
        ))
        .contains(&"unknown_placeholder"));
        assert!(codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">The value {{KEEP_7}} is known here</p>"#
        ))
        .contains(&"unknown_placeholder"));
    }

    // ── atom_hint_leakage ───────────────────────────────────────────────
    #[test]
    fn atom_hint_leakage() {
        let u = src_unit("P01-001", "The formula {{KEEP_1}} converges quickly");
        let hints = vec!["x^2 + y^2".to_string()];
        let mut c = ctx(&u, "en");
        c.atom_hints = &hints;
        assert!(validate(
            &c,
            r#"<p id="P01-001">The formula {{KEEP_1}} converges quickly</p>"#
        )
        .is_ok());
        assert!(codes(validate(
            &c,
            r#"<p id="P01-001">The formula x^2 + y^2 converges quickly {{KEEP_1}}</p>"#
        ))
        .contains(&"atom_hint_leakage"));
    }

    // ── link_label_changed ──────────────────────────────────────────────
    #[test]
    fn link_label_changed() {
        let u = src_unit("P01-001", "Write to alice@example.com for the dataset");
        assert!(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">For the dataset write to alice@example.com</p>"#
        )
        .is_ok());
        assert!(codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">For the dataset write to alice@sample.com</p>"#
        ))
        .contains(&"link_label_changed"));
    }

    // ── flexible style layout / resolvable style references ─────────────
    #[test]
    fn known_style_occurrences_may_change() {
        let u = Unit {
            id: "P01-001".parse().unwrap(),
            html: r#"<p id="P01-001">a <span data-style="1">bold</span> and <span data-style="2">italic</span> text</p>"#.into(),
            styles: 2,
            atoms: vec![],
            breaks: 0,
        };
        // 顺序自由：两个 span 交换位置仍然合法。
        assert!(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">a <span data-style="2">italic</span> and <span data-style="1">bold</span> text</p>"#
        )
        .is_ok());
        assert!(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">a <span data-style="1">bold</span> and italic text</p>"#
        )
        .is_ok());
    }

    #[test]
    fn unknown_style() {
        let u = Unit {
            id: "P01-001".parse().unwrap(),
            html: r#"<p id="P01-001">a <span data-style="1">bold</span> word here</p>"#.into(),
            styles: 1,
            atoms: vec![],
            breaks: 0,
        };
        assert!(!codes(validate(&ctx(&u, "en"), &u.html)).contains(&"unknown_style"));
        assert!(codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">a <span data-style="9">bold</span> word here</p>"#
        ))
        .contains(&"unknown_style"));
    }

    #[test]
    fn empty_style_is_not_a_translation_failure() {
        let u = Unit {
            id: "P01-001".parse().unwrap(),
            html: r#"<p id="P01-001">a <span data-style="1">bold</span> word here</p>"#.into(),
            styles: 1,
            atoms: vec![],
            breaks: 0,
        };
        assert!(validate(&ctx(&u, "en"), &u.html).is_ok());
        assert!(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">a bold<span data-style="1"> </span> word here</p>"#
        )
        .is_ok());
    }

    // ── unsafe_bidi_control / unbalanced_bidi_isolate ───────────────────
    #[test]
    fn unsafe_bidi_control() {
        let u = src_unit("P01-001", "Mixed direction text sample here");
        assert!(!codes(validate(&ctx(&u, "en"), &u.html)).contains(&"unsafe_bidi_control"));
        assert!(codes(validate(
            &ctx(&u, "en"),
            "<p id=\"P01-001\">Mixed \u{202E}direction text sample here</p>"
        ))
        .contains(&"unsafe_bidi_control"));
    }

    #[test]
    fn unbalanced_bidi_isolate() {
        let u = src_unit("P01-001", "Mixed direction text sample here");
        assert!(!codes(validate(
            &ctx(&u, "en"),
            "<p id=\"P01-001\">Mixed \u{2066}direction\u{2069} text sample here</p>"
        ))
        .contains(&"unbalanced_bidi_isolate"));
        assert!(codes(validate(
            &ctx(&u, "en"),
            "<p id=\"P01-001\">Mixed \u{2066}direction text sample here</p>"
        ))
        .contains(&"unbalanced_bidi_isolate"));
    }

    // ── duplicated_text_slots / unknown_paragraph ───────────────────────
    #[test]
    fn duplicated_text_slots() {
        let u = src_unit("P01-001", "Deep learning models work well");
        assert!(!codes(validate(&ctx(&u, "en"), &u.html)).contains(&"duplicated_text_slots"));
        assert!(codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">Deep learning</p><p id="P01-001">models work well</p>"#
        ))
        .contains(&"duplicated_text_slots"));
    }

    #[test]
    fn unknown_paragraph() {
        let u = src_unit("P01-001", "Deep learning models work well");
        assert!(!codes(validate(&ctx(&u, "en"), &u.html)).contains(&"unknown_paragraph"));
        assert!(codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P09-042">Deep learning models work well</p>"#
        ))
        .contains(&"unknown_paragraph"));
    }

    // ── neighbor_context_leakage ────────────────────────────────────────
    #[test]
    fn neighbor_context_leakage() {
        let u = src_unit("P01-002", "This paragraph stands entirely on its own");
        let before = "The previous paragraph discussed convergence rates in detail";
        let mut c = ctx(&u, "en");
        c.context_before = before;
        assert!(validate(
            &c,
            r#"<p id="P01-002">This paragraph stands entirely on its own</p>"#
        )
        .is_ok());
        assert!(codes(validate(
            &c,
            r#"<p id="P01-002">The previous paragraph discussed convergence rates in detail. This paragraph stands on its own</p>"#
        ))
        .contains(&"neighbor_context_leakage"));
    }

    // ── pathological_text_repetition ────────────────────────────────────
    #[test]
    fn pathological_text_repetition() {
        let u = src_unit("P01-001", "The model converges after a few epochs");
        assert!(!codes(validate(&ctx(&u, "en"), &u.html)).contains(&"pathological_text_repetition"));
        assert!(codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">The model converges converges converges converges converges converges</p>"#
        ))
        .contains(&"pathological_text_repetition"));
    }

    // ── protected_literal_count ─────────────────────────────────────────
    #[test]
    fn protected_literal_count() {
        let u = src_unit("P01-001", "We trained for 200 epochs with batch size 64");
        assert!(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">With batch size 64 we trained for 200 epochs</p>"#
        )
        .is_ok());
        assert!(codes(validate(
            &ctx(&u, "en"),
            r#"<p id="P01-001">We trained for 300 epochs with batch size 64</p>"#
        ))
        .contains(&"protected_literal_count"));
    }

    // ── excessive_target_expansion ──────────────────────────────────────
    #[test]
    fn excessive_target_expansion() {
        let u = src_unit("P01-001", "A short source sentence");
        assert!(!codes(validate(&ctx(&u, "en"), &u.html)).contains(&"excessive_target_expansion"));
        let long = "word ".repeat(40);
        let html = format!("<p id=\"P01-001\">{}</p>", long.trim());
        assert!(codes(validate(&ctx(&u, "en"), &html)).contains(&"excessive_target_expansion"));
    }

    #[test]
    fn target_script_percentage_is_not_required() {
        let u = src_unit("P01-001", "Deep learning models achieve strong results");
        assert!(validate(
            &ctx(&u, "zh-CN"),
            r#"<p id="P01-001">深度学习模型取得了很强的效果</p>"#
        )
        .is_ok());
        for lang in ["zh-CN", "ja", "ko", "ar", "ru", "en", "xx"] {
            assert!(validate(&ctx(&u, lang), &u.html).is_ok(), "{lang}");
        }
    }

    #[test]
    fn english_method_names_do_not_disqualify_chinese_headings() {
        let u = src_unit("P19-018", "G Evading Neural Cleanse");
        assert!(validate(
            &ctx(&u, "zh-CN"),
            r#"<p id="P19-018">G 规避 Neural Cleanse</p>"#,
        )
        .is_ok());
    }

    // ── residual_japanese ───────────────────────────────────────────────
    #[test]
    fn residual_japanese() {
        let u = src_unit("P01-001", "ディープラーニングのモデルは強い結果を出す");
        assert!(!codes(validate(
            &ctx(&u, "zh"),
            r#"<p id="P01-001">深度学习模型取得了很强的效果</p>"#
        ))
        .contains(&"residual_japanese"));
        assert!(codes(validate(
            &ctx(&u, "zh"),
            r#"<p id="P01-001">深度学习モデル取得了很强的效果</p>"#
        ))
        .contains(&"residual_japanese"));
        // 目标就是日文时不报。
        assert!(!codes(validate(&ctx(&u, "ja"), &u.html)).contains(&"residual_japanese"));
    }
}
