//! Deterministic same-page layout frames, allocated before streaming translation.
//! Glyph identities define ownership; neighbors and retained drawing constrain space.
use std::collections::{BTreeMap, BTreeSet};
use syncpdf_core::ir::{DisplayItem, PageIR, Paragraph, Region, Translatable};
use syncpdf_core::{GlyphId, ParagraphId, Rect};

#[derive(Debug, Clone)]
pub struct LayoutFrame {
    pub bbox: Rect,
    pub first_baseline: f32,
    /// Visible source content outside this paragraph plus nontext paint.
    pub obstacles: Vec<Rect>,
}

/// Allocate disjoint neighboring text frames using midpoints of measured blank gaps.
/// The first source baseline stays fixed; there is no cross-page or cross-column move.
pub fn page_frames(
    ir: &PageIR,
    regions: &[Region],
    paras: &[Paragraph],
) -> BTreeMap<ParagraphId, LayoutFrame> {
    let glyphs: Vec<_> = ir
        .glyphs()
        .filter(|g| !g.flags.invisible && !g.flags.outside_clip)
        .collect();
    let paint: Vec<Rect> = ir
        .items
        .iter()
        .filter_map(|item| match item {
            DisplayItem::Image { bbox } | DisplayItem::InlineImage { bbox } => Some(*bbox),
            DisplayItem::Path {
                bbox,
                is_fill,
                is_stroke,
            } if *is_fill || *is_stroke => {
                // A stroked rule can have zero geometric height/width.
                let pad = if *is_stroke { 0.5 } else { 0.0 };
                Some(Rect::new(
                    bbox.x0 - pad,
                    bbox.y0 - pad,
                    bbox.x1 + pad,
                    bbox.y1 + pad,
                ))
            }
            _ => None,
        })
        .collect();
    let mut out = BTreeMap::new();
    for para in paras
        .iter()
        .filter(|p| p.page == ir.page && matches!(p.translatable, Translatable::Yes))
    {
        let own: BTreeSet<GlyphId> = para.glyphs.iter().copied().collect();
        let Some(line) = para.lines.first() else {
            continue;
        };
        let first: BTreeSet<GlyphId> = line.glyphs.iter().copied().collect();
        let mut baselines: Vec<f32> = glyphs
            .iter()
            .filter(|g| first.contains(&g.id))
            .map(|g| g.matrix.f)
            .filter(|y| y.is_finite())
            .collect();
        if baselines.is_empty() {
            continue;
        }
        baselines.sort_by(f32::total_cmp);
        let baseline = baselines[baselines.len() / 2];
        let source = para.bbox;
        let region = regions
            .iter()
            .find(|r| r.index == para.region)
            .map_or(source, |r| r.bbox);
        let mut bbox = source.union(&region);
        // Preserve the source left anchor; model padding is not text indentation.
        bbox.x0 = if para.kind == syncpdf_core::ir::RegionKind::Caption
            && para.align == syncpdf_core::ir::Align::Center
        {
            region.x0
        } else {
            source.x0
        }
        .max(ir.crop_box.x0);
        bbox.x1 = bbox.x1.min(ir.crop_box.x1);
        let obstacles: Vec<Rect> = glyphs
            .iter()
            .filter(|g| {
                !own.contains(&g.id)
                    && (g.unicode.is_empty() || g.unicode.iter().any(|c| !c.is_whitespace()))
            })
            .map(|g| g.bbox)
            .chain(paint.iter().copied().filter(|b| {
                !para.atoms.iter().filter_map(|a| a.source).any(|s| {
                    s.bbox.x0 <= b.x0 && b.x1 <= s.bbox.x1 && s.bbox.y0 <= b.y0 && b.y1 <= s.bbox.y1
                })
            }))
            .collect();
        // A neighboring column can start on a different row. Horizontal ownership
        // therefore cannot depend on overlap with this paragraph's source y range.
        // This conservative bound spends only the blank gap between source extents.
        for other in &obstacles {
            if para.kind == syncpdf_core::ir::RegionKind::Caption
                && para.align == syncpdf_core::ir::Align::Center
                && (other.y1 <= source.y0 || other.y0 >= source.y1)
            {
                // The detected panel proves horizontal ownership. Its table cells
                // and plot labels on other rows do not constrain caption width.
                continue;
            }
            if other.x1 <= source.x0 {
                bbox.x0 = bbox.x0.max((source.x0 + other.x1) * 0.5);
            }
            if other.x0 >= source.x1 {
                bbox.x1 = bbox.x1.min((source.x1 + other.x0) * 0.5);
            }
        }
        if para.align == syncpdf_core::ir::Align::Center {
            let center = source.center().x;
            let half = (center - bbox.x0).min(bbox.x1 - center);
            bbox.x0 = center - half;
            bbox.x1 = center + half;
        }
        bbox.y0 = ir.crop_box.y0;
        bbox.y1 = ir.crop_box.y1;
        for other in &obstacles {
            if other.x1 > bbox.x0 && other.x0 < bbox.x1 {
                if other.y1 <= source.y0 {
                    bbox.y0 = bbox.y0.max((source.y0 + other.y1) * 0.5);
                }
                if other.y0 >= source.y1 {
                    bbox.y1 = bbox.y1.min((source.y1 + other.y0) * 0.5);
                }
            }
        }
        if bbox.width() > 0.0 && bbox.height() > 0.0 && baseline >= bbox.y0 && baseline <= bbox.y1 {
            out.insert(
                para.id.clone(),
                LayoutFrame {
                    bbox,
                    first_baseline: baseline,
                    obstacles,
                },
            );
        }
    }
    out
}

