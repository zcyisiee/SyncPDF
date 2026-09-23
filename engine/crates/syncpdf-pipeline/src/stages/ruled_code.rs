//! 保守拆分带三条横线的算法框与其右侧说明。
//!
//! 仅接受模型Code区域、算法标题、成组源横线和贯通空白栏缝共同支持的情形。
//! 缺少证据或有歧义时保持检测结果；本模块不做通用代码/正文分类。

use syncpdf_core::ir::{DisplayItem, Glyph, PageIR, Region, RegionKind};
use syncpdf_core::Rect;

/// 返回实际拆出的右侧说明区域数量。字形、绘制操作和区域外对象均不改变。
pub(super) fn refine_ruled_code_sidebars(regions: &mut Vec<Region>, ir: &PageIR) -> usize {
    let original = regions.clone();
    let mut count = 0;
    for (index, region) in original.iter().enumerate() {
        if region.kind != RegionKind::Code {
            continue;
        }
        let Some((code, sidebar)) = split_candidate(region, &original, ir) else {
            continue;
        };
        let next_index = regions.iter().map(|r| r.index).max().unwrap_or(0) + 1;
        regions[index].bbox = code;
        regions.push(Region {
            page: region.page,
            index: next_index,
            kind: RegionKind::Text,
            bbox: sidebar,
            score: region.score,
            order: region.order.saturating_add(1),
        });
        count += 1;
    }
    count
}

fn visible(g: &&Glyph) -> bool {
    !g.flags.invisible && !g.flags.outside_clip && !g.flags.is_space && !g.unicode.is_empty()
}

