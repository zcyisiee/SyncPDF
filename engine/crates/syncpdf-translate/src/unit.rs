//! 翻译单元：`Paragraph` → 受限 HTML；模型回包 → `ParsedUnit`。
//!
//! 设计基准：02-技术路径与架构.md §5.1。受限 HTML 只认四种元素：
//! `<p id>`、`<span data-style="n">`、`{{KEEP_n}}`、`<br>`，其余一律非法。

use std::fmt;

use syncpdf_core::ir::Paragraph;
use syncpdf_core::{AtomId, GlyphId, ParagraphId, StyleId};

/// 行间基线距离超过 `line_height` 的该倍数时视为硬换行（`<br>`）。
const HARD_BREAK_RATIO: f32 = 1.6;

/// 翻译单元（一段的待译形态）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unit {
    pub id: ParagraphId,
    /// 受限 HTML 源文本（原子已是 `{{KEEP_n}}`）。
    pub html: String,
    /// `html` 中 `<span>` 的个数（供校验器比对 span 多重集）。
    pub styles: u32,
    /// `html` 中出现的原子（按 `AtomId` 升序，多重集）。
    pub atoms: Vec<AtomId>,
    /// 硬换行数（`<br>`）。
    pub breaks: u32,
}

impl Unit {
    /// 源文纯文本（不含标签与原子占位）。
    pub fn plain_text(&self) -> String {
        parse_unit_html(&self.html)
            .map(|p| p.text())
            .unwrap_or_default()
    }

    /// `html` 中出现过的样式号集合（升序去重），供 `unknown_style` 判定。
    pub fn style_ids(&self) -> Vec<StyleId> {
        let mut ids = parse_unit_html(&self.html)
            .map(|p| p.style_ids())
            .unwrap_or_default();
        ids.sort_unstable();
        ids.dedup();
        ids
    }
}

/// 解析后的翻译单元：文本 / 样式 / 原子 / 硬换行的树形结构。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedUnit {
    pub id: ParagraphId,
    pub segments: Vec<Segment>,
}

/// 段内节点。受限协议只允许一层 `<span>`，嵌套在解析期就被拒。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Segment {
    Text(String),
    Style { id: StyleId, inner: Vec<Segment> },
    Atom(AtomId),
    Br,
}

impl ParsedUnit {
    /// 全部文本节点拼接（不含原子、样式标签、换行）。
    pub fn text(&self) -> String {
        let mut out = String::new();
        collect_text(&self.segments, &mut out);
        out
    }

    /// 样式 span 多重集（顺序即出现顺序）。
    pub fn style_ids(&self) -> Vec<StyleId> {
        let mut out = Vec::new();
        collect_styles(&self.segments, &mut out);
        out
    }

    /// 原子多重集（顺序即出现顺序）。
    pub fn atom_ids(&self) -> Vec<AtomId> {
        let mut out = Vec::new();
        collect_atoms(&self.segments, &mut out);
        out
    }

    /// `<br>` 数量。
    pub fn break_count(&self) -> u32 {
        let mut n = 0;
        collect_breaks(&self.segments, &mut n);
        n
    }

    /// 每个 span 的纯文本（顺序即出现顺序），供 `empty_style` 判定。
    pub fn style_texts(&self) -> Vec<(StyleId, String)> {
        let mut out = Vec::new();
        collect_style_texts(&self.segments, &mut out);
        out
    }

    /// 序列化回受限 HTML（`parse_unit_html` 的逆运算）。
    pub fn to_html(&self) -> String {
        let mut out = format!("<p id=\"{}\">", self.id);
        write_segments(&self.segments, &mut out);
        out.push_str("</p>");
        out
    }
}

fn write_segments(segs: &[Segment], out: &mut String) {
    for s in segs {
        match s {
            Segment::Text(t) => out.push_str(&escape_text(t)),
            Segment::Style { id, inner } => {
                out.push_str(&format!("<span data-style=\"{}\">", id.0));
                write_segments(inner, out);
                out.push_str("</span>");
            }
            Segment::Atom(a) => out.push_str(&format!("{{{{KEEP_{}}}}}", a.0)),
            Segment::Br => out.push_str("<br>"),
        }
    }
}

