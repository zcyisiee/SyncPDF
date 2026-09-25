//! Conservative first-page author and affiliation protection.
//!
//! This pure policy needs a detected title and an abstract heading/body below it.
//! Only short, horizontal, roughly centered text in the intervening band is eligible.
//! An email or explicit affiliation is required as an anchor; nearby name/marked
//! lines are included only when they form a tight vertical cluster with that anchor.
//! Unusual journal layouts, missing region labels, and ambiguous prose are left for
//! translation rather than guessed to be metadata.

use std::collections::HashSet;

use syncpdf_core::ir::{PageIR, Paragraph, Region, RegionKind, Translatable};
use syncpdf_core::{GlyphId, ParagraphId};

const REASON: &str = "author_metadata";

/// Mark confidently identified front matter and return only IDs actually changed.
/// Existing `No` decisions and all source text, glyphs, and geometry are preserved.
pub fn protect_front_matter(
    ir: &PageIR,
    regions: &[Region],
    paragraphs: &mut [Paragraph],
) -> Vec<ParagraphId> {
    if ir.page.0 != 0 || ir.rotation.rem_euclid(180) != 0 {
        return Vec::new();
    }

    let title = paragraphs
        .iter()
        .filter(|p| {
            p.page == ir.page
                && p.kind == RegionKind::Title
                && regions.iter().any(|r| {
                    r.page == ir.page && r.index == p.region && r.kind == RegionKind::Title
                })
        })
        .max_by(|a, b| a.bbox.y0.total_cmp(&b.bbox.y0));
    let Some(title) = title else {
        return Vec::new();
    };
    let abstract_top = paragraphs
        .iter()
        .filter(|p| {
            p.page == ir.page
                && regions.iter().any(|r| {
                    r.page == ir.page
                        && r.index == p.region
                        && (r.kind == RegionKind::Abstract
                            || (r.kind == RegionKind::ParagraphTitle
                                && p.text.trim().eq_ignore_ascii_case("abstract")))
                })
                && p.bbox.y1 < title.bbox.y0
        })
        .max_by(|a, b| a.bbox.y1.total_cmp(&b.bbox.y1))
        .map(|p| p.bbox.y1);
    let Some(lower) = abstract_top else {
        return Vec::new();
    };
    let upper = title.bbox.y0;
    if upper - lower < 12.0 || upper - lower > ir.crop_box.height() * 0.28 {
        return Vec::new();
    }

    let glyphs: std::collections::HashMap<GlyphId, _> = ir.glyphs().map(|g| (g.id, g)).collect();
    let page_center = ir.crop_box.center().x;
    let page_width = ir.crop_box.width();
    let mut candidates: Vec<(usize, bool)> = paragraphs
        .iter()
        .enumerate()
        .filter_map(|(i, p)| {
            if p.page != ir.page
                || p.kind != RegionKind::Text
                || p.bbox.y0 <= lower + 1.0
                || p.bbox.y1 >= upper - 1.0
                || p.bbox.width() > page_width * 0.75
                || (p.bbox.center().x - page_center).abs() > page_width * 0.16
                || p.lines.is_empty()
                || p.lines.len() > 2
                || p.text.chars().count() > 160
                || rotated(p, &glyphs)
            {
                return None;
            }
            let text = p.text.trim();
            let anchor = email(text) || affiliation(text);
            (anchor || name_or_marked(text) || address(text)).then_some((i, anchor))
        })
        .collect();
    if !candidates.iter().any(|(_, anchor)| *anchor) {
        return Vec::new();
    }
    candidates.sort_by(|a, b| paragraphs[b.0].bbox.y0.total_cmp(&paragraphs[a.0].bbox.y0));

    // Connected components of near-touching metadata lines. A name is never
    // protected without an email/affiliation in its own component.
    let mut selected = HashSet::new();
    let mut start = 0;
    while start < candidates.len() {
        let mut end = start + 1;
        while end < candidates.len()
            && paragraphs[candidates[end - 1].0].bbox.y0 - paragraphs[candidates[end].0].bbox.y1
                <= 24.0
        {
            end += 1;
        }
        if candidates[start..end].iter().any(|(_, anchor)| *anchor) {
            selected.extend(candidates[start..end].iter().map(|(i, _)| *i));
        }
        start = end;
    }
    let source_glyphs: HashSet<GlyphId> = selected
        .iter()
        .flat_map(|&i| paragraphs[i].glyphs.iter().copied())
        .collect();
    let mut changed = Vec::new();
    for (i, p) in paragraphs.iter_mut().enumerate() {
        if matches!(p.translatable, Translatable::Yes)
            && (selected.contains(&i)
                || (p.page == ir.page
                    && p.glyphs.iter().any(|id| source_glyphs.contains(id))
                    && !rotated(p, &glyphs)))
        {
            p.translatable = Translatable::No {
                reason: REASON.into(),
            };
            changed.push(p.id.clone());
        }
    }
    changed
}

