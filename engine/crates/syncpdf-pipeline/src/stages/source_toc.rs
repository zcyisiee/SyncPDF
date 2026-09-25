//! Keep a linked contents label as one semantic unit, separate from its leaders
//! and page number. Only complete, unambiguous local-link rows are rewritten;
//! ordinary prose and partial rows retain their original region segmentation.
use lopdf::{Document, Object};
use syncpdf_core::ir::{Glyph, PageIR, Region, RegionKind};
use syncpdf_core::Rect;

fn object<'a>(doc: &'a Document, value: &'a Object) -> Option<&'a Object> {
    match value {
        Object::Reference(id) => doc.get_object(*id).ok(),
        other => Some(other),
    }
}

fn local_links(doc: &Document, page: u32) -> Vec<Rect> {
    let Some(page) = doc.get_pages().get(&(page + 1)).copied() else {
        return vec![];
    };
    let Some(annots) = doc
        .get_dictionary(page)
        .ok()
        .and_then(|p| p.get(b"Annots").ok())
        .and_then(|a| object(doc, a))
        .and_then(|a| a.as_array().ok())
    else {
        return vec![];
    };
    annots
        .iter()
        .filter_map(|a| {
            let d = object(doc, a)?.as_dict().ok()?;
            if d.get(b"Subtype").ok()?.as_name().ok()? != b"Link" {
                return None;
            }
            let local = d.get(b"Dest").is_ok()
                || d.get(b"A")
                    .ok()
                    .and_then(|a| object(doc, a))
                    .and_then(|a| a.as_dict().ok())
                    .is_some_and(|a| {
                        a.get(b"S").ok().and_then(|s| s.as_name().ok()) == Some(b"GoTo")
                            && a.get(b"D").is_ok()
                    });
            if !local {
                return None;
            }
            let values = object(doc, d.get(b"Rect").ok()?)?.as_array().ok()?;
            let v: Vec<_> = values
                .iter()
                .map(|v| v.as_float().ok())
                .collect::<Option<_>>()?;
            (v.len() == 4 && v.iter().all(|v| v.is_finite()) && v[0] < v[2] && v[1] < v[3])
                .then(|| Rect::new(v[0], v[1], v[2], v[3]))
        })
        .collect()
}

fn bounds(glyphs: &[&Glyph]) -> Option<Rect> {
    glyphs.iter().map(|g| g.bbox).reduce(|a, b| a.union(&b))
}
fn white(g: &Glyph) -> bool {
    !g.unicode.is_empty() && g.unicode.iter().all(|c| c.is_whitespace())
}

fn row_parts(row: &[&Glyph], links: &[Rect], page: &[&Glyph]) -> Option<(Rect, Rect)> {
    if row.iter().any(|g| g.unicode.is_empty()) {
        return None;
    }
    let links: Vec<_> = links
        .iter()
        .filter(|r| row.iter().any(|g| r.contains(g.bbox.center())))
        .collect();
    if links.len() != 1 {
        return None;
    }
    let link = links[0];
    // A multiline/shared annotation cannot be given two paragraph owners.
    if page
        .iter()
        .any(|g| !white(g) && link.contains(g.bbox.center()) && !row.iter().any(|a| a.id == g.id))
    {
        return None;
    }
    let split = row.iter().position(|g| !link.contains(g.bbox.center()))?;
    let (label, suffix) = row.split_at(split);
    if label.is_empty()
        || suffix.iter().any(|g| link.contains(g.bbox.center()))
        || !label
            .iter()
            .flat_map(|g| &g.unicode)
            .any(|c| c.is_alphabetic())
    {
        return None;
    }
    let suffix_text: String = suffix
        .iter()
        .flat_map(|g| &g.unicode)
        .filter(|c| !c.is_whitespace() && **c != '.')
        .collect();
    let page_number = !suffix_text.is_empty()
        && (suffix_text.chars().all(|c| c.is_ascii_digit())
            || suffix_text.chars().all(|c| "ivxlcdmIVXLCDM".contains(c)));
    if !page_number {
        return None;
    }
    let label_box = bounds(label)?;
    let suffix_box = bounds(suffix)?;
    let number_x = suffix
        .iter()
        .find(|g| g.unicode.iter().any(|c| c.is_ascii_alphanumeric()))?
        .bbox
        .x0;
    // Separate page-number column, not an inline linked phrase followed by a numeral.
    if number_x - label_box.x1 < label[0].size * 3.0 {
        return None;
    }
    Some((label_box, suffix_box))
}

