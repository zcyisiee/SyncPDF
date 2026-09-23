//! paragraph_analysis 阶段：区域内行聚类 → 段落（样式 run / 原子 / 可译判定）。
//!
//! 设计基准：02-技术路径与架构.md §3（paragraph_analysis 行）与 §4.3。
//! 本模块是**纯函数**：输入 `PageIR` + 区域，输出 `Vec<Paragraph>`，不碰 IO，
//! 因此覆盖了绝大部分单元测试。
//!
//! 段落切分规则（按区域 `order` 依次处理，`seq` 跨区域连续）：
//! 1. 相邻两行 x 范围重叠 **且** 基线差 < 1.8×字号 → 同段；
//! 2. 当前行首字形 x 比前一行首字形 x 大 ≥ 1.5×字号（首行缩进）→ 新段；
//! 3. 否则新段。

use std::sync::OnceLock;

use regex::Regex;
use syncpdf_core::ir::{
    Align, Atom, AtomKind, Line, PageIR, Paragraph, Region, RegionKind, SourceTextSpan, StyleRun,
    Translatable,
};
use syncpdf_core::{AtomId, GlyphId, PageId, ParagraphId, Rect, StyleId};
use syncpdf_layout::group_lines;

/// 行距（基线差）超过字号该倍数即视为不同段。
const PARAGRAPH_GAP_RATIO: f32 = 1.8;
/// 首行缩进判定：首字形 x 前移超过字号该倍数。
const INDENT_RATIO: f32 = 1.5;
/// 对齐判定容差（pt）。
const ALIGN_TOL: f32 = 1.0;
/// 居中的最小左右边距（pt）：小于它就只能算左对齐。
const CENTER_MIN_MARGIN: f32 = 3.0;
/// 「太短」阈值：去掉原子后有效字符数低于它不可译。
const MIN_TEXT_CHARS: usize = 2;

/// 分析一页：逐区域产出段落。
pub fn analyze_page(ir: &PageIR, regions: &[Region]) -> Vec<Paragraph> {
    let mut regions: Vec<&Region> = regions.iter().collect();
    regions.sort_by_key(|r| (r.order, r.index));

    let glyphs: Vec<&syncpdf_core::ir::Glyph> = ir.glyphs().collect();
    // 保留区域的源字形不能经重叠正文再次进入替换集合。
    // 可靠的行内原子放置尚未接通时，保留整个相交段并显式报告。
    let protected: std::collections::BTreeSet<GlyphId> = glyphs
        .iter()
        .filter(|g| {
            regions
                .iter()
                .any(|r| !r.kind.translatable() && r.bbox.contains(g.bbox.center()))
        })
        .map(|g| g.id)
        .collect();
    let mut out: Vec<Paragraph> = Vec::new();
    let mut seq: u32 = 0;

    for region in regions {
        let in_region: Vec<(GlyphId, Rect)> = glyphs
            .iter()
            .filter(|g| !g.flags.invisible && !g.flags.outside_clip)
            .filter(|g| {
                let c = g.bbox.center();
                c.x >= region.bbox.x0
                    && c.x <= region.bbox.x1
                    && c.y >= region.bbox.y0
                    && c.y <= region.bbox.y1
            })
            .map(|g| (g.id, g.bbox))
            .collect();
        if in_region.is_empty() {
            continue;
        }
        let lines = group_lines(&in_region, &region.bbox);
        for group in merge_lines(&lines, &glyphs) {
            seq += 1;
            let mut paragraph = build_paragraph(ir, region, group, &glyphs, seq);
            if matches!(paragraph.translatable, Translatable::Yes)
                && paragraph.glyphs.iter().any(|id| protected.contains(id))
            {
                paragraph.translatable = Translatable::No {
                    reason: "protected_source_overlap".into(),
                };
            }
            if matches!(paragraph.translatable, Translatable::Yes)
                && group_is_rotated(&paragraph, &glyphs)
            {
                paragraph.translatable = Translatable::No {
                    reason: "rotated_source_text".into(),
                };
            }
            out.push(paragraph);
        }
    }
    // A rotated source paragraph can also intersect an ordinary text region.
    // Keep every candidate that contains any of those source glyphs.
    let rotated: std::collections::BTreeSet<GlyphId> = out
        .iter()
        .filter(|p| matches!(&p.translatable, Translatable::No { reason } if reason == "rotated_source_text"))
        .flat_map(|p| p.glyphs.iter().copied())
        .collect();
    for p in &mut out {
        if matches!(p.translatable, Translatable::Yes)
            && p.glyphs.iter().any(|id| rotated.contains(id))
        {
            p.translatable = Translatable::No {
                reason: "rotated_source_text".into(),
            };
        }
    }
    // 重叠的可译区域可能各自产出同一源字形。整段保留，避免双重删除。
    let mut counts = std::collections::HashMap::<GlyphId, u32>::new();
    for p in &out {
        if matches!(p.translatable, Translatable::Yes) {
            for id in &p.glyphs {
                *counts.entry(*id).or_default() += 1;
            }
        }
    }
    for p in &mut out {
        if matches!(p.translatable, Translatable::Yes)
            && p.glyphs
                .iter()
                .any(|id| counts.get(id).copied().unwrap_or(0) > 1)
        {
            p.translatable = Translatable::No {
                reason: "translatable_region_overlap".into(),
            };
        }
    }
    out
}

