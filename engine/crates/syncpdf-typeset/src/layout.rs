//! Cluster-safe paragraph layout using Knuth–Plass and actual glyph ink.

use crate::breaks::{break_opportunities, is_forbidden_line_end, is_forbidden_line_start, Lang};
use crate::fit::Inline;
use crate::knuth_plass::{self, Node};
use crate::shaper::{is_cjk_char, ShapedGlyph, Shaper, StyleSpec, UnderlineStyle};
use syncpdf_core::ir::{Align, LineBox, PlacedGlyph, TypesetParagraph, Underline};
use syncpdf_core::{AtomId, Color, ParagraphId, Rect, StyleId};
use unicode_bidi::BidiInfo;

pub(crate) struct LayoutInput<'a> {
    pub id: ParagraphId,
    pub font_size: f32,
    pub first_baseline: Option<f32>,
    pub align: Align,
    pub first_indent: f32,
    pub is_rtl: bool,
    pub color: Color,
    pub lang: Lang,
    pub styles: &'a [(StyleId, StyleSpec)],
}

pub(crate) struct LayoutOut {
    pub paragraph: TypesetParagraph,
    pub scale: f32,
    pub lines: usize,
    pub line_capacity: f32,
    pub overflowed: bool,
}

#[derive(Clone)]
enum Item {
    Cluster {
        glyphs: Vec<ShapedGlyph>,
        text: String,
        size: f32,
        style: StyleId,
        width: f32,
        start: usize,
        end: usize,
    },
    Atom {
        source: Option<syncpdf_core::ir::SourceAtom>,
        id: AtomId,
        width: f32,
        height: f32,
        start: usize,
        end: usize,
    },
}

impl Item {
    fn width(&self) -> f32 {
        match self {
            Self::Cluster { width, .. } | Self::Atom { width, .. } => *width,
        }
    }
    fn start(&self) -> usize {
        match self {
            Self::Cluster { start, .. } | Self::Atom { start, .. } => *start,
        }
    }
    fn end(&self) -> usize {
        match self {
            Self::Cluster { end, .. } | Self::Atom { end, .. } => *end,
        }
    }
    fn space(&self) -> bool {
        matches!(self, Self::Cluster { text, .. } if text.chars().all(char::is_whitespace))
    }
}

#[derive(Default)]
pub(crate) struct BreaksCache {
    segments: std::cell::OnceCell<Vec<Segment>>,
}
impl BreaksCache {
    pub(crate) fn new() -> Self {
        Self::default()
    }
}

#[derive(Clone, Default)]
struct Segment {
    text: String,
    items: Vec<Item>,
}

fn spec(input: &LayoutInput<'_>, style: StyleId) -> StyleSpec {
    input
        .styles
        .iter()
        .find(|(id, _)| *id == style)
        .map_or(StyleSpec::default(), |(_, s)| *s)
}