/// 把纯文本转义为受限 HTML 文本节点。
pub fn escape_text(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            _ => out.push(c),
        }
    }
    out
}

fn collect_text(segs: &[Segment], out: &mut String) {
    for s in segs {
        match s {
            Segment::Text(t) => out.push_str(t),
            Segment::Style { inner, .. } => collect_text(inner, out),
            Segment::Atom(_) | Segment::Br => {}
        }
    }
}

fn collect_styles(segs: &[Segment], out: &mut Vec<StyleId>) {
    for s in segs {
        if let Segment::Style { id, inner } = s {
            out.push(*id);
            collect_styles(inner, out);
        }
    }
}

fn collect_style_texts(segs: &[Segment], out: &mut Vec<(StyleId, String)>) {
    for s in segs {
        if let Segment::Style { id, inner } = s {
            let mut t = String::new();
            collect_text(inner, &mut t);
            out.push((*id, t));
            collect_style_texts(inner, out);
        }
    }
}

fn collect_atoms(segs: &[Segment], out: &mut Vec<AtomId>) {
    for s in segs {
        match s {
            Segment::Atom(a) => out.push(*a),
            Segment::Style { inner, .. } => collect_atoms(inner, out),
            _ => {}
        }
    }
}

fn collect_breaks(segs: &[Segment], out: &mut u32) {
    for s in segs {
        match s {
            Segment::Br => *out += 1,
            Segment::Style { inner, .. } => collect_breaks(inner, out),
            _ => {}
        }
    }
}

/// 从 `Paragraph` 构建翻译单元。
///
/// 两条取文路径：
/// - 段内有字形（`p.glyphs` 非空）时逐字形取文，才能把 `style_runs` / `atoms`
///   的字形区间对齐到文本位置，从而包出 `<span data-style="n">` 与 `{{KEEP_n}}`，
///   并按 `p.lines` 的基线间距插入硬换行 `<br>`；`glyph_text` 返回 `None` 的字形
///   贡献空串。
/// - 段内没有字形（上游只给了 `p.text`）时退化为单段纯文本，不产生 span/br。
///
/// 调用方负责先筛掉 `Translatable::No` 的段落。
pub fn build_unit(p: &Paragraph, glyph_text: impl Fn(GlyphId) -> Option<String>) -> Unit {
    let body = if p.glyphs.is_empty() {
        escape_text(&p.text)
    } else if !p.text_spans.is_empty() {
        build_body_from_spans(p)
    } else {
        build_body_from_glyphs(p, glyph_text)
    };
    let html = format!("<p id=\"{}\">{}</p>", p.id, body);
    // 统计一律以最终 HTML 为准，保证与校验器看到的是同一份事实。
    let parsed = parse_unit_html(&html);
    let (styles, mut atoms, breaks) = match &parsed {
        Ok(u) => (u.style_ids().len() as u32, u.atom_ids(), u.break_count()),
        // 理论不可达：本函数自己拼的 HTML 一定合法；真出现就按空统计兜底。
        Err(_) => (0, Vec::new(), 0),
    };
    atoms.sort_unstable();
    Unit {
        id: p.id.clone(),
        html,
        styles,
        atoms,
        breaks,
    }
}

