//! 不可证明源墨迹 → 段落排除（源绑定失败粒度下沉的唯一接入点）。
//!
//! * 操作级：与 [`UnprovenSourceOp::ink`] 相交（或含其字形）的段落保留原文，
//!   原因码复用 `unmapped_source_glyph`（与 source_opaque 的语义一致：未映射
//!   /不可证明的源字形，保留原文、不计成功）。
//! * 页级：`check_replacement` 拒绝的页整页保留原文，原因码
//!   `bind_page_unreliable`。
//!
//! 排除只发生在 `run.rs` 段落分析循环对本函数的一次调用里；其它阶段不重复检查。

use std::collections::BTreeSet;

use syncpdf_core::ir::{Paragraph, Translatable};
use syncpdf_pdf::bind::{PageReliability, UnprovenSourceOp};

/// 把不可证明源墨迹（操作级）与页级不可信（结构错误）落到段落可译判定上。
///
/// 已是 `Translatable::No` 的段落保留其原有更具体的原因，不改写。
pub fn protect(
    paragraphs: &mut [Paragraph],
    reliability: PageReliability,
    unproven: &[UnprovenSourceOp],
) {
    if reliability.is_unreliable() {
        for p in paragraphs.iter_mut() {
            if matches!(p.translatable, Translatable::Yes) {
                p.translatable = Translatable::No {
                    reason: "bind_page_unreliable".into(),
                };
            }
        }
        return;
    }
    if unproven.is_empty() {
        return;
    }
    let ops: BTreeSet<_> = unproven.iter().map(|u| u.op).collect();
    for p in paragraphs.iter_mut() {
        if !matches!(p.translatable, Translatable::Yes) {
            continue;
        }
        // 墨迹相交或段落含该操作字形：任一命中即整段保留（§7：字形永不删除）。
        if unproven.iter().any(|u| p.bbox.intersects(&u.ink))
            || p.glyphs.iter().any(|id| ops.contains(&id.op))
        {
            p.translatable = Translatable::No {
                reason: "unmapped_source_glyph".into(),
            };
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::{Align, RegionKind};
    use syncpdf_core::{GlyphId, ObjRef, OpKey, PageId, ParagraphId, Rect};

    fn paragraph(glyphs: Vec<GlyphId>, bbox: Rect) -> Paragraph {
        Paragraph {
            id: ParagraphId::new(PageId(0), 1),
            page: PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox,
            lines: vec![],
            glyphs,
            text_spans: Vec::new(),
            style_runs: vec![],
            atoms: vec![],
            decorations: Vec::new(),
            text: "text".into(),
            align: Align::Left,
            first_indent: 0.0,
            line_height: 12.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        }
    }

    fn op_key(i: u32) -> OpKey {
        OpKey::new(ObjRef::new(i, 0), i)
    }

    fn glyph(op: OpKey, ordinal: u16) -> GlyphId {
        GlyphId {
            page: PageId(0),
            op,
            ordinal,
        }
    }

    fn ink(op: OpKey, bbox: Rect) -> UnprovenSourceOp {
        UnprovenSourceOp {
            op,
            ink: bbox,
            note: "align op=…".into(),
        }
    }

    #[test]
    fn ink_overlap_or_op_membership_blocks_only_that_paragraph() {
        let unproven = ink(op_key(7), Rect::new(72.0, 600.0, 120.0, 620.0));
        let hit = paragraph(
            vec![glyph(op_key(1), 0), glyph(op_key(7), 0)],
            Rect::new(60.0, 595.0, 300.0, 625.0),
        );
        let near = paragraph(
            vec![glyph(op_key(2), 0)],
            Rect::new(72.0, 590.0, 200.0, 640.0),
        );
        let far = paragraph(
            vec![glyph(op_key(3), 0)],
            Rect::new(72.0, 100.0, 200.0, 120.0),
        );
        let mut ps = vec![hit, near, far];
        protect(&mut ps, PageReliability::Reliable, &[unproven]);
        for p in &ps[..2] {
            assert!(
                matches!(&p.translatable, Translatable::No { reason } if reason == "unmapped_source_glyph"),
                "{:?}",
                p.translatable
            );
        }
        assert!(
            matches!(ps[2].translatable, Translatable::Yes),
            "远离墨迹的段落照常可译"
        );
    }

    #[test]
    fn page_unreliable_blocks_everything_and_keeps_existing_reasons() {
        let mut ps = vec![
            paragraph(vec![], Rect::new(0.0, 0.0, 100.0, 10.0)),
            paragraph(vec![], Rect::new(0.0, 20.0, 100.0, 30.0)),
        ];
        ps[1].translatable = Translatable::No {
            reason: "region_kind".into(),
        };
        protect(&mut ps, PageReliability::Unreliable, &[]);
        assert!(
            matches!(&ps[0].translatable, Translatable::No { reason } if reason == "bind_page_unreliable")
        );
        assert!(
            matches!(&ps[1].translatable, Translatable::No { reason } if reason == "region_kind"),
            "已有更具体原因的段落不改写"
        );
    }

    #[test]
    fn empty_ink_on_reliable_page_is_a_noop() {
        let mut ps = vec![paragraph(vec![], Rect::new(0.0, 0.0, 100.0, 10.0))];
        let before = ps.clone();
        protect(&mut ps, PageReliability::Reliable, &[]);
        assert_eq!(ps, before);
    }
}