pub(crate) fn refine(regions: &mut Vec<Region>, ir: &PageIR, doc: &Document) {
    let links = local_links(doc, ir.page.0);
    if links.is_empty() {
        return;
    }
    let glyphs: Vec<_> = ir
        .glyphs()
        .filter(|g| !g.flags.invisible && !g.flags.outside_clip && !white(g))
        .collect();
    let mut next = regions.iter().map(|r| r.index).max().unwrap_or(0) + 1;
    let mut output = Vec::new();
    for region in regions.iter() {
        if !matches!(
            region.kind,
            RegionKind::Text | RegionKind::ParagraphTitle | RegionKind::List
        ) {
            output.push(region.clone());
            continue;
        }
        let selected: Vec<_> = glyphs
            .iter()
            .filter(|g| region.bbox.contains(g.bbox.center()))
            .map(|g| (g.id, g.bbox))
            .collect();
        let rows = syncpdf_layout::group_lines(&selected, &region.bbox);
        let parts: Option<Vec<_>> = rows
            .iter()
            .map(|ids| {
                let mut row: Vec<_> = glyphs
                    .iter()
                    .copied()
                    .filter(|g| ids.contains(&g.id))
                    .collect();
                row.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
                row_parts(&row, &links, &glyphs)
            })
            .collect();
        let Some(parts) = parts.filter(|parts| !parts.is_empty()) else {
            output.push(region.clone());
            continue;
        };
        for (label, suffix) in parts {
            // Preserve the source glyph box, but give its layout region the
            // evidenced blank measure up to the protected leaders/page column.
            let measure = Rect::new(label.x0, label.y0, suffix.x0, label.y1);
            let label = if glyphs
                .iter()
                .any(|g| measure.contains(g.bbox.center()) && !label.contains(g.bbox.center()))
            {
                label
            } else {
                measure
            };
            for (bbox, kind) in [(label, region.kind), (suffix, RegionKind::Other)] {
                output.push(Region {
                    bbox,
                    kind,
                    index: next,
                    ..region.clone()
                });
                next += 1;
            }
        }
    }
    *regions = output;
}