/// Source spans carry both genuine glyph text and zero-length generated separators.
/// Process a separator before the glyph at its boundary, then account for atoms using
/// only real glyph ranges. This prevents a generated space from consuming a glyph.
fn build_body_from_spans(p: &Paragraph) -> String {
    let n = p.glyphs.len() as u32;
    let breaks = hard_break_indices(p);
    let mut out = String::new();
    let mut open = None;
    let mut span_index = 0usize;
    let mut i = 0u32;
    while i <= n {
        let hard_break = breaks.contains(&i);
        if hard_break {
            switch_style(&mut out, &mut open, None);
            out.push_str("<br>");
        }
        while let Some(span) = p.text_spans.get(span_index) {
            if span.glyph_range != (i, i) {
                break;
            }
            // A hard line break supersedes a soft separator at the same boundary.
            if !(hard_break && span.text.chars().all(char::is_whitespace)) {
                let next_style = if i < n { style_at(p, i) } else { None };
                if open != next_style {
                    switch_style(&mut out, &mut open, None);
                }
                out.push_str(&escape_text(&span.text));
            }
            span_index += 1;
        }
        if i == n {
            break;
        }
        if let Some(atom) = atom_at(p, i) {
            switch_style(&mut out, &mut open, style_at(p, i));
            out.push_str(&format!("{{{{KEEP_{}}}}}", atom.id.0));
            i = atom.glyph_range.1.max(i + 1).min(n);
            while p
                .text_spans
                .get(span_index)
                .is_some_and(|s| s.glyph_range.0 < i)
            {
                span_index += 1;
            }
            continue;
        }
        switch_style(&mut out, &mut open, style_at(p, i));
        if let Some(span) = p.text_spans.get(span_index) {
            if span.glyph_range.0 == i && span.glyph_range.1 > i {
                out.push_str(&escape_text(&span.text));
                i = span.glyph_range.1.min(n);
                span_index += 1;
                continue;
            }
        }
        // A real glyph with no Unicode has no source span, but still owns an index.
        i += 1;
    }
    switch_style(&mut out, &mut open, None);
    out
}

fn switch_style(out: &mut String, open: &mut Option<StyleId>, want: Option<StyleId>) {
    if *open == want {
        return;
    }
    if open.is_some() {
        out.push_str("</span>");
    }
    if let Some(id) = want {
        out.push_str(&format!("<span data-style=\"{}\">", id.0));
    }
    *open = want;
}

/// 逐字形拼 body：样式 run 包 span、原子折叠成 `{{KEEP_n}}`、硬换行插 `<br>`。
fn build_body_from_glyphs(p: &Paragraph, glyph_text: impl Fn(GlyphId) -> Option<String>) -> String {
    let n = p.glyphs.len() as u32;
    let breaks = hard_break_indices(p);
    let mut out = String::new();
    let mut open: Option<StyleId> = None;
    let mut i = 0u32;
    while i < n {
        // 硬换行插在该字形之前，且放在 span 之外（换行不属于任何样式）。
        if breaks.contains(&i) {
            if open.take().is_some() {
                out.push_str("</span>");
            }
            out.push_str("<br>");
        }
        let want = style_at(p, i);
        if want != open {
            if open.is_some() {
                out.push_str("</span>");
            }
            if let Some(s) = want {
                out.push_str(&format!("<span data-style=\"{}\">", s.0));
            }
            open = want;
        }
        match atom_at(p, i) {
            Some(atom) => {
                out.push_str(&format!("{{{{KEEP_{}}}}}", atom.id.0));
                // 原子整体折叠；空区间也至少前进一个字形，避免死循环。
                i = atom.glyph_range.1.max(i + 1);
            }
            None => {
                if let Some(t) = glyph_text(p.glyphs[i as usize]) {
                    out.push_str(&escape_text(&t));
                }
                i += 1;
            }
        }
    }
    if open.is_some() {
        out.push_str("</span>");
    }
    out
}

/// 段内字形序号 `i` 所属的样式 run（`StyleId(0)` 视为无样式）。
fn style_at(p: &Paragraph, i: u32) -> Option<StyleId> {
    p.style_runs
        .iter()
        .find(|r| i >= r.glyph_range.0 && i < r.glyph_range.1)
        .map(|r| r.id)
        .filter(|s| s.0 != 0)
}

/// 段内字形序号 `i` 是否是某个原子的起点。
fn atom_at(p: &Paragraph, i: u32) -> Option<&syncpdf_core::ir::Atom> {
    p.atoms.iter().find(|a| a.glyph_range.0 == i)
}