fn segments(shaper: &dyn Shaper, input: &LayoutInput<'_>, inlines: &[Inline]) -> Vec<Segment> {
    let mut result = vec![Segment::default()];
    for inline in inlines {
        if matches!(inline, Inline::Br) {
            result.push(Segment::default());
            continue;
        }
        let seg = result.last_mut().expect("segment exists");
        match inline {
            Inline::Atom { id, width, height } => {
                let start = seg.text.len();
                seg.text.push('\u{FFFC}');
                seg.items.push(Item::Atom {
                    source: None,
                    id: *id,
                    width: *width,
                    height: *height,
                    start,
                    end: seg.text.len(),
                });
            }
            Inline::SourceAtom { id, source } => {
                let start = seg.text.len();
                seg.text.push('\u{FFFC}');
                seg.items.push(Item::Atom {
                    id: *id,
                    source: Some(*source),
                    width: source.advance.unwrap_or(source.bbox.width()),
                    height: source.bbox.height(),
                    start,
                    end: seg.text.len(),
                });
            }
            Inline::Text { text, style } => {
                if text.is_empty() {
                    continue;
                }
                let s = spec(input, *style);
                let font = s.font.unwrap_or_else(|| shaper.font_for(&s));
                let size = s.size.unwrap_or(input.font_size);
                let base = seg.text.len();
                seg.text.push_str(text);
                // Shape directional logical runs. The shaper itself emits visual glyph order.
                let bidi = BidiInfo::new(text, None);
                let mut runs: Vec<(std::ops::Range<usize>, bool)> = Vec::new();
                if let Some(para) = bidi.paragraphs.first() {
                    let (levels, visual) = bidi.visual_runs(para, para.range.clone());
                    for run in visual {
                        if run.start < run.end
                            && text.is_char_boundary(run.start)
                            && text.is_char_boundary(run.end)
                        {
                            runs.push((run.clone(), levels[run.start].is_rtl()));
                        }
                    }
                }
                if runs.is_empty() {
                    runs.push((0..text.len(), input.is_rtl));
                }
                runs.sort_by_key(|(range, _)| range.start);
                for (range, rtl) in runs {
                    let part = &text[range.clone()];
                    let glyphs = shaper.shape_styled(font, &s, part, size, rtl);
                    let mut groups: std::collections::BTreeMap<(usize, usize), Vec<ShapedGlyph>> =
                        std::collections::BTreeMap::new();
                    for glyph in glyphs {
                        let a = glyph.cluster as usize;
                        let b = glyph.cluster_end as usize;
                        if a < b
                            && b <= part.len()
                            && part.is_char_boundary(a)
                            && part.is_char_boundary(b)
                        {
                            groups.entry((a, b)).or_default().push(glyph);
                        }
                    }
                    for ((a, b), glyphs) in groups {
                        let width = glyphs.iter().map(|g| g.x_advance).sum();
                        seg.items.push(Item::Cluster {
                            glyphs,
                            text: part[a..b].to_owned(),
                            size,
                            style: *style,
                            width,
                            start: base + range.start + a,
                            end: base + range.start + b,
                        });
                    }
                }
            }
            Inline::Br => unreachable!(),
        }
    }
    result
}

fn scaled(base: &Segment, scale: f32) -> Segment {
    let mut seg = base.clone();
    for item in &mut seg.items {
        if let Item::Cluster {
            glyphs,
            size,
            width,
            ..
        } = item
        {
            *size *= scale;
            *width *= scale;
            for g in glyphs {
                g.x_advance *= scale;
                g.x_offset *= scale;
                g.y_offset *= scale;
            }
        }
    }
    seg
}

#[derive(Clone)]
struct Row {
    items: Vec<Item>,
    ratio: f32,
    hyphen: Option<Item>,
    first: bool,
    last: bool,
    cjk_glue: Vec<usize>,
    space_glue: Vec<usize>,
}