/// A single-row heading may use its TOC label measure without being narrowed by
/// indented entries on other rows. Require the adjoining protected page column,
/// complete paragraph ownership and numeric/leader text rather than a wide model box.
pub(super) fn heading_measure(
    ir: &PageIR,
    regions: &[Region],
    p: &syncpdf_core::ir::Paragraph,
) -> Option<Rect> {
    if p.lines.len() != 1 {
        return None;
    }
    let label = regions
        .iter()
        .find(|r| r.index == p.region && r.page == p.page)?;
    let suffix = regions.iter().find(|r| {
        r.kind == RegionKind::Other
            && r.page == p.page
            && r.bbox.x0 == label.bbox.x1
            && (r.bbox.center().y - label.bbox.center().y).abs() < label.bbox.height() * 0.25
    })?;
    let glyphs: Vec<_> = ir
        .glyphs()
        .filter(|g| !g.flags.invisible && !g.flags.outside_clip && !white(g))
        .collect();
    if glyphs
        .iter()
        .any(|g| label.bbox.contains(g.bbox.center()) && !p.glyphs.contains(&g.id))
    {
        return None;
    }
    let text: String = glyphs
        .iter()
        .filter(|g| suffix.bbox.contains(g.bbox.center()))
        .flat_map(|g| &g.unicode)
        .filter(|c| !c.is_whitespace() && **c != '.')
        .collect();
    if text.is_empty()
        || !(text.chars().all(|c| c.is_ascii_digit())
            || text.chars().all(|c| "ivxlcdmIVXLCDM".contains(c)))
    {
        return None;
    }
    Some(label.bbox)
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::{GlyphFlags, GlyphSource};
    use syncpdf_core::{Color, GlyphId, Matrix, ObjRef, OpKey, PageId};

    fn glyph(n: u16, c: char, x: f32, y: f32) -> Glyph {
        Glyph {
            id: GlyphId {
                page: PageId(0),
                op: OpKey {
                    stream: ObjRef { obj: 1, gen: 0 },
                    op_index: 0,
                },
                ordinal: n,
            },
            unicode: vec![c].into(),
            code: u32::from(c),
            font: 0,
            size: 10.0,
            matrix: Matrix {
                e: x,
                f: y,
                ..Matrix::IDENTITY
            },
            bbox: Rect::new(x, y, x + 5.0, y + 10.0),
            ink: None,
            advance: 5.0,
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
    #[test]
    fn linked_label_is_separate_from_dots_and_page_number() {
        let gs = vec![
            glyph(0, '1', 20.0, 100.0),
            glyph(1, 'A', 30.0, 100.0),
            glyph(2, '.', 50.0, 100.0),
            glyph(3, '.', 80.0, 100.0),
            glyph(4, '9', 150.0, 100.0),
        ];
        let row: Vec<_> = gs.iter().collect();
        let link = Rect::new(19.0, 99.0, 36.0, 111.0);
        let (label, suffix) = row_parts(&row, &[link], &row).unwrap();
        assert_eq!(label, Rect::new(20.0, 100.0, 35.0, 110.0));
        assert_eq!(suffix, Rect::new(50.0, 100.0, 155.0, 110.0));
        assert!(row_parts(&row, &[link, link], &row).is_none());
        let partial = Rect::new(29.0, 99.0, 36.0, 111.0);
        assert!(row_parts(&row, &[partial], &row).is_none());
        let mut other = gs.clone();
        other[4].unicode = vec!['z'].into();
        let refs: Vec<_> = other.iter().collect();
        assert!(row_parts(&refs, &[link], &refs).is_none());
        other[4] = glyph(4, '9', 40.0, 100.0);
        let refs: Vec<_> = other.iter().collect();
        assert!(row_parts(&refs, &[link], &refs).is_none());
        let mut unknown = gs.clone();
        unknown[2].unicode.clear();
        let refs: Vec<_> = unknown.iter().collect();
        assert!(row_parts(&refs, &[link], &refs).is_none());
        let outside = glyph(5, 'B', 25.0, 95.0);
        let mut page = row.clone();
        page.push(&outside);
        assert!(row_parts(&row, &[link], &page).is_none());
    }

    #[test]
    #[ignore = "manual immutable paper evidence; requires TOC_SOURCE, TOC_IR, TOC_REGIONS, TOC_OUTPUT"]
    fn real_toc_inventory() {
        let doc = Document::load(std::env::var("TOC_SOURCE").unwrap()).unwrap();
        let pages: Vec<PageIR> =
            serde_json::from_slice(&std::fs::read(std::env::var("TOC_IR").unwrap()).unwrap())
                .unwrap();
        let mut regions: Vec<Region> =
            serde_json::from_slice(&std::fs::read(std::env::var("TOC_REGIONS").unwrap()).unwrap())
                .unwrap();
        let page = regions[0].page;
        let ir = pages.iter().find(|p| p.page == page).unwrap();
        refine(&mut regions, ir, &doc);
        let paras = crate::stages::analyze_page(ir, &regions);
        std::fs::write(
            std::env::var("TOC_OUTPUT").unwrap(),
            serde_json::to_vec_pretty(&paras).unwrap(),
        )
        .unwrap();
    }
}