fn group_is_rotated(p: &Paragraph, glyphs: &[&syncpdf_core::ir::Glyph]) -> bool {
    // The binder currently stores a translation-only glyph matrix, so PDF text drawn
    // vertically also needs a geometric guard. A narrow stack of mostly single-glyph
    // rows is not a horizontal paragraph even when each glyph box is axis-aligned.
    if p.lines.len() >= 4
        && p.lines.iter().all(|line| line.glyphs.len() <= 2)
        && p.bbox.height() > p.bbox.width() * 2.0
    {
        return true;
    }
    let by_id: std::collections::HashMap<GlyphId, &syncpdf_core::ir::Glyph> =
        glyphs.iter().map(|g| (g.id, *g)).collect();
    p.glyphs.iter().any(|id| {
        by_id.get(id).is_some_and(|g| {
            let m = g.matrix;
            let horizontal = (m.a * m.a + m.b * m.b).sqrt();
            horizontal > 0.0 && m.b.abs() > horizontal * 0.25
        })
    })
}

/// 一行：字形序号（段内 0 基）+ 基线 y。
#[derive(Debug, Clone)]
struct Row {
    glyphs: Vec<u32>,
    baseline_y: f32,
    bbox: Rect,
}

/// 行聚类结果 → 段落的行分组。
fn merge_lines(line_ids: &[Vec<GlyphId>], glyphs: &[&syncpdf_core::ir::Glyph]) -> Vec<Vec<Row>> {
    let index_of: std::collections::HashMap<GlyphId, usize> =
        glyphs.iter().enumerate().map(|(i, g)| (g.id, i)).collect();

    let rows: Vec<Row> = line_ids
        .iter()
        .filter_map(|ids| {
            let mut gs: Vec<u32> = ids
                .iter()
                .filter_map(|id| index_of.get(id).map(|i| *i as u32))
                .collect();
            if gs.is_empty() {
                return None;
            }
            gs.sort_by(|a, b| {
                glyphs[*a as usize]
                    .bbox
                    .x0
                    .total_cmp(&glyphs[*b as usize].bbox.x0)
            });
            let mut bbox = glyphs[gs[0] as usize].bbox;
            let mut baseline = glyphs[gs[0] as usize].bbox.y0;
            for &g in &gs[1..] {
                let gl = glyphs[g as usize];
                bbox = bbox.union(&gl.bbox);
                baseline = baseline.min(gl.bbox.y0);
            }
            Some(Row {
                glyphs: gs,
                baseline_y: baseline,
                bbox,
            })
        })
        .collect();

    let mut groups: Vec<Vec<Row>> = Vec::new();
    for row in rows {
        let start_new = match groups.last() {
            None => true,
            Some(prev) => {
                let last = prev.last().expect("组内至少一行");
                !same_paragraph(last, &row, glyphs)
            }
        };
        if start_new {
            groups.push(vec![row]);
        } else {
            groups.last_mut().expect("刚判定过存在").push(row);
        }
    }
    groups
}

