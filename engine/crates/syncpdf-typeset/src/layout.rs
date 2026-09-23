//! Cluster-safe paragraph layout using Knuth–Plass and actual glyph ink.

use crate::breaks::{break_opportunities, is_forbidden_line_end, is_forbidden_line_start, Lang};
use crate::fit::Inline;
use crate::knuth_plass::{self, Node};
use crate::shaper::{is_cjk_char, ShapedGlyph, Shaper, StyleSpec};
use syncpdf_core::ir::{Align, LineBox, PlacedGlyph, TypesetParagraph};
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
                    id: *id,
                    width: *width,
                    height: *height,
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
                    let glyphs = shaper.shape(font, part, size, rtl);
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
        let starts: std::collections::HashSet<usize> = seg.items.iter().map(Item::start).collect();
        let ends: std::collections::HashSet<usize> = seg.items.iter().map(Item::end).collect();
        let mut at: std::collections::HashMap<usize, (bool, bool)> =
            std::collections::HashMap::new();
        for opp in opps {
            let byte = opp.byte as usize;
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
                            let hy = shaper.shape(font, "-", *size, false);
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
            knuth_plass::solve(&nodes, &widths, 10.0).ok()
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
    let mut ink_boxes = Vec::new();
    let mut bounds: Option<Rect> = None;
    let adjust = input.align == Align::Justify && !row.last;

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
                for g in shaped {
                    let placed = PlacedGlyph {
                        font: g.font,
                        gid: g.gid,
                        text: if first { text.clone() } else { String::new() },
                        x: x + g.x_offset,
                        y: baseline + g.y_offset,
                        size: *size,
                        scale_x: 1.0,
                        style: *style,
                        color: spec(input, *style).color,
                    };
                    first = false;
                    if !text.chars().all(char::is_whitespace) {
                        let rect = ink(shaper, &placed, g.x_advance);
                        ink_boxes.push(rect);
                        bounds = Some(bounds.map_or(rect, |b| b.union(&rect)));
                    }
                    glyphs.push(placed);
                    x += g.x_advance;
                }
                if adjust && row.space_glue.contains(&item.end()) {
                    x += row.ratio
                        * if row.ratio >= 0.0 {
                            item.width().max(1.0) * 4.0
                        } else {
                            item.width()
                        };
                }
                if adjust && row.cjk_glue.contains(&item.end()) {
                    x += row.ratio * input.font_size * scale * 0.05;
                }
            }
            Item::Atom {
                id, width, height, ..
            } => {
                let rect = Rect::new(x, baseline, x + *width, baseline + *height);
                ink_boxes.push(rect);
                bounds = Some(bounds.map_or(rect, |b| b.union(&rect)));
                atoms.push(*id);
                x += *width;
            }
        }
    }
    // Negative side bearings are ink, not overflow: place the ink origin inside
    // the requested left edge. Never shrink, clip, or relax collision tolerances.
    if matches!(input.align, Align::Left | Align::Justify) && atoms.is_empty() {
        if let Some(b) = bounds {
            let dx = (bbox.x0 + indent - b.x0).max(0.0);
            if dx > 0.0 && b.x1 + dx <= bbox.x1 {
                for g in &mut glyphs {
                    g.x += dx;
                }
                for ink in &mut ink_boxes {
                    ink.x0 += dx;
                    ink.x1 += dx;
                }
                bounds = Some(Rect::new(b.x0 + dx, b.y0, b.x1 + dx, b.y1));
            }
        }
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
        },
        ink: ink_boxes,
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
    for (i, row) in rows.iter().enumerate() {
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
        let baseline = first_baseline - i as f32 * line_h;
        let line = place(shaper, input, row, bbox, baseline, scale);
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