/// 需要插入 `<br>` 的段内字形序号集合。
///
/// 只有当 `p.lines` 的字形总数与 `p.glyphs` 一致时才敢做位置映射；行间基线距离
/// 超过 `line_height * HARD_BREAK_RATIO` 的判为硬换行（诗歌、地址），普通折行
/// 是软换行不入 HTML。
fn hard_break_indices(p: &Paragraph) -> Vec<u32> {
    let total: usize = p.lines.iter().map(|l| l.glyphs.len()).sum();
    if p.lines.len() < 2 || total != p.glyphs.len() || p.line_height <= 0.0 {
        return Vec::new();
    }
    let mut out = Vec::new();
    let mut start = 0usize;
    for w in p.lines.windows(2) {
        start += w[0].glyphs.len();
        let gap = (w[0].baseline_y - w[1].baseline_y).abs();
        if gap > p.line_height * HARD_BREAK_RATIO {
            out.push(start as u32);
        }
    }
    out
}

/// `parse_unit_html` 失败原因。
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum UnitParseError {
    #[error("invalid markup: {0}")]
    Invalid(String),
    #[error("invalid paragraph id: {0}")]
    BadId(String),
}

impl fmt::Display for Segment {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Segment::Text(t) => f.write_str(t),
            Segment::Style { id, .. } => write!(f, "<span data-style=\"{}\">…</span>", id.0),
            Segment::Atom(a) => write!(f, "{{{{KEEP_{}}}}}", a.0),
            Segment::Br => f.write_str("<br>"),
        }
    }
}

/// 手写受限 HTML 解析器：只认 `<p id="…">`、`<span data-style="n">`、
/// `</span>`、`</p>`、`<br>`、`{{KEEP_n}}` 与普通文本。
///
/// - 必须恰好一个顶层 `<p>`，且 `<p>` 之前 / `</p>` 之后不得有非空白内容。
/// - 任何其他标签、未闭合、属性格式不对 = `Invalid`。
/// - `<span>` 不得嵌套（受限协议只允许一层）。
/// - 文本节点里 `&lt; &gt; &amp; &quot; &#39;` 反转义；其余裸 `&` 按字面处理。
pub fn parse_unit_html(html: &str) -> Result<ParsedUnit, UnitParseError> {
    let n = html.len();
    let invalid = |what: &str| UnitParseError::Invalid(what.to_string());

    let mut pos = skip_ws(html, 0);
    let id = match parse_p_open(&html[pos..]) {
        Ok((id, len)) => {
            pos += len;
            id.parse::<ParagraphId>()
                .map_err(|_| UnitParseError::BadId(id.to_string()))?
        }
        Err(e) => return Err(invalid(&e)),
    };

    // `segments` 始终是「当前打开的容器」的内容；`style_stack` 存被挂起的外层。
    let mut segments: Vec<Segment> = Vec::new();
    let mut style_stack: Vec<(StyleId, Vec<Segment>)> = Vec::new();
    let mut closed_p = false;

    while pos < n {
        let rest = &html[pos..];
        if rest.starts_with("</p>") {
            if !style_stack.is_empty() {
                return Err(invalid("unclosed <span> before </p>"));
            }
            pos += 4;
            if skip_ws(html, pos) != n {
                return Err(invalid("content after </p>"));
            }
            closed_p = true;
            break;
        }
        if rest.starts_with("<span") {
            if !style_stack.is_empty() {
                return Err(invalid("nested <span>"));
            }
            match parse_span_open(rest) {
                Some((style_id, tag_len)) => {
                    // 把外层内容挂起，`segments` 换成这个 span 的内容缓冲。
                    style_stack.push((style_id, std::mem::take(&mut segments)));
                    pos += tag_len;
                }
                None => return Err(invalid("malformed <span data-style=…>")),
            }
            continue;
        }
        if rest.starts_with("</span>") {
            match style_stack.pop() {
                Some((sid, outer)) => {
                    // `segments` 现在是 span 的内容，换回外层后把 span 挂上去。
                    let inner = std::mem::replace(&mut segments, outer);
                    segments.push(Segment::Style { id: sid, inner });
                    pos += 7;
                }
                None => return Err(invalid("unbalanced </span>")),
            }
            continue;
        }
        if rest.starts_with("<br>") || rest.starts_with("<br/>") || rest.starts_with("<br />") {
            segments.push(Segment::Br);
            pos += if rest.starts_with("<br>") {
                4
            } else if rest.starts_with("<br/>") {
                5
            } else {
                6
            };
            continue;
        }
        // {{KEEP_n}}：非规范形态（空号、非数字、前导 0）一律 Invalid。
        if rest.starts_with("{{KEEP_") {
            let Some(end) = rest.find("}}") else {
                return Err(invalid("unterminated {{KEEP_n}}"));
            };
            let num = &rest["{{KEEP_".len()..end];
            let well_formed = !num.is_empty()
                && num != "0"
                && !num.starts_with('0')
                && num.bytes().all(|b| b.is_ascii_digit());
            match (well_formed, num.parse::<u32>()) {
                (true, Ok(k)) => {
                    segments.push(Segment::Atom(AtomId(k)));
                    pos += end + 2;
                }
                _ => return Err(invalid(&format!("bad atom token {}", &rest[..end + 2]))),
            }
            continue;
        }
        if rest.starts_with('<') {
            // 其余任何 `<…`（含未闭合、未知标签、大写变体）都非法。
            let tag_end = rest.find('>').map(|i| i + 1).unwrap_or(rest.len());
            return Err(invalid(&format!("disallowed tag {}", &rest[..tag_end])));
        }
        // 普通文本：消费到下一个 `<` 或 `{{KEEP_` 为止（字面 `{` 合法）。
        let mut end = pos;
        let mut text = String::new();
        while end < n {
            let r = &html[end..];
            if r.starts_with('<') || r.starts_with("{{KEEP_") {
                break;
            }
            if r.starts_with('&') {
                if let Some((ch, len)) = decode_entity(r) {
                    text.push(ch);
                    end += len;
                    continue;
                }
            }
            let ch = r.chars().next().expect("non-empty remainder has a char");
            text.push(ch);
            end += ch.len_utf8();
        }
        debug_assert!(end > pos, "text consumer must make progress");
        if !text.is_empty() {
            segments.push(Segment::Text(text));
        }
        pos = end;
    }

    if !closed_p {
        return Err(invalid("missing </p>"));
    }
    if !style_stack.is_empty() {
        return Err(invalid("unclosed <span>"));
    }
    Ok(ParsedUnit { id, segments })
}