fn split_candidate(region: &Region, regions: &[Region], ir: &PageIR) -> Option<(Rect, Rect)> {
    let glyphs: Vec<&Glyph> = ir.glyphs().filter(visible).collect();
    let old = region.bbox;
    let mut sizes: Vec<f32> = glyphs
        .iter()
        .filter(|g| old.contains(g.bbox.center()) && g.size.is_finite() && g.size > 0.0)
        .map(|g| g.size)
        .collect();
    sizes.sort_by(f32::total_cmp);
    let em = *sizes.get(sizes.len() / 2)?;
    let rules: Vec<Rect> = ir
        .items
        .iter()
        .filter_map(|item| match item {
            DisplayItem::Path {
                bbox,
                is_fill: false,
                is_stroke: true,
            } if bbox.height().abs() <= 0.1
                && bbox.width() >= 8.0 * em
                && bbox.x0 >= old.x0 - 0.5
                && bbox.x0 <= old.x0 + 2.0 * em
                && old.x1 - bbox.x1 >= 6.0 * em
                && bbox.y0 >= old.y0 - em
                && bbox.y1 <= old.y1 + em =>
            {
                Some(*bbox)
            }
            _ => None,
        })
        .collect();
    let mut groups: Vec<Vec<Rect>> = Vec::new();
    for rule in rules {
        if let Some(group) = groups.iter_mut().find(|group| {
            (group[0].x0 - rule.x0).abs() <= 0.5 && (group[0].x1 - rule.x1).abs() <= 0.5
        }) {
            group.push(rule);
        } else {
            groups.push(vec![rule]);
        }
    }
    // 多套框线不能凭距离挑一个；表格的多条横线也不视作算法框。
    if groups.len() != 1 || groups[0].len() != 3 {
        return None;
    }
    let rules = &mut groups[0];
    rules.sort_by(|a, b| b.y0.total_cmp(&a.y0));
    let (top, header_bottom, bottom) = (rules[0], rules[1], rules[2]);
    if !(0.7 * em..=2.0 * em).contains(&(top.y0 - header_bottom.y0))
        || header_bottom.y0 - bottom.y0 < 5.0 * em
        || (old.y1 - top.y0).abs() > 2.0 * em
        || (old.y0 - bottom.y0).abs() > 2.0 * em
    {
        return None;
    }
    let mut header: Vec<&Glyph> = glyphs
        .iter()
        .copied()
        .filter(|g| {
            let c = g.bbox.center();
            c.x >= top.x0 && c.x <= top.x1 && c.y > header_bottom.y0 && c.y < top.y0
        })
        .collect();
    header.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
    let title: String = header.iter().flat_map(|g| g.unicode.iter()).collect();
    let title = title.to_lowercase();
    if !title.starts_with("algorithm") && !title.starts_with("算法") {
        return None;
    }

    // 右侧首行可略高于框线；下界仍以原模型范围为准，避免吞掉下方全宽正文。
    let band = Rect::new(old.x0, old.y0, old.x1, old.y1.max(top.y0 + 0.5 * em));
    let right: Vec<&Glyph> = glyphs
        .iter()
        .copied()
        .filter(|g| band.contains(g.bbox.center()) && g.bbox.x0 > top.x1)
        .collect();
    if right
        .iter()
        .flat_map(|g| g.unicode.iter())
        .filter(|c| c.is_alphabetic())
        .count()
        < 30
    {
        return None;
    }
    let sidebar = right
        .iter()
        .skip(1)
        .fold(right.first()?.bbox, |b, g| b.union(&g.bbox));
    if sidebar.width() < 5.0 * em || sidebar.height() < 5.0 * em {
        return None;
    }
    let split = (top.x1 + sidebar.x0) * 0.5;
    if sidebar.x0 - top.x1 < 0.2 * em {
        return None;
    }
    // 栏缝必须贯通，不能切开任何字形、图像或路径（含公式横线）。
    if glyphs
        .iter()
        .any(|g| band.contains(g.bbox.center()) && g.bbox.x0 < split && g.bbox.x1 > split)
        || ir.items.iter().any(|item| {
            let bbox = match item {
                DisplayItem::Path { bbox, .. }
                | DisplayItem::Image { bbox }
                | DisplayItem::InlineImage { bbox } => bbox,
                _ => return false,
            };
            bbox.y1 >= band.y0 && bbox.y0 <= band.y1 && bbox.x0 < split && bbox.x1 > split
        })
    {
        return None;
    }
    let code = Rect::new(
        old.x0,
        old.y0.max(bottom.y0 - 0.25 * em),
        split,
        old.y1.max(top.y0),
    );
    // 原Code中的每个可见字形必须仍在两块之一；且右文不能抢走其他语义区。
    if glyphs.iter().any(|g| {
        let c = g.bbox.center();
        (old.contains(c) && !code.contains(c) && !sidebar.contains(c))
            || (sidebar.contains(c)
                && regions.iter().any(|r| {
                    r.index != region.index && r.kind != RegionKind::Formula && r.bbox.contains(c)
                }))
    }) {
        return None;
    }
    Some((code, sidebar))
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::{GlyphFlags, GlyphSource};
    use syncpdf_core::{Color, GlyphId, Matrix, ObjRef, OpKey, PageId};

    fn text(ir: &mut PageIR, value: &str, x: f32, y: f32) {
        let first = ir.glyphs().count() as u16;
        let glyphs = value
            .chars()
            .enumerate()
            .map(|(i, c)| Glyph {
                id: GlyphId {
                    page: PageId(0),
                    op: OpKey::new(ObjRef::new(1, 0), 0),
                    ordinal: first + i as u16,
                },
                unicode: [c].into_iter().collect(),
                code: c as u32,
                font: 0,
                size: 10.0,
                matrix: Matrix::IDENTITY,
                bbox: Rect::new(x + i as f32 * 5.0, y, x + i as f32 * 5.0 + 4.0, y + 8.0),
                advance: 5.0,
                fill: Color::BLACK,
                render_mode: 0,
                source: GlyphSource {
                    element_index: 0,
                    string_operand_range: (0, 1),
                    decoded_code_range: (0, 1),
                },
                flags: GlyphFlags::default(),
            })
            .collect();
        ir.items.push(DisplayItem::Text { glyphs });
    }

    fn fixture() -> (Vec<Region>, PageIR) {
        let mut ir = PageIR {
            page: PageId(0),
            media_box: Rect::new(0.0, 0.0, 300.0, 300.0),
            crop_box: Rect::new(0.0, 0.0, 300.0, 300.0),
            rotation: 0,
            fonts: Vec::new(),
            items: Vec::new(),
        };
        for y in [195.0, 180.0, 25.0] {
            ir.items.push(DisplayItem::Path {
                bbox: Rect::new(20.0, y, 150.0, y),
                is_fill: false,
                is_stroke: true,
            });
        }
        text(&mut ir, "Algorithm 1", 20.0, 183.0);
        text(&mut ir, "1 return value", 12.0, 30.0);
        for y in [180.0, 160.0, 140.0, 120.0, 100.0, 80.0, 60.0, 40.0, 20.0] {
            text(&mut ir, "Some useful words", 170.0, y);
        }
        let regions = vec![Region {
            page: PageId(0),
            index: 0,
            kind: RegionKind::Code,
            bbox: Rect::new(10.0, 10.0, 260.0, 190.0),
            score: 0.9,
            order: 0,
        }];
        (regions, ir)
    }

    #[test]
    fn three_rules_and_header_split_sidebar_without_losing_line_numbers() {
        let (mut regions, ir) = fixture();
        let original = regions[0].bbox;
        assert_eq!(refine_ruled_code_sidebars(&mut regions, &ir), 1);
        assert_eq!(regions.len(), 2);
        assert_eq!(regions[0].kind, RegionKind::Code);
        assert_eq!(regions[0].bbox.x0, original.x0);
        assert_eq!(regions[1].kind, RegionKind::Text);
        for g in ir.glyphs().filter(|g| original.contains(g.bbox.center())) {
            assert_eq!(
                regions
                    .iter()
                    .filter(|r| r.bbox.contains(g.bbox.center()))
                    .count(),
                1
            );
        }
        let first = regions.clone();
        assert_eq!(refine_ruled_code_sidebars(&mut regions, &ir), 0);
        assert_eq!(regions, first, "重复后处理不继续切块");
    }

    #[test]
    fn no_rules_table_label_and_missing_algorithm_header_are_not_guessed() {
        let (base, ir) = fixture();
        let mut cases = Vec::new();
        let mut no_rules = ir.clone();
        no_rules
            .items
            .retain(|i| !matches!(i, DisplayItem::Path { .. }));
        cases.push((base.clone(), no_rules));
        let mut table = base.clone();
        table[0].kind = RegionKind::Table;
        cases.push((table, ir.clone()));
        let mut no_header = ir;
        no_header.items.retain(|i| !matches!(i, DisplayItem::Text { glyphs } if glyphs.first().is_some_and(|g| g.bbox.y0 == 183.0)));
        cases.push((base, no_header));
        for (mut regions, ir) in cases {
            let before = regions.clone();
            assert_eq!(refine_ruled_code_sidebars(&mut regions, &ir), 0);
            assert_eq!(regions, before);
        }
    }

    #[test]
    fn crossing_text_or_path_and_multiple_rule_groups_are_rejected() {
        let (regions, base) = fixture();
        let mut crossing_text = base.clone();
        text(&mut crossing_text, "crossing", 148.0, 70.0);
        let mut crossing_rule = base.clone();
        crossing_rule.items.push(DisplayItem::Path {
            bbox: Rect::new(140.0, 70.0, 180.0, 70.0),
            is_fill: false,
            is_stroke: true,
        });
        let mut second_frame = base;
        for y in [193.0, 178.0, 28.0] {
            second_frame.items.push(DisplayItem::Path {
                bbox: Rect::new(25.0, y, 145.0, y),
                is_fill: false,
                is_stroke: true,
            });
        }
        for ir in [crossing_text, crossing_rule, second_frame] {
            let mut got = regions.clone();
            assert_eq!(refine_ruled_code_sidebars(&mut got, &ir), 0);
            assert_eq!(got, regions);
        }
    }
}