/// 判定当前行是否接续前一行（同一段）。
fn same_paragraph(prev: &Row, cur: &Row, glyphs: &[&syncpdf_core::ir::Glyph]) -> bool {
    let size = row_size(cur, glyphs);
    let gap = (prev.baseline_y - cur.baseline_y).abs();
    if gap >= PARAGRAPH_GAP_RATIO * size {
        return false;
    }
    // x 范围无重叠 → 不同段（跨栏 / 换块）。
    if !(prev.bbox.x0 < cur.bbox.x1 && cur.bbox.x0 < prev.bbox.x1) {
        return false;
    }
    // 首行缩进：本行起点明显右移 → 新段。
    let prev_x0 = glyphs[prev.glyphs[0] as usize].bbox.x0;
    let cur_x0 = glyphs[cur.glyphs[0] as usize].bbox.x0;
    if cur_x0 - prev_x0 >= INDENT_RATIO * size {
        return false;
    }
    true
}

/// 一行代表字号：行内字形字号的中位数。
fn row_size(row: &Row, glyphs: &[&syncpdf_core::ir::Glyph]) -> f32 {
    let mut sizes: Vec<f32> = row
        .glyphs
        .iter()
        .map(|&i| glyphs[i as usize].size)
        .filter(|s| *s > 0.0)
        .collect();
    if sizes.is_empty() {
        return 12.0;
    }
    sizes.sort_by(f32::total_cmp);
    sizes[sizes.len() / 2]
}

/// 一组行 → `Paragraph`。
fn build_paragraph(
    ir: &PageIR,
    region: &Region,
    rows: Vec<Row>,
    glyphs: &[&syncpdf_core::ir::Glyph],
    seq: u32,
) -> Paragraph {
    // 段内字形（按阅读顺序展开）。
    let mut seg_glyphs: Vec<GlyphId> = Vec::new();
    for row in &rows {
        for &i in &row.glyphs {
            seg_glyphs.push(glyphs[i as usize].id);
        }
    }

    let reading = read_source(&rows, glyphs);
    let style_runs = style_runs(&rows, glyphs, &ir.fonts);
    let atoms = detect_atoms(&reading.text, &reading.char_map);
    let bbox = rows.iter().fold(rows[0].bbox, |acc, r| acc.union(&r.bbox));
    let size = dominant_size(&rows, glyphs);
    let align = detect_align(&rows, &region.bbox);
    let first_indent = detect_first_indent(&rows, glyphs);
    let line_height = detect_line_height(&rows, size);

    let translatable = judge_translatable(region.kind, &reading.text, &atoms);

    Paragraph {
        id: ParagraphId::new(PageId(ir.page.0), seq),
        page: ir.page,
        region: region.index,
        kind: region.kind,
        bbox,
        lines: rows
            .iter()
            .map(|r| Line {
                glyphs: r.glyphs.iter().map(|&i| glyphs[i as usize].id).collect(),
                baseline_y: r.baseline_y,
                bbox: r.bbox,
            })
            .collect(),
        glyphs: seg_glyphs,
        text_spans: reading.spans,
        style_runs,
        atoms,
        text: reading.text,
        align,
        first_indent,
        line_height,
        is_rtl: false,
        translatable,
    }
}

struct SourceReading {
    text: String,
    spans: Vec<SourceTextSpan>,
    /// char starts followed by char ends; generated text uses a zero length range.
    char_map: Vec<u32>,
}

/// One traversal owns the logical text and every character's source interval.
fn read_source(rows: &[Row], glyphs: &[&syncpdf_core::ir::Glyph]) -> SourceReading {
    let mut text = String::new();
    let mut spans = Vec::new();
    let mut starts = Vec::new();
    let mut ends = Vec::new();
    let mut index = 0u32;
    let mut previous: Option<&syncpdf_core::ir::Glyph> = None;
    for (ri, row) in rows.iter().enumerate() {
        if ri > 0 {
            // A line boundary is genuine spacing unless the source already supplies it or
            // punctuation/CJK joins naturally. Preserve terminal hyphens verbatim.
            let next = glyphs[row.glyphs[0] as usize];
            if previous.is_some_and(|prev| should_join_with_space(prev, next, true)) {
                emit(
                    " ",
                    (index, index),
                    &mut text,
                    &mut spans,
                    &mut starts,
                    &mut ends,
                );
            }
            previous = None;
        }
        for &i in &row.glyphs {
            let g = glyphs[i as usize];
            if previous.is_some_and(|prev| should_join_with_space(prev, g, false)) {
                emit(
                    " ",
                    (index, index),
                    &mut text,
                    &mut spans,
                    &mut starts,
                    &mut ends,
                );
            }
            let glyph_text: String = g.unicode.iter().collect();
            if !glyph_text.is_empty() {
                emit(
                    &glyph_text,
                    (index, index + 1),
                    &mut text,
                    &mut spans,
                    &mut starts,
                    &mut ends,
                );
            }
            index += 1;
            previous = Some(g);
        }
    }
    starts.extend(ends);
    SourceReading {
        text,
        spans,
        char_map: starts,
    }
}