/// 识别受限协议允许的五个实体；其余返回 `None`（裸 `&` 按字面处理）。
fn decode_entity(rest: &str) -> Option<(char, usize)> {
    for (name, ch) in [
        ("&amp;", '&'),
        ("&lt;", '<'),
        ("&gt;", '>'),
        ("&quot;", '"'),
        ("&apos;", '\''),
        ("&#39;", '\''),
    ] {
        if rest.starts_with(name) {
            return Some((ch, name.len()));
        }
    }
    None
}

fn skip_ws(s: &str, mut i: usize) -> usize {
    let b = s.as_bytes();
    while i < b.len() && (b[i] == b' ' || b[i] == b'\n' || b[i] == b'\r' || b[i] == b'\t') {
        i += 1;
    }
    i
}

/// 解析 `<p id="…">`，返回 (id 字面, 标签总长)。
fn parse_p_open(rest: &str) -> Result<(&str, usize), String> {
    let after = rest.strip_prefix("<p").ok_or("expected <p id=…>")?;
    // `<p` 后必须是空白，否则是 `<pre>` 之类的别的标签。
    let trimmed = after.trim_start_matches([' ', '\t', '\n', '\r']);
    if trimmed.len() == after.len() {
        return Err("expected whitespace after <p".into());
    }
    let ws = after.len() - trimmed.len();
    let attr = trimmed
        .strip_prefix("id=\"")
        .ok_or("expected id=\"…\" in <p>")?;
    let q = attr.find('"').ok_or("unterminated id attribute")?;
    let tail = &attr[q + 1..];
    if !tail.starts_with('>') {
        return Err("expected > after id attribute".into());
    }
    Ok((&attr[..q], "<p".len() + ws + "id=\"".len() + q + 2))
}