fn rows(
    shaper: &dyn Shaper,
    input: &LayoutInput<'_>,
    segments: &[Segment],
    width: f32,
    scale: f32,
) -> (Vec<Row>, bool) {
    let mut out = Vec::new();
    let mut failed = false;
    for base in segments {
        let seg = scaled(base, scale);
        if seg.items.is_empty() {
            // Explicit Br retains empty lines; a completely empty paragraph is rejected later.
            if segments.len() > 1 {
                out.push(Row {
                    items: Vec::new(),
                    ratio: 0.0,
                    hyphen: None,
                    first: out.is_empty(),
                    last: true,
                    cjk_glue: Vec::new(),
                    space_glue: Vec::new(),
                });
            }
            continue;
        }
        let opps = break_opportunities(&seg.text, input.lang);
        // Do not strand one CJK letter (plus closing punctuation) on the last
        // line. Explicit hard breaks remain authoritative.
        let mut letters = seg
            .text
            .char_indices()
            .rev()
            .filter(|(_, c)| c.is_alphanumeric());
        let lone_tail = letters
            .next()
            .filter(|(_, c)| is_cjk_char(*c))
            .map(|(last, _)| (letters.next().map(|(previous, _)| previous), last));
        let starts: std::collections::HashSet<usize> = seg.items.iter().map(Item::start).collect();
        let ends: std::collections::HashSet<usize> = seg.items.iter().map(Item::end).collect();
        let mut at: std::collections::HashMap<usize, (bool, bool)> =
            std::collections::HashMap::new();
        for opp in opps {
            let byte = opp.byte as usize;
            if !opp.mandatory
                && lone_tail.is_some_and(|(previous, last)| {
                    previous.is_some_and(|previous| previous < byte) && byte <= last
                })
            {
                continue;
            }
            if byte < seg.text.len() && ends.contains(&byte) && starts.contains(&byte) {
                let entry = at.entry(byte).or_insert((false, false));
                entry.0 |= opp.mandatory;
                entry.1 |= opp.soft_hyphen;
            }
        }
        let mut nodes = Vec::new();
        let mut mapping = Vec::new();
        let mut hyphens: std::collections::HashMap<usize, Item> = std::collections::HashMap::new();
        let mut cjk_glue = Vec::new();
        let mut space_glue = Vec::new();
        for (i, item) in seg.items.iter().enumerate() {
            let next = seg.items.get(i + 1);
            if item.space() && at.contains_key(&item.end()) && i > 0 && !seg.items[i - 1].space() {
                // A ragged first line can end before its only interword space.
                nodes.push(Node::Penalty {
                    width: 0.0,
                    cost: 50,
                    flagged: false,
                });
                mapping.push(None);
            }
            if item.space() && at.contains_key(&item.end()) {
                let w = item.width().max(0.0);
                nodes.push(Node::Glue {
                    width: w,
                    stretch: w.max(1.0) * 4.0,
                    shrink: if input.align == Align::Justify {
                        w
                    } else {
                        0.0
                    },
                });
                space_glue.push(item.end());
            } else {
                nodes.push(Node::Box {
                    width: item.width().max(0.0),
                });
            }
            mapping.push(Some(i));
            if let Some(next) = next {
                if next.start() != item.end() {
                    continue;
                }
                if let Some(&(mandatory, soft)) = at.get(&item.end()) {
                    if item.space() {
                        continue;
                    }
                    if soft {
                        if let Item::Cluster {
                            glyphs,
                            size,
                            style,
                            ..
                        } = item
                        {
                            let font = glyphs
                                .last()
                                .map_or_else(|| shaper.font_for(&spec(input, *style)), |g| g.font);
                            let hy =
                                shaper.shape_styled(font, &spec(input, *style), "-", *size, false);
                            if !hy.is_empty() {
                                let w = hy.iter().map(|g| g.x_advance).sum();
                                hyphens.insert(
                                    nodes.len(),
                                    Item::Cluster {
                                        glyphs: hy,
                                        text: "-".into(),
                                        size: *size,
                                        style: *style,
                                        width: w,
                                        start: item.end(),
                                        end: item.end(),
                                    },
                                );
                                nodes.push(Node::Penalty {
                                    width: w,
                                    cost: 100,
                                    flagged: true,
                                });
                                mapping.push(None);
                            }
                        }
                    } else if mandatory {
                        nodes.push(Node::Penalty {
                            width: 0.0,
                            cost: -10_000,
                            flagged: false,
                        });
                        mapping.push(None);
                    } else if input.lang.is_cjk()
                        && matches!((item, next), (Item::Cluster { text: a, .. }, Item::Cluster { text: b, .. }) if a.chars().any(is_cjk_char) && b.chars().any(is_cjk_char) && !a.chars().any(is_forbidden_line_end) && !b.chars().any(is_forbidden_line_start))
                    {
                        // Legal CJK tracking is adjustable glue, never a split through a cluster.
                        nodes.push(Node::Glue {
                            width: 0.0,
                            stretch: input.font_size * scale * 0.05,
                            shrink: 0.0,
                        });
                        mapping.push(None);
                        cjk_glue.push(item.end());
                    } else {
                        nodes.push(Node::Penalty {
                            width: 0.0,
                            cost: 0,
                            flagged: false,
                        });
                        mapping.push(None);
                    }
                }
            }
        }
        let measure = width
            - if out.is_empty() {
                input.first_indent.max(0.0)
            } else {
                0.0
            };
        let widths = [measure, width];
        let solution = if measure > 0.0 && width > 0.0 {
            knuth_plass::solve_aligned(&nodes, &widths, 10.0, input.align == Align::Justify).ok()
        } else {
            None
        };
        if let Some(solution) = solution {
            let n = solution.lines.len();
            for (li, line) in solution.lines.iter().enumerate() {
                let items: Vec<Item> = (line.start..line.end)
                    .filter_map(|j| mapping[j].map(|i| seg.items[i].clone()))
                    .collect();
                let first = items.first().map_or(0, Item::start);
                let end = items.last().map_or(0, Item::end);
                let cjk = cjk_glue
                    .iter()
                    .copied()
                    .filter(|b| *b > first && *b < end)
                    .collect();
                let spaces = space_glue
                    .iter()
                    .copied()
                    .filter(|b| *b > first && *b < end)
                    .collect();
                out.push(Row {
                    items,
                    ratio: line.ratio,
                    hyphen: hyphens.get(&line.break_at).cloned(),
                    first: out.is_empty(),
                    last: li + 1 == n,
                    cjk_glue: cjk,
                    space_glue: spaces,
                });
            }
        } else {
            failed = true;
            out.push(Row {
                items: seg.items,
                ratio: 0.0,
                hyphen: None,
                first: out.is_empty(),
                last: true,
                cjk_glue: Vec::new(),
                space_glue: Vec::new(),
            });
        }
    }
    (out, failed)
}