fn emit(
    text_part: &str,
    range: (u32, u32),
    text: &mut String,
    spans: &mut Vec<SourceTextSpan>,
    starts: &mut Vec<u32>,
    ends: &mut Vec<u32>,
) {
    text.push_str(text_part);
    spans.push(SourceTextSpan {
        text: text_part.into(),
        glyph_range: range,
    });
    for _ in text_part.chars() {
        starts.push(range.0);
        ends.push(range.1);
    }
}

fn should_join_with_space(
    prev: &syncpdf_core::ir::Glyph,
    next: &syncpdf_core::ir::Glyph,
    line_boundary: bool,
) -> bool {
    let Some(last) = prev.unicode.last().copied() else {
        return false;
    };
    let Some(first) = next.unicode.first().copied() else {
        return false;
    };
    if last.is_whitespace() || first.is_whitespace() || last == '-' || last == '\u{00ad}' {
        return false;
    }
    if is_cjk(last) || is_cjk(first) || !first.is_alphanumeric() {
        return false;
    }
    let word_end = last.is_alphanumeric() || matches!(last, ',' | '.' | ';' | ':' | '!' | '?');
    if !word_end {
        return false;
    }
    line_boundary || next.bbox.x0 - prev.bbox.x1 > prev.size.min(next.size).max(1.0) * 0.12
}

/// `[s, e)` char 区间 → 字形区间 `(gs, ge)`（`e` 为排他端）。
fn char_span_to_glyphs(map: &[u32], nchars: usize, s: usize, e: usize) -> (u32, u32) {
    let starts = &map[..nchars];
    let ends = &map[nchars..];
    let gs = starts
        .get(s)
        .copied()
        .unwrap_or_else(|| ends.last().copied().unwrap_or(0));
    let ge = if e == 0 {
        gs
    } else {
        ends.get(e - 1).copied().unwrap_or(glyph_total(map, nchars))
    };
    (gs, ge.max(gs))
}

/// `map` 里记录的字形总数（`end` 的最大值）。
fn glyph_total(map: &[u32], nchars: usize) -> u32 {
    map[nchars..].iter().copied().max().unwrap_or(0)
}

/// 按 (font, size 四舍五入 0.5, bold, italic, color) 切样式 run，`StyleId` 从 1 编。
///
/// 粗斜体取自 `PageIR::fonts[glyph.font]`（`FontRef::is_bold` / `is_italic`），
/// 字形本身只存字体下标。
fn style_runs(
    rows: &[Row],
    glyphs: &[&syncpdf_core::ir::Glyph],
    fonts: &[syncpdf_core::ir::FontRef],
) -> Vec<StyleRun> {
    #[derive(Clone, Copy, PartialEq, Eq)]
    struct Key {
        font: u32,
        size_half: i32,
        bold: bool,
        italic: bool,
        color: [u8; 3],
    }

    let key_of = |i: u32| -> Key {
        let g = glyphs[i as usize];
        let (bold, italic) = font_flags(fonts, g.font);
        Key {
            font: g.font,
            size_half: (g.size * 2.0).round() as i32,
            bold,
            italic,
            color: g.fill.to_rgb8(),
        }
    };

    let flat: Vec<u32> = rows.iter().flat_map(|r| r.glyphs.iter().copied()).collect();
    let mut runs: Vec<StyleRun> = Vec::new();
    let mut next_id: u32 = 1;
    let mut start = 0usize;
    while start < flat.len() {
        let k = key_of(flat[start]);
        let mut end = start + 1;
        while end < flat.len() && key_of(flat[end]) == k {
            end += 1;
        }
        let (bold, italic) = font_flags(fonts, glyphs[flat[start] as usize].font);
        runs.push(StyleRun {
            id: StyleId(next_id),
            glyph_range: (start as u32, end as u32),
            font: k.font,
            size: k.size_half as f32 / 2.0,
            color: glyphs[flat[start] as usize].fill,
            bold,
            italic,
        });
        next_id += 1;
        start = end;
    }
    runs
}