/// 解析 `<span data-style="n">`，返回 (StyleId, 标签总长)。
fn parse_span_open(rest: &str) -> Option<(StyleId, usize)> {
    let prefix = "<span data-style=\"";
    let after = rest.strip_prefix(prefix)?;
    let q = after.find('"')?;
    let num = &after[..q];
    // 1 基、无前导 0、纯数字。
    if num.is_empty() || num.starts_with('0') || !num.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    if !after[q + 1..].starts_with('>') {
        return None;
    }
    Some((StyleId(num.parse().ok()?), prefix.len() + q + 2))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use syncpdf_core::ir::{
        Align, Atom, AtomKind, Line, RegionKind, SourceTextSpan, StyleRun, Translatable,
    };
    use syncpdf_core::{OpKey, PageId, Rect};

    pub(crate) fn glyph(i: u32) -> GlyphId {
        GlyphId {
            page: PageId(0),
            op: OpKey::new(syncpdf_core::ObjRef::new(1, 0), 0),
            ordinal: i as u16,
        }
    }

    /// 造一个「字形即字符」的段落：`chars` 每个元素是一个字形的文本。
    pub(crate) fn paragraph_of(id: &str, chars: &[&str]) -> Paragraph {
        Paragraph {
            id: id.parse().unwrap(),
            page: PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox: Rect::new(0.0, 0.0, 100.0, 20.0),
            lines: vec![],
            glyphs: (0..chars.len() as u32).map(glyph).collect(),
            text_spans: Vec::new(),
            style_runs: vec![],
            atoms: vec![],
            text: chars.concat(),
            align: Align::Left,
            first_indent: 0.0,
            line_height: 12.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        }
    }

    fn run(id: u32, range: (u32, u32)) -> StyleRun {
        StyleRun {
            id: StyleId(id),
            glyph_range: range,
            font: 0,
            size: 10.0,
            color: Default::default(),
            bold: false,
            italic: false,
        }
    }

    /// 测试用取文闭包：按 ordinal 从 `chars` 里取。
    pub(crate) fn texter(chars: Vec<String>) -> impl Fn(GlyphId) -> Option<String> {
        move |g: GlyphId| chars.get(g.ordinal as usize).cloned()
    }

    #[test]
    fn build_unit_from_text_only() {
        let mut p = paragraph_of("P01-001", &[]);
        p.text = "Deep learning {{KEEP_1}} works".into();
        let u = build_unit(&p, |_| None);
        assert_eq!(
            u.html,
            "<p id=\"P01-001\">Deep learning {{KEEP_1}} works</p>"
        );
        assert_eq!(u.styles, 0);
        assert_eq!(u.atoms, vec![AtomId(1)]);
        assert_eq!(u.breaks, 0);
        assert_eq!(u.plain_text(), "Deep learning  works");
    }

    #[test]
    fn build_unit_escapes_text_only_body() {
        let mut p = paragraph_of("P01-001", &[]);
        p.text = "a < b & c".into();
        let u = build_unit(&p, |_| None);
        assert_eq!(u.html, "<p id=\"P01-001\">a &lt; b &amp; c</p>");
        assert_eq!(u.plain_text(), "a < b & c");
    }

    #[test]
    fn build_unit_wraps_style_runs_and_atoms() {
        let chars: Vec<String> = "ABCDEF".chars().map(|c| c.to_string()).collect();
        let mut p = paragraph_of("P01-002", &["A", "B", "C", "D", "E", "F"]);
        // 字形 1..3 是样式 1；字形 3..5 是一个原子。
        p.style_runs = vec![run(1, (1, 3))];
        p.atoms = vec![Atom {
            id: AtomId(1),
            glyph_range: (3, 5),
            kind: AtomKind::Formula,
            text: "x^2".into(),
        }];
        let u = build_unit(&p, texter(chars));
        assert_eq!(
            u.html,
            "<p id=\"P01-002\">A<span data-style=\"1\">BC</span>{{KEEP_1}}F</p>"
        );
        assert_eq!(u.styles, 1);
        assert_eq!(u.atoms, vec![AtomId(1)]);
        assert_eq!(u.plain_text(), "ABCF");
    }

    #[test]
    fn mapped_source_space_stays_between_glyphs_and_styles() {
        let mut p = paragraph_of("P01-004", &["A", "B", "C", "D"]);
        p.text = "AB CD".into();
        p.style_runs = vec![run(1, (0, 2)), run(2, (2, 4))];
        p.text_spans = vec![
            SourceTextSpan {
                text: "A".into(),
                glyph_range: (0, 1),
            },
            SourceTextSpan {
                text: "B".into(),
                glyph_range: (1, 2),
            },
            SourceTextSpan {
                text: " ".into(),
                glyph_range: (2, 2),
            },
            SourceTextSpan {
                text: "C".into(),
                glyph_range: (2, 3),
            },
            SourceTextSpan {
                text: "D".into(),
                glyph_range: (3, 4),
            },
        ];
        let unit = build_unit(&p, |_| Some("wrong".into()));
        assert_eq!(unit.html, "<p id=\"P01-004\"><span data-style=\"1\">AB</span> <span data-style=\"2\">CD</span></p>");
        assert_eq!(unit.plain_text(), "AB CD");
    }

    #[test]
    fn mapped_atom_owns_only_its_true_glyph_range() {
        let mut p = paragraph_of("P01-005", &["2", "5", "m", "s", "X"]);
        p.text = "25 ms X".into();
        p.atoms = vec![Atom {
            id: AtomId(1),
            glyph_range: (0, 4),
            kind: AtomKind::Number,
            text: "25 ms".into(),
        }];
        p.text_spans = vec![
            SourceTextSpan {
                text: "2".into(),
                glyph_range: (0, 1),
            },
            SourceTextSpan {
                text: "5".into(),
                glyph_range: (1, 2),
            },
            SourceTextSpan {
                text: " ".into(),
                glyph_range: (2, 2),
            },
            SourceTextSpan {
                text: "m".into(),
                glyph_range: (2, 3),
            },
            SourceTextSpan {
                text: "s".into(),
                glyph_range: (3, 4),
            },
            SourceTextSpan {
                text: " ".into(),
                glyph_range: (4, 4),
            },
            SourceTextSpan {
                text: "X".into(),
                glyph_range: (4, 5),
            },
        ];
        let unit = build_unit(&p, |_| Some("wrong".into()));
        assert_eq!(unit.html, "<p id=\"P01-005\">{{KEEP_1}} X</p>");
        assert_eq!(unit.atoms, vec![AtomId(1)]);
    }

    #[test]
    fn build_unit_inserts_hard_break_on_large_line_gap() {
        let chars: Vec<String> = "abcd".chars().map(|c| c.to_string()).collect();
        let mut p = paragraph_of("P01-003", &["a", "b", "c", "d"]);
        p.line_height = 10.0;
        p.lines = vec![
            Line {
                glyphs: vec![glyph(0), glyph(1)],
                baseline_y: 100.0,
                bbox: Rect::new(0.0, 0.0, 10.0, 10.0),
            },
            Line {
                glyphs: vec![glyph(2), glyph(3)],
                baseline_y: 70.0, // 间距 30 > 10*1.6 → 硬换行
                bbox: Rect::new(0.0, 0.0, 10.0, 10.0),
            },
        ];
        let u = build_unit(&p, texter(chars.clone()));
        assert_eq!(u.html, "<p id=\"P01-003\">ab<br>cd</p>");
        assert_eq!(u.breaks, 1);

        // 正常行距 → 软换行，不产生 <br>。
        p.lines[1].baseline_y = 89.0;
        let u = build_unit(&p, texter(chars));
        assert_eq!(u.html, "<p id=\"P01-003\">abcd</p>");
        assert_eq!(u.breaks, 0);
    }

    #[test]
    fn parse_roundtrip() {
        let html =
            r#"<p id="P01-003">Deep <span data-style="1">learning</span> {{KEEP_1}}<br>end</p>"#;
        let u = parse_unit_html(html).unwrap();
        assert_eq!(u.id, "P01-003".parse::<ParagraphId>().unwrap());
        assert_eq!(u.style_ids(), vec![StyleId(1)]);
        assert_eq!(u.atom_ids(), vec![AtomId(1)]);
        assert_eq!(u.break_count(), 1);
        assert_eq!(u.text(), "Deep learning end");
        // Text("Deep ") + Style + Text(" ") + Atom + Br + Text("end")
        assert_eq!(u.segments.len(), 6);
        assert_eq!(u.to_html(), html);
    }

    #[test]
    fn span_contents_land_inside_the_span() {
        // 回归：曾经把 span 内的 Atom/Br/Text 误挂到被挂起的外层缓冲上。
        let html = r#"<p id="P01-001">A <span data-style="2">{{KEEP_1}} B<br>C</span> D</p>"#;
        let u = parse_unit_html(html).unwrap();
        assert_eq!(
            u.segments,
            vec![
                Segment::Text("A ".into()),
                Segment::Style {
                    id: StyleId(2),
                    inner: vec![
                        Segment::Atom(AtomId(1)),
                        Segment::Text(" B".into()),
                        Segment::Br,
                        Segment::Text("C".into()),
                    ],
                },
                Segment::Text(" D".into()),
            ]
        );
        assert_eq!(u.style_texts(), vec![(StyleId(2), " BC".to_string())]);
        assert_eq!(u.to_html(), html);
    }

    #[test]
    fn parse_rejects_bad_markup() {
        for bad in [
            "<p id=\"P01-001\">a</p>trailing",
            "<p id=\"P01-001\">a",
            "<p id=\"P01-001\">a</p><p id=\"P01-002\">b</p>",
            "<p id=\"P01-001\"><b>bold</b></p>",
            "<p id=\"P01-001\">a</span></p>",
            "<p id=\"P01-001\"><span data-style=\"1\">a</p>",
            "<p id=\"P01-001\"><span data-style=\"1\"><span data-style=\"2\">a</span></span></p>",
            "<p class=\"x\" id=\"P01-001\">a</p>",
            "<p id='P01-001'>a</p>",
            "<pre id=\"P01-001\">a</pre>",
            "<p id=\"P01-001\">{{KEEP_}}</p>",
            "<p id=\"P01-001\">{{KEEP_x}}</p>",
            "<p id=\"P01-001\">{{KEEP_01}}</p>",
            "<p id=\"P01-001\">{{KEEP_0}}</p>",
            "<p id=\"P01-001\">{{KEEP_1</p>",
            "<p id=\"P01-001\"><span data-style=\"0\">a</span></p>",
            "<p id=\"bad\">a</p>",
            "",
            "   ",
        ] {
            assert!(
                parse_unit_html(bad).is_err(),
                "should reject {bad:?}: got {:?}",
                parse_unit_html(bad)
            );
        }
    }

    #[test]
    fn parse_allows_literal_braces_and_rejects_stray_lt() {
        // 字面 `{` 是合法文本；裸 `<` 不是受限协议标签，必须拒绝（源文该转义）。
        let u = parse_unit_html(r#"<p id="P01-001">a {b} c</p>"#).unwrap();
        assert_eq!(u.text(), "a {b} c");
        assert!(parse_unit_html(r#"<p id="P01-001">1<2 ok</p>"#).is_err());
    }

    #[test]
    fn parse_decodes_entities_and_tolerates_bare_amp() {
        let u = parse_unit_html(r#"<p id="P01-001">a &lt;b&gt; &amp; c &nbsp d</p>"#).unwrap();
        assert_eq!(u.text(), "a <b> & c &nbsp d");
    }

    #[test]
    fn parse_accepts_self_closing_br() {
        for html in [
            r#"<p id="P01-001">a<br/>b</p>"#,
            r#"<p id="P01-001">a<br />b</p>"#,
        ] {
            assert_eq!(parse_unit_html(html).unwrap().break_count(), 1);
        }
    }

    #[test]
    fn parse_tolerates_surrounding_whitespace() {
        let u = parse_unit_html("  \n<p id=\"P01-001\">a</p>\n  ").unwrap();
        assert_eq!(u.text(), "a");
    }
}