fn visual_items(row: &Row, rtl: bool) -> Vec<Item> {
    if row.items.is_empty() {
        return Vec::new();
    }
    let text: String = row
        .items
        .iter()
        .map(|i| match i {
            Item::Cluster { text, .. } => text.as_str(),
            Item::Atom { .. } => "\u{FFFC}",
        })
        .collect();
    let bidi = BidiInfo::new(&text, None);
    let Some(para) = bidi.paragraphs.first() else {
        return row.items.clone();
    };
    let (levels, runs) = bidi.visual_runs(para, para.range.clone());
    let mut offsets = Vec::with_capacity(row.items.len());
    let mut pos = 0;
    for item in &row.items {
        let len = match item {
            Item::Cluster { text, .. } => text.len(),
            Item::Atom { .. } => 3,
        };
        offsets.push((pos, pos + len));
        pos += len;
    }
    let mut result = Vec::new();
    for run in runs {
        let mut part: Vec<_> = row
            .items
            .iter()
            .zip(&offsets)
            .filter(|(_, (a, b))| *a >= run.start && *b <= run.end)
            .map(|(item, _)| item.clone())
            .collect();
        if levels.get(run.start).is_some_and(|l| l.is_rtl()) {
            part.reverse();
        }
        result.extend(part);
    }
    if result.len() == row.items.len() {
        result
    } else if rtl {
        row.items.iter().rev().cloned().collect()
    } else {
        row.items.clone()
    }
}

fn ink(shaper: &dyn Shaper, g: &PlacedGlyph, advance: f32) -> Rect {
    let b = shaper
        .glyph_bounds(g.font, g.gid, g.size)
        .unwrap_or_else(|| {
            let m = shaper.metrics(g.font);
            Rect::new(
                0.0,
                -m.descent * g.size,
                advance.max(0.0),
                m.ascent * g.size,
            )
        });
    Rect::new(g.x + b.x0, g.y + b.y0, g.x + b.x1, g.y + b.y1)
}