/// Protect structured name rosters under an explicit author-list heading,
/// including continuations on later pages. Narrative author-contribution prose
/// is still translatable; the next section heading closes the roster scope.
pub fn protect_author_lists(paragraphs: &mut [Paragraph]) -> Vec<ParagraphId> {
    let heading = regex::Regex::new(
        r"(?i)^(?:(?:[a-z]|[0-9]+(?:\.[0-9]+)*)[.)]?\s+)?(?:author list|authors|contributors)$",
    )
    .expect("author heading pattern");
    let mut in_roster = false;
    let mut changed = Vec::new();
    for p in paragraphs {
        if matches!(p.kind, RegionKind::Title | RegionKind::ParagraphTitle) {
            in_roster = heading.is_match(p.text.trim());
            continue;
        }
        if !in_roster || p.kind != RegionKind::Text || p.translatable != Translatable::Yes {
            continue;
        }
        let names = p
            .text
            .split_once(':')
            .map_or(p.text.as_str(), |(label, rest)| {
                if label.len() <= 80 {
                    rest
                } else {
                    p.text.as_str()
                }
            });
        let entries: Vec<_> = names
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect();
        if entries.len() < 2 || !entries.iter().all(|entry| roster_name(entry)) {
            continue;
        }
        p.translatable = Translatable::No {
            reason: REASON.into(),
        };
        changed.push(p.id.clone());
    }
    changed
}

fn roster_name(text: &str) -> bool {
    let words: Vec<_> = text.split_whitespace().collect();
    (2..=6).contains(&words.len())
        && words.iter().all(|word| {
            let word = word.trim_matches(|c: char| "*†‡".contains(c));
            word.chars().next().is_some_and(char::is_uppercase)
                && word
                    .chars()
                    .all(|c| c.is_alphabetic() || ".-'’".contains(c))
        })
}

fn rotated(
    p: &Paragraph,
    glyphs: &std::collections::HashMap<GlyphId, &syncpdf_core::ir::Glyph>,
) -> bool {
    if p.bbox.height() > p.bbox.width() * 2.0 {
        return true;
    }
    p.glyphs.iter().any(|id| {
        glyphs.get(id).is_some_and(|g| {
            let m = g.matrix;
            let horizontal = (m.a * m.a + m.b * m.b).sqrt();
            horizontal > 0.0 && m.b.abs() > horizontal * 0.25
        })
    })
}

/// 含邮箱地址；分组写法 `{a, b}@x.edu` 的 `}` 留在本地部分里。
fn email(text: &str) -> bool {
    text.split(|c: char| c.is_whitespace() || ",;{<>".contains(c))
        .any(|part| {
            part.split_once('@').is_some_and(|(local, domain)| {
                !local.is_empty() && domain.contains('.') && !domain.ends_with('.')
            })
        })
}