/// Actual translated ink must not overwrite visible source paint outside its ownership.
pub fn collides(frame: &LayoutFrame, lines: &[syncpdf_core::ir::LineBox]) -> bool {
    lines.iter().any(|line| {
        frame.obstacles.iter().any(|other| {
            let x = line.bbox.x1.min(other.x1) - line.bbox.x0.max(other.x0);
            let y = line.bbox.y1.min(other.y1) - line.bbox.y0.max(other.y0);
            x > 0.01 && y > 0.01
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::{Align, Glyph, GlyphFlags, GlyphSource, Line, LineBox, RegionKind};
    use syncpdf_core::{Color, Matrix, ObjRef, OpKey, PageId};

    pub(super) fn glyph(ordinal: u16, bbox: Rect, baseline: f32) -> Glyph {
        Glyph {
            id: GlyphId {
                page: PageId(0),
                op: OpKey::new(ObjRef::new(1, 0), 0),
                ordinal,
            },
            unicode: ['x'].into_iter().collect(),
            code: 120,
            font: 0,
            size: 10.0,
            matrix: Matrix::new(1.0, 0.0, 0.0, 1.0, bbox.x0, baseline),
            bbox,
            advance: bbox.width(),
            fill: Color::BLACK,
            render_mode: 0,
            source: GlyphSource {
                element_index: 0,
                string_operand_range: (0, 1),
                decoded_code_range: (0, 1),
            },
            flags: GlyphFlags::default(),
        }
    }
    pub(super) fn paragraph(g: &Glyph) -> Paragraph {
        Paragraph {
            id: ParagraphId::new(PageId(0), u32::from(g.id.ordinal) + 1),
            page: PageId(0),
            region: u32::from(g.id.ordinal),
            kind: RegionKind::Text,
            bbox: g.bbox,
            lines: vec![Line {
                glyphs: vec![g.id],
                baseline_y: g.matrix.f,
                bbox: g.bbox,
            }],
            glyphs: vec![g.id],
            text_spans: vec![],
            style_runs: vec![],
            atoms: vec![],
            text: "x".into(),
            align: Align::Left,
            first_indent: 0.0,
            line_height: 12.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        }
    }
    pub(super) fn page(glyphs: Vec<Glyph>) -> PageIR {
        PageIR {
            page: PageId(0),
            media_box: Rect::new(0.0, 0.0, 200.0, 200.0),
            crop_box: Rect::new(0.0, 0.0, 200.0, 200.0),
            rotation: 0,
            fonts: vec![],
            items: vec![DisplayItem::Text { glyphs }],
        }
    }
    #[test]
    fn neighboring_frames_share_gap_midpoint_and_keep_source_baseline() {
        let a = glyph(0, Rect::new(20.0, 120.0, 80.0, 130.0), 123.5);
        let b = glyph(1, Rect::new(20.0, 90.0, 80.0, 100.0), 93.5);
        let paras = vec![paragraph(&a), paragraph(&b)];
        let frames = page_frames(&page(vec![a, b]), &[], &paras);
        assert_eq!(frames[&paras[0].id].bbox.y0, 110.0);
        assert_eq!(frames[&paras[1].id].bbox.y1, 110.0);
        assert_eq!(frames[&paras[0].id].first_baseline, 123.5);
    }
    #[test]
    fn wide_model_region_cannot_invade_neighbor_column_or_retained_paint() {
        let a = glyph(0, Rect::new(20.0, 120.0, 80.0, 130.0), 123.5);
        let b = glyph(1, Rect::new(120.0, 120.0, 180.0, 130.0), 123.5);
        let para = paragraph(&a);
        let mut ir = page(vec![a, b]);
        ir.items.push(DisplayItem::Image {
            bbox: Rect::new(10.0, 70.0, 90.0, 100.0),
        });
        ir.items.push(DisplayItem::Path {
            bbox: Rect::new(10.0, 150.0, 90.0, 150.0),
            is_fill: false,
            is_stroke: true,
        });
        let regions = [Region {
            page: PageId(0),
            index: 0,
            kind: RegionKind::Text,
            bbox: Rect::new(10.0, 110.0, 190.0, 140.0),
            score: 1.0,
            order: 0,
        }];
        let frames = page_frames(&ir, &regions, std::slice::from_ref(&para));
        let f = &frames[&para.id];
        assert_eq!(f.bbox.x1, 100.0);
        assert_eq!(f.bbox.y0, 110.0);
        assert_eq!(f.bbox.y1, 139.75);
        let line = |bbox| LineBox {
            bbox,
            baseline_y: 123.5,
            glyphs: vec![],
            kept_atoms: vec![],
            placed_atoms: Vec::new(),
        };
        assert!(collides(f, &[line(Rect::new(20.0, 99.0, 30.0, 115.0))]));
        assert!(!collides(f, &[line(Rect::new(20.0, 120.0, 80.0, 130.0))]));
    }
    #[test]
    fn missing_source_baseline_never_invents_a_frame() {
        let a = glyph(0, Rect::new(20.0, 120.0, 80.0, 130.0), f32::NAN);
        assert!(page_frames(&page(vec![a.clone()]), &[], &[paragraph(&a)]).is_empty());
    }
}

#[cfg(test)]
mod staggered_column_tests {
    use super::*;
    #[test]
    fn staggered_column_limits_frame_even_without_vertical_source_overlap() {
        // The full geometry fixture lives in the sibling module; here use its
        // constructors to exercise the actual frame→typeset placement path.
        let a = super::tests::glyph(0, Rect::new(20.0, 120.0, 80.0, 130.0), 123.5);
        let b = super::tests::glyph(1, Rect::new(120.0, 90.0, 180.0, 100.0), 93.5);
        let para = super::tests::paragraph(&a);
        let ir = super::tests::page(vec![a, b]);
        let region = Region {
            page: ir.page,
            index: 0,
            kind: syncpdf_core::ir::RegionKind::Text,
            bbox: Rect::new(10.0, 110.0, 190.0, 140.0),
            score: 1.0,
            order: 0,
        };
        let frames = page_frames(&ir, &[region], std::slice::from_ref(&para));
        let frame = &frames[&para.id];
        assert_eq!(frame.bbox.x0, 20.0);
        assert_eq!(frame.bbox.x1, 100.0);
        let parsed = syncpdf_translate::parse_unit_html(&format!(
            "<p id=\"{}\">{}</p>",
            para.id,
            "中".repeat(15)
        ))
        .unwrap();
        let result = crate::stages::typeset::typeset_with_frame(
            &syncpdf_typeset::shaper::MonoShaper,
            &para,
            &parsed,
            &syncpdf_typeset::Obstacles::default(),
            Some(frame),
        );
        assert!(!result.paragraph.overflow);
        assert!(result.paragraph.lines.len() > 1);
        assert!(
            result.paragraph.lines.iter().all(|l| l.bbox.x1 <= 100.01),
            "cannot spend adjacent-column blank space"
        );
    }
}