struct PlacedLine {
    line: LineBox,
    /// Non-whitespace glyph ink (or conservative metrics) and atom rectangles.
    ink: Vec<Rect>,
}

fn intersects(a: Rect, b: Rect) -> bool {
    a.x1.min(b.x1) - a.x0.max(b.x0) > 0.01 && a.y1.min(b.y1) - a.y0.max(b.y0) > 0.01
}

fn place(
    shaper: &dyn Shaper,
    input: &LayoutInput<'_>,
    row: &Row,
    bbox: &Rect,
    baseline: f32,
    scale: f32,
) -> PlacedLine {
    let placed = place_row(shaper, input, row, bbox, baseline, scale);
    let available = bbox.width()
        - if row.first {
            input.first_indent.max(0.0)
        } else {
            0.0
        };
    // Justification positions advances; serif ink and side bearings can extend
    // past them. Spend only as much less glue as the ink needs, preserving every
    // glyph size and the chosen breaks: first give back added stretch, then use
    // the breaker's own shrink allowance (never beyond its limit, ratio -1).
    if input.align == Align::Justify && !row.last && placed.line.bbox.width() > available {
        let at = |ratio: f32| {
            let mut adjusted = row.clone();
            adjusted.ratio = ratio;
            place_row(shaper, input, &adjusted, bbox, baseline, scale)
        };
        let mut high = (row.ratio, placed.line.bbox.width());
        for low in [0.0, -1.0] {
            if low >= high.0 {
                continue;
            }
            let width = at(low).line.bbox.width();
            if width <= available {
                let t = if high.1 > width {
                    ((available - width - 0.001) / (high.1 - width)).clamp(0.0, 1.0)
                } else {
                    0.0
                };
                return at(low + (high.0 - low) * t);
            }
            high = (low, width);
        }
    }
    placed
}