/// 原子识别：URL / 邮箱 / 引用标记 / 数字+单位 / 数学符号串。
fn detect_atoms(text: &str, char_map: &[u32]) -> Vec<Atom> {
    let chars: Vec<char> = text.chars().collect();
    let mut spans: Vec<(usize, usize, AtomKind)> = Vec::new();
    for re in regexes().iter() {
        for m in re.re.find_iter(text) {
            let start = text[..m.start()].chars().count();
            let end = text[..m.end()].chars().count();
            spans.push((start, end, re.kind));
        }
    }
    spans.sort_by_key(|(s, e, _)| (*s, *e));
    // 去重叠：先到先占（更长的整体匹配优先由正则顺序保证）。
    let mut taken: Vec<(usize, usize)> = Vec::new();
    let mut out: Vec<Atom> = Vec::new();
    let mut next_id: u32 = 1;
    for (s, e, kind) in spans {
        if taken.iter().any(|(ts, te)| s < *te && *ts < e) {
            continue;
        }
        taken.push((s, e));
        let text: String = chars[s..e].iter().collect();
        let (gs, ge) = char_span_to_glyphs(char_map, chars.len(), s, e);
        out.push(Atom {
            id: AtomId(next_id),
            glyph_range: (gs, ge),
            kind,
            text,
        });
        next_id += 1;
    }
    out.sort_by_key(|a| a.glyph_range.0);
    for (i, a) in out.iter_mut().enumerate() {
        a.id = AtomId(i as u32 + 1);
    }
    out
}

struct RegexDef {
    re: Regex,
    kind: AtomKind,
}

fn regexes() -> &'static [RegexDef] {
    static RES: OnceLock<Vec<RegexDef>> = OnceLock::new();
    RES.get_or_init(|| {
        let defs: [(&str, AtomKind); 5] = [
            (r"https?://\S+|www\.\S+", AtomKind::Url),
            (
                r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
                AtomKind::Other,
            ),
            (r"\[\d+(?:,\s*\d+)*\]", AtomKind::Other),
            (
                r"\d+(?:\.\d+)?\s?(?:%|mm|cm|kg|ms|Hz|GHz|MHz|nm|μm|px|pt|s)",
                AtomKind::Number,
            ),
            (r"[∑∫∂√±×÷≤≥≠≈∞αβγδθλμπσω]{2,}", AtomKind::Formula),
        ];
        defs.iter()
            .map(|(pat, kind)| RegexDef {
                re: Regex::new(pat).expect("内置正则在编译期已校验"),
                kind: *kind,
            })
            .collect()
    })
}

/// 可译判定。
fn judge_translatable(kind: RegionKind, text: &str, atoms: &[Atom]) -> Translatable {
    if !kind.translatable() {
        return Translatable::No {
            reason: "region_kind".into(),
        };
    }
    // 去掉原子覆盖的字符后再看还有没有字母 / 中日韩。
    let chars: Vec<char> = text.chars().collect();
    let mut removed = vec![false; chars.len()];
    for a in atoms {
        // 用原子文本回找位置（原子文本是原文本子串）。
        if let Some(pos) = find_subslice(&chars, &a.text.chars().collect::<Vec<_>>()) {
            let end = (pos + a.text.chars().count()).min(chars.len());
            for slot in removed.iter_mut().take(end).skip(pos) {
                *slot = true;
            }
        }
    }
    let rest: String = chars
        .iter()
        .enumerate()
        .filter(|(i, _)| !removed[*i])
        .map(|(_, c)| *c)
        .collect();
    let has_letters = rest.chars().any(|c| c.is_alphabetic() || is_cjk(c));
    if !has_letters {
        return Translatable::No {
            reason: "no_letters".into(),
        };
    }
    let meaningful = rest.chars().filter(|c| !c.is_whitespace()).count();
    if meaningful < MIN_TEXT_CHARS {
        return Translatable::No {
            reason: "too_short".into(),
        };
    }
    Translatable::Yes
}