fn affiliation(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    !text.contains(['!', '?'])
        && !text.ends_with('.')
        && text
            .split_whitespace()
            .filter(|word| !matches!(word.to_ascii_lowercase().as_str(), "of" | "and" | "the"))
            .all(|word| {
                word.chars()
                    .find(|c| c.is_alphabetic())
                    .is_some_and(char::is_uppercase)
            })
        && [
            "university",
            "college",
            "institute",
            "department",
            "school of",
            "laboratory",
            "research center",
            "research centre",
        ]
        .iter()
        .any(|word| lower.contains(word))
}

/// 信息带里的机构 / 邮箱 / 地址行（作者名行之外的元数据锚点）。
pub(crate) fn metadata_anchor(text: &str) -> bool {
    email(text) || affiliation(text) || address(text)
}

fn address(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    text.chars().any(|c| c.is_ascii_digit())
        && [
            " street",
            " st.",
            " avenue",
            " ave.",
            " road",
            " rd.",
            " boulevard",
            " blvd",
            " p.o. box",
        ]
        .iter()
        .any(|part| lower.contains(part))
}

fn name_or_marked(text: &str) -> bool {
    let words: Vec<&str> = text
        .split(|c: char| c.is_whitespace() || ",;†‡*".contains(c))
        .filter(|word| !word.is_empty())
        .collect();
    (2..=12).contains(&words.len())
        && !text.contains(['.', '!', '?', ':'])
        && words.iter().all(|word| {
            word.chars().next().is_some_and(char::is_uppercase)
                && word
                    .chars()
                    .all(|c| c.is_alphabetic() || c == '-' || c == '\'')
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn email_accepts_plain_and_brace_grouped_addresses() {
        assert!(email("ada@umd.edu"));
        assert!(email("<ada@umd.edu>, alan@cs.ox.ac.uk"));
        // 回归：分组写法曾因按 `}` 切分而漏判
        assert!(email("{jennarus, miyyer}@umd.edu"));
        assert!(email("University of Maryland {ada,alan}@umd.edu"));
        assert!(!email("see @mention or user@localhost"));
        assert!(!email("{a, b}@ and x@y."));
    }
    use syncpdf_core::ir::{Align, DisplayItem, Line};
    use syncpdf_core::{ObjRef, OpKey, PageId, Rect};

    fn paragraph(seq: u32, kind: RegionKind, text: &str, bbox: Rect) -> Paragraph {
        Paragraph {
            id: ParagraphId::new(PageId(0), seq),
            page: PageId(0),
            region: seq,
            kind,
            bbox,
            lines: vec![Line {
                glyphs: Vec::new(),
                baseline_y: bbox.y0,
                bbox,
            }],
            glyphs: Vec::new(),
            text_spans: Vec::new(),
            style_runs: Vec::new(),
            atoms: Vec::new(),
            decorations: Vec::new(),
            text: text.into(),
            align: Align::Center,
            first_indent: 0.0,
            line_height: 12.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        }
    }

    fn fixture() -> (PageIR, Vec<Region>, Vec<Paragraph>) {
        let page = PageIR {
            page: PageId(0),
            media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            crop_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            rotation: 0,
            fonts: Vec::new(),
            items: vec![DisplayItem::Text { glyphs: Vec::new() }],
        };
        let paragraphs = vec![
            paragraph(
                1,
                RegionKind::Title,
                "A Paper Title",
                Rect::new(120.0, 675.0, 490.0, 691.0),
            ),
            paragraph(
                2,
                RegionKind::Text,
                "Ada Lovelace, Grace Hopper",
                Rect::new(200.0, 623.0, 412.0, 634.0),
            ),
            paragraph(
                3,
                RegionKind::Text,
                "Example University",
                Rect::new(255.0, 608.0, 357.0, 619.0),
            ),
            paragraph(
                4,
                RegionKind::Text,
                "†Research Group",
                Rect::new(265.0, 595.0, 347.0, 606.0),
            ),
            paragraph(
                5,
                RegionKind::Text,
                "ada@example.edu",
                Rect::new(240.0, 582.0, 372.0, 593.0),
            ),
            paragraph(
                6,
                RegionKind::ParagraphTitle,
                "Abstract",
                Rect::new(275.0, 543.0, 337.0, 555.0),
            ),
            paragraph(
                7,
                RegionKind::Abstract,
                "We study neural networks.",
                Rect::new(140.0, 420.0, 472.0, 530.0),
            ),
            paragraph(
                8,
                RegionKind::ParagraphTitle,
                "1 Introduction",
                Rect::new(108.0, 380.0, 200.0, 391.0),
            ),
            paragraph(
                9,
                RegionKind::Text,
                "Example University appears in this ordinary body sentence.",
                Rect::new(108.0, 320.0, 504.0, 365.0),
            ),
        ];
        let regions = paragraphs
            .iter()
            .map(|p| Region {
                page: p.page,
                index: p.region,
                kind: p.kind,
                bbox: p.bbox,
                score: 0.9,
                order: p.region,
            })
            .collect();
        (page, regions, paragraphs)
    }

    #[test]
    fn author_rosters_require_a_heading_and_names_not_contribution_prose() {
        let bbox = Rect::new(50.0, 60.0, 500.0, 100.0);
        let make = |n, kind, text| paragraph(n, kind, text, bbox);
        let mut ps = vec![
            make(1, RegionKind::Text, "Ada Lovelace, Grace Hopper"),
            make(2, RegionKind::ParagraphTitle, "A. Author List"),
            make(
                3,
                RegionKind::Text,
                "Authors are listed alphabetically by their first name.",
            ),
            make(
                4,
                RegionKind::Text,
                "Research: Ada Lovelace*, G. Hopper, Alan Turing,",
            ),
            make(5, RegionKind::Footer, "46"),
            make(6, RegionKind::Text, "Edsger Dijkstra, Donald Knuth"),
            make(
                7,
                RegionKind::Text,
                "Ada Lovelace developed the method, Grace Hopper evaluated it.",
            ),
            make(8, RegionKind::ParagraphTitle, "B. Evaluation Details"),
            make(9, RegionKind::Text, "Ada Lovelace, Grace Hopper"),
        ];
        ps[5].page = PageId(1);
        ps[5].id = ParagraphId::new(PageId(1), 1);
        let before = ps.clone();
        assert_eq!(
            protect_author_lists(&mut ps),
            vec![before[3].id.clone(), before[5].id.clone()]
        );
        for (index, p) in ps.iter().enumerate() {
            assert_eq!(p.text, before[index].text);
            assert_eq!(p.bbox, before[index].bbox);
            assert_eq!(p.glyphs, before[index].glyphs);
            assert_eq!(
                p.translatable == Translatable::Yes,
                ![3, 5].contains(&index)
            );
        }
    }

    #[test]
    fn protects_author_cluster_but_not_title_abstract_or_body() {
        let (ir, regions, mut paragraphs) = fixture();
        let before = paragraphs.clone();
        let changed = protect_front_matter(&ir, &regions, &mut paragraphs);
        assert_eq!(
            changed,
            (2..=5)
                .map(|n| ParagraphId::new(PageId(0), n))
                .collect::<Vec<_>>()
        );
        for (index, p) in paragraphs.iter().enumerate() {
            assert_eq!(p.text, before[index].text);
            assert_eq!(p.glyphs, before[index].glyphs);
            assert_eq!(p.bbox, before[index].bbox);
            if (1..=4).contains(&index) {
                assert_eq!(
                    p.translatable,
                    Translatable::No {
                        reason: REASON.into()
                    }
                );
            } else {
                assert_eq!(p.translatable, Translatable::Yes);
            }
        }
        assert!(protect_front_matter(&ir, &regions, &mut paragraphs).is_empty());
    }

    #[test]
    fn refuses_missing_boundaries_or_nonfirst_page() {
        let (mut ir, mut regions, mut paragraphs) = fixture();
        regions.retain(|r| r.kind != RegionKind::Title);
        assert!(protect_front_matter(&ir, &regions, &mut paragraphs).is_empty());
        let (ir2, mut regions, mut paragraphs) = fixture();
        regions.retain(|r| r.kind != RegionKind::Abstract && r.kind != RegionKind::ParagraphTitle);
        assert!(protect_front_matter(&ir2, &regions, &mut paragraphs).is_empty());
        let (_, regions, mut paragraphs) = fixture();
        ir.page = PageId(1);
        assert!(protect_front_matter(&ir, &regions, &mut paragraphs).is_empty());
    }

    #[test]
    fn existing_no_rotated_note_and_shared_metadata_glyph() {
        let (ir, mut regions, mut paragraphs) = fixture();
        paragraphs[3].translatable = Translatable::No {
            reason: "previous_reason".into(),
        };
        let source = GlyphId {
            page: PageId(0),
            op: OpKey::new(ObjRef::new(1, 0), 0),
            ordinal: 1,
        };
        paragraphs[1].glyphs.push(source);
        let mut duplicate = paragraph(
            10,
            RegionKind::Text,
            "unclassified fragment",
            Rect::new(200.0, 623.0, 300.0, 634.0),
        );
        duplicate.glyphs.push(source);
        let mut rotated = paragraph(
            11,
            RegionKind::Text,
            "Vertical University",
            Rect::new(300.0, 580.0, 311.0, 630.0),
        );
        rotated.glyphs.push(source);
        regions.extend([&duplicate, &rotated].map(|p| Region {
            page: p.page,
            index: p.region,
            kind: p.kind,
            bbox: p.bbox,
            score: 0.9,
            order: p.region,
        }));
        paragraphs.extend([duplicate, rotated]);
        let changed = protect_front_matter(&ir, &regions, &mut paragraphs);
        assert!(changed.contains(&ParagraphId::new(PageId(0), 10)));
        assert!(!changed.contains(&ParagraphId::new(PageId(0), 11)));
        assert_eq!(
            paragraphs[3].translatable,
            Translatable::No {
                reason: "previous_reason".into()
            }
        );
        assert_eq!(paragraphs[10].translatable, Translatable::Yes);
    }

    #[test]
    fn address_needs_metadata_anchor_and_prose_is_not_affiliation() {
        let (ir, regions, mut paragraphs) = fixture();
        paragraphs[3].text = "123 Main Street".into();
        paragraphs[4].text = "some ordinary wording".into();
        assert!(address(&paragraphs[3].text));
        assert!(!affiliation("we work with Example University"));
        let changed = protect_front_matter(&ir, &regions, &mut paragraphs);
        assert!(changed.contains(&ParagraphId::new(PageId(0), 4)));
        assert!(!changed.contains(&ParagraphId::new(PageId(0), 5)));

        let (ir, regions, mut paragraphs) = fixture();
        paragraphs[2].text = "ordinary fragment".into();
        paragraphs[4].text = "ordinary fragment".into();
        assert!(protect_front_matter(&ir, &regions, &mut paragraphs).is_empty());
    }

    #[test]
    fn existing_no_affiliation_still_anchors_author() {
        let (ir, regions, mut paragraphs) = fixture();
        paragraphs[2].translatable = Translatable::No {
            reason: "existing_policy".into(),
        };
        paragraphs[4].text = "ordinary fragment".into();
        let changed = protect_front_matter(&ir, &regions, &mut paragraphs);
        assert!(changed.contains(&ParagraphId::new(PageId(0), 2)));
        assert!(!changed.contains(&ParagraphId::new(PageId(0), 3)));
        assert_eq!(
            paragraphs[2].translatable,
            Translatable::No {
                reason: "existing_policy".into()
            }
        );
    }
}