fn place_row(
    shaper: &dyn Shaper,
    input: &LayoutInput<'_>,
    row: &Row,
    bbox: &Rect,
    baseline: f32,
    scale: f32,
) -> PlacedLine {
    let items = visual_items(row, input.is_rtl);
    let indent = if row.first {
        input.first_indent.max(0.0)
    } else {
        0.0
    };
    let natural: f32 =
        items.iter().map(Item::width).sum::<f32>() + row.hyphen.as_ref().map_or(0.0, Item::width);
    let available = bbox.width() - indent;
    let dx = match input.align {
        Align::Center => (available - natural) / 2.0,
        Align::Right => available - natural,
        _ => 0.0,
    };
    let mut x = bbox.x0 + indent + dx;
    let mut glyphs = Vec::new();
    let mut atoms = Vec::new();
    let mut placed_atoms = Vec::new();
    let mut ink_boxes = Vec::new();
    // Parallel to `ink_boxes`: the source-evidenced underline style, if any, so
    // the decoration is drawn from the translated glyphs' real ink.
    let mut underline_for: Vec<Option<UnderlineStyle>> = Vec::new();
    let mut bounds: Option<Rect> = None;
    let adjust = input.align == Align::Justify && !row.last;
    // In a row with CJK tracking, interword spaces and CJK gaps are equal
    // justification opportunities: the breaker's stretch is kept for choosing
    // breaks, but the resulting slack is spread evenly so a few Latin spaces
    // do not absorb it (a space's stretch is ~80x one CJK gap's).
    let even_stretch = (adjust && row.ratio > 0.0 && !row.cjk_glue.is_empty()).then(|| {
        let spaces: f32 = items
            .iter()
            .filter(|i| row.space_glue.contains(&i.end()))
            .map(|i| i.width().max(1.0) * 4.0)
            .sum();
        let total = spaces + row.cjk_glue.len() as f32 * input.font_size * scale * 0.05;
        row.ratio * total / (row.space_glue.len() + row.cjk_glue.len()) as f32
    });

    for item in items.iter().chain(row.hyphen.iter()) {
        match item {
            Item::Cluster {
                glyphs: shaped,
                text,
                size,
                style,
                ..
            } => {
                let mut first = true;
                let rise = spec(input, *style).rise * *size;
                for g in shaped {
                    let placed = PlacedGlyph {
                        font: g.font,
                        gid: g.gid,
                        text: if first { text.clone() } else { String::new() },
                        x: x + g.x_offset,
                        y: baseline + rise + g.y_offset,
                        size: *size,
                        scale_x: 1.0,
                        // 合成斜体按片段携带；剪切不改变 advance/断行（见模块文档）。
                        shear_x: g.shear_x,
                        style: *style,
                        color: spec(input, *style).color,
                    };
                    first = false;
                    if text.is_empty() || !text.chars().all(char::is_whitespace) {
                        let rect = ink(shaper, &placed, g.x_advance);
                        ink_boxes.push(rect);
                        underline_for.push(spec(input, *style).underline);
                        bounds = Some(bounds.map_or(rect, |b| b.union(&rect)));
                    }
                    glyphs.push(placed);
                    x += g.x_advance;
                }
                if adjust && row.space_glue.contains(&item.end()) {
                    x += even_stretch.unwrap_or_else(|| {
                        row.ratio
                            * if row.ratio >= 0.0 {
                                item.width().max(1.0) * 4.0
                            } else {
                                item.width()
                            }
                    });
                }
                if adjust && row.cjk_glue.contains(&item.end()) {
                    x += even_stretch.unwrap_or(row.ratio * input.font_size * scale * 0.05);
                }
            }
            Item::Atom {
                id,
                width,
                height,
                source,
                ..
            } => {
                let bottom = baseline + source.map_or(0.0, |s| s.bbox.y0 - s.baseline);
                let rect = Rect::new(x, bottom, x + *width, bottom + *height);
                if let Some(source) = source {
                    placed_atoms.push(syncpdf_core::ir::PlacedAtom {
                        id: *id,
                        source: source.bbox,
                        bbox: rect,
                    });
                }
                ink_boxes.push(rect);
                // Atoms keep their own source drawing; they carry no underline.
                underline_for.push(None);
                bounds = Some(bounds.map_or(rect, |b| b.union(&rect)));
                atoms.push(*id);
                x += *width;
            }
        }
    }
    // Side bearings are ink, not advance: translate a line that fits in full back
    // inside either edge. Never shrink, clip, or relax collision tolerances.
    if matches!(input.align, Align::Left | Align::Justify) && atoms.len() == placed_atoms.len() {
        if let Some(b) = bounds {
            let left = bbox.x0 + indent;
            let dx = if b.x0 < left {
                left - b.x0
            } else {
                (bbox.x1 - b.x1).min(0.0)
            };
            if dx != 0.0 && b.x0 + dx >= left && b.x1 + dx <= bbox.x1 {
                for g in &mut glyphs {
                    g.x += dx;
                }
                for ink in &mut ink_boxes {
                    ink.x0 += dx;
                    ink.x1 += dx;
                }
                // Source drawings move rigidly with their line; the immutable
                // source rectangle and original dimensions are unchanged.
                for atom in &mut placed_atoms {
                    atom.bbox.x0 += dx;
                    atom.bbox.x1 += dx;
                }
                bounds = Some(Rect::new(b.x0 + dx, b.y0, b.x1 + dx, b.y1));
            }
        }
    }
    let underlines = underlines(baseline, &ink_boxes, &underline_for);
    for underline in &underlines {
        ink_boxes.push(underline.bbox);
        bounds = Some(bounds.map_or(underline.bbox, |b| b.union(&underline.bbox)));
    }
    PlacedLine {
        line: LineBox {
            bbox: bounds.unwrap_or(Rect::new(
                bbox.x0 + indent,
                baseline,
                bbox.x0 + indent,
                baseline,
            )),
            baseline_y: baseline,
            glyphs,
            kept_atoms: atoms,
            placed_atoms,
            underlines,
        },
        ink: ink_boxes,
    }
}