fn find_subslice(hay: &[char], needle: &[char]) -> Option<usize> {
    if needle.is_empty() || needle.len() > hay.len() {
        return None;
    }
    (0..=hay.len() - needle.len()).find(|&i| hay[i..i + needle.len()] == *needle)
}

fn is_cjk(c: char) -> bool {
    matches!(c as u32,
        0x3000..=0x303F | 0x2E80..=0x9FFF | 0xAC00..=0xD7FF | 0xF900..=0xFAFF
            | 0xFF00..=0xFF60)
}

/// 对齐判定：居中 / 两端对齐 / 左对齐。
///
/// 边距相对**区域框**度量（行相对区域左右两侧的留白）：
/// - 居中：每行左右边距都 > 3pt，且同一行的左右边距差 < 2pt；
/// - 两端对齐：各行左端（±1pt）与右端（±1pt）都齐；
/// - 其余 → 左对齐。单行段一律 Left。
fn detect_align(rows: &[Row], region: &Rect) -> Align {
    if rows.len() == 1 {
        return Align::Left;
    }
    let left_aligned = rows
        .iter()
        .all(|r| (r.bbox.x0 - rows[0].bbox.x0).abs() <= ALIGN_TOL);
    let max_right = rows
        .iter()
        .map(|r| r.bbox.x1)
        .fold(f32::NEG_INFINITY, f32::max);
    let right_aligned = rows
        .iter()
        .all(|r| (max_right - r.bbox.x1).abs() <= ALIGN_TOL);

    let centered = rows.iter().all(|r| {
        let ml = r.bbox.x0 - region.x0;
        let mr = region.x1 - r.bbox.x1;
        ml > CENTER_MIN_MARGIN && mr > CENTER_MIN_MARGIN && (ml - mr).abs() < 2.0
    });
    if centered {
        return Align::Center;
    }
    if left_aligned && right_aligned {
        return Align::Justify;
    }
    Align::Left
}

/// 首行缩进：首行首字形 x − 其余行最小首字形 x（负数取 0）。
fn detect_first_indent(rows: &[Row], glyphs: &[&syncpdf_core::ir::Glyph]) -> f32 {
    if rows.len() < 2 {
        return 0.0;
    }
    let x_of = |r: &Row| glyphs[r.glyphs[0] as usize].bbox.x0;
    let first = x_of(&rows[0]);
    let rest_min = rows[1..].iter().map(x_of).fold(f32::INFINITY, f32::min);
    (first - rest_min).max(0.0)
}

/// 行距：相邻基线差的中位数；单行段 = 1.2×字号。
fn detect_line_height(rows: &[Row], size: f32) -> f32 {
    if rows.len() < 2 {
        return 1.2 * size;
    }
    let mut gaps: Vec<f32> = rows
        .windows(2)
        .map(|w| (w[0].baseline_y - w[1].baseline_y).abs())
        .collect();
    gaps.sort_by(f32::total_cmp);
    gaps[gaps.len() / 2]
}

/// 段的主字号：全部字形字号的中位数。
fn dominant_size(rows: &[Row], glyphs: &[&syncpdf_core::ir::Glyph]) -> f32 {
    let mut sizes: Vec<f32> = rows
        .iter()
        .flat_map(|r| r.glyphs.iter())
        .map(|&i| glyphs[i as usize].size)
        .filter(|s| *s > 0.0)
        .collect();
    if sizes.is_empty() {
        return 12.0;
    }
    sizes.sort_by(f32::total_cmp);
    sizes[sizes.len() / 2]
}

/// 从 `PageIR::fonts` 读字体的粗斜体标志；下标越界按非粗非斜处理。
fn font_flags(fonts: &[syncpdf_core::ir::FontRef], font: u32) -> (bool, bool) {
    fonts
        .get(font as usize)
        .map(|f| (f.is_bold, f.is_italic))
        .unwrap_or((false, false))
}

#[cfg(test)]
mod tests;