/// Group consecutive decorated ink into underline segments under their baseline.
///
/// Geometry comes from the *translated* ink boxes, so wraps produce one segment
/// per line and a run that spans a line break draws on both lines. Width, offset
/// and color are the claimed source values; nothing is inferred.
fn underlines(baseline: f32, ink: &[Rect], styles: &[Option<UnderlineStyle>]) -> Vec<Underline> {
    let mut out: Vec<Underline> = Vec::new();
    let mut group: Option<(UnderlineStyle, Rect)> = None;
    for (box_, style) in ink.iter().zip(styles) {
        match (style, group) {
            (Some(style), Some((current, span))) if current == *style => {
                group = Some((current, span.union(box_)));
            }
            (Some(style), existing) => {
                if let Some((current, span)) = existing {
                    out.push(segment(current, span, baseline));
                }
                group = Some((*style, *box_));
            }
            (None, Some((current, span))) => {
                out.push(segment(current, span, baseline));
                group = None;
            }
            (None, None) => {}
        }
    }
    if let Some((current, span)) = group {
        out.push(segment(current, span, baseline));
    }
    out.retain(|u| u.bbox.width() > 0.0);
    out
}

/// One underline segment: it spans the decorated ink and sits at the claimed
/// offset below the line's baseline.
fn segment(style: UnderlineStyle, span: Rect, baseline: f32) -> Underline {
    let y = baseline - style.offset;
    let half = (style.width * 0.5).max(0.01);
    Underline {
        bbox: Rect::new(span.x0, y - half, span.x1, y + half),
        color: style.color,
        width: style.width,
    }
}

pub(crate) fn layout(
    shaper: &dyn Shaper,
    input: &LayoutInput<'_>,
    inlines: &[Inline],
    bbox: &Rect,
    scale: f32,
    line_height_mult: f32,
    cache: &BreaksCache,
) -> LayoutOut {
    let valid = [bbox.x0, bbox.y0, bbox.x1, bbox.y1]
        .into_iter()
        .all(f32::is_finite)
        && bbox.width().is_finite()
        && bbox.height().is_finite()
        && bbox.width() > 0.0
        && bbox.height() > 0.0
        && input.font_size.is_finite()
        && input.font_size > 0.0
        && input.first_indent.is_finite()
        && input.first_baseline.is_none_or(f32::is_finite)
        && input
            .styles
            .iter()
            .all(|(_, style)| style.size.is_none_or(|size| size.is_finite() && size > 0.0))
        && scale.is_finite()
        && scale > 0.0
        && line_height_mult.is_finite()
        && line_height_mult > 0.0;
    if !valid {
        return LayoutOut {
            paragraph: TypesetParagraph {
                id: input.id.clone(),
                lines: Vec::new(),
                font_scale: scale,
                line_height: 0.0,
                color: input.color,
                used_bbox: *bbox,
                overflow: true,
            },
            scale,
            lines: 0,
            line_capacity: 0.0,
            overflowed: true,
        };
    }
    let base = cache
        .segments
        .get_or_init(|| segments(shaper, input, inlines));
    let (rows, failed) = rows(shaper, input, base, bbox.width(), scale);
    let line_h = input.font_size * scale * line_height_mult;
    let mut lines: Vec<PlacedLine> = Vec::with_capacity(rows.len());
    let mut used: Option<Rect> = None;
    let mut overflow = !valid || failed || (rows.is_empty() && !inlines.is_empty());
    let first_top = rows
        .first()
        .map(|row| place(shaper, input, row, bbox, 0.0, scale).line.bbox.y1)
        .unwrap_or(0.0);
    let first_baseline = input.first_baseline.unwrap_or(bbox.y1 - first_top);
    for row in &rows {
        let advance: f32 = row.items.iter().map(Item::width).sum::<f32>()
            + row.hyphen.as_ref().map_or(0.0, Item::width);
        if advance
            > bbox.width()
                - if row.first {
                    input.first_indent.max(0.0)
                } else {
                    0.0
                }
                + 0.01
            && (input.align != Align::Justify || row.ratio >= 0.0)
        {
            overflow = true;
        }
        let mut baseline = lines
            .last()
            .map_or(first_baseline, |l| l.line.baseline_y - line_h);
        let mut line = place(shaper, input, row, bbox, baseline, scale);
        // Source formulas retain their full superscript/subscript ink. The requested
        // leading is a minimum: spend only the extra space their actual ink needs.
        let extra = lines
            .iter()
            .filter(|previous| {
                !previous.line.placed_atoms.is_empty() || !line.line.placed_atoms.is_empty()
            })
            .flat_map(|previous| {
                previous.ink.iter().flat_map(|a| {
                    line.ink
                        .iter()
                        .filter_map(move |b| (intersects(*a, *b)).then_some(b.y1 - a.y0 + 0.02))
                })
            })
            .fold(0.0_f32, f32::max);
        if extra > 0.0 {
            baseline -= extra;
            line = place(shaper, input, row, bbox, baseline, scale);
        }
        let b = line.line.bbox;
        if b.x0 < bbox.x0 - 0.01
            || b.x1 > bbox.x1 + 0.01
            || b.y0 < bbox.y0 - 0.01
            || b.y1 > bbox.y1 + 0.01
        {
            overflow = true;
        }
        // Union boxes may overlap when a descender and an ascender lie at
        // different x positions. Only actual glyph/atom rectangles prove a
        // collision; keep the same tolerance, size, baselines and line spacing.
        if lines.iter().any(|previous| {
            intersects(previous.line.bbox, b)
                && previous
                    .ink
                    .iter()
                    .any(|a| line.ink.iter().any(|b| intersects(*a, *b)))
        }) {
            overflow = true;
        }
        used = Some(used.map_or(b, |u| u.union(&b)));
        lines.push(line);
    }
    if inlines.is_empty()
        || base.iter().any(|s| {
            let mut end = 0;
            let mut complete = true;
            for item in &s.items {
                if item.start() != end || item.end() <= end {
                    complete = false;
                }
                end = item.end();
            }
            !complete
                || end != s.text.len()
                || s.items.iter().any(|i| match i {
                    Item::Cluster {
                        glyphs,
                        size,
                        width,
                        ..
                    } => {
                        glyphs.is_empty()
                            || glyphs.iter().any(|g| {
                                g.gid == 0
                                    || !g.x_advance.is_finite()
                                    || !g.x_offset.is_finite()
                                    || !g.y_offset.is_finite()
                            })
                            || !size.is_finite()
                            || *size <= 0.0
                            || !width.is_finite()
                            || *width < 0.0
                    }
                    Item::Atom { width, height, .. } => {
                        !width.is_finite() || !height.is_finite() || *width < 0.0 || *height < 0.0
                    }
                })
        })
    {
        overflow = true;
    }
    let capacity = if line_h > 0.0 {
        (bbox.height() / line_h).floor()
    } else {
        0.0
    };
    LayoutOut {
        paragraph: TypesetParagraph {
            id: input.id.clone(),
            lines: lines.into_iter().map(|placed| placed.line).collect(),
            font_scale: scale,
            line_height: line_h,
            color: input.color,
            used_bbox: used.unwrap_or(*bbox),
            overflow,
        },
        scale,
        lines: rows.len(),
        line_capacity: capacity,
        overflowed: overflow,
    }
}
