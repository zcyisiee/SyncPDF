//! 子集化（subsetter 0.2）。
//!
//! **重要语义**（subsetter 0.2.6 实测）：`GlyphRemapper` 会把 gid 重映射为
//! 从 0 开始的连续整数（新 gid = 原 gid 在排序去重集合中的位次；
//! `.notdef` 恒为 0）。02-hjfy 调研文档 §3.4 所述 "subsetter 保持 gid"
//! 与实际代码不符——subsetter 自身文档明确说明 CID 应使用 **重映射后的
//! GID**（"you can always use the remapped GID as the CID"）。
//!
//! 因此 [`subset`] 返回 [`SubsetResult::gid_map`]（原 gid → 新 gid）：
//! - PDF 内容流 `Tj` 的 code = **新 gid**（子集字体内的实际字形编号）；
//! - 自定义 CMap cidchar 写 code(=新 gid) → cid(=新 gid)；
//! - `/CIDToGIDMap /Identity` 依然成立（cid = 新 gid = 子集内 gid）；
//! - `/W` 按新 gid 索引。
//!
//! 另注意 subsetter 会做 **glyph closure**（复合字形引用的部件字形也会被
//! 收进子集），TrueType 字体（glyf 表）触发；闭包追加的 gid 在
//! [`SubsetResult::gids`] 与 `gid_map` 中都会出现。

use crate::loader::LoadedFont;
use crate::{FontError, Result};

/// 子集结果。
#[derive(Debug, Clone)]
pub struct SubsetResult {
    /// 子集字体数据（完整 SFNT 文件）。
    pub data: Vec<u8>,
    /// 子集包含的全部原 gid（已排序去重，含 closure 追加的复合字形部件）。
    pub gids: Vec<u16>,
    /// 原 gid → 子集内新 gid（subsetter 重映射）。键与 `gids` 一致。
    pub gid_map: std::collections::BTreeMap<u16, u16>,
}

/// 生成子集并给出 gid 重映射表。
///
/// `gids` 不需要预先排序/去重；`.notdef`（gid 0）总会被包含。
/// 复合字形的部件字形由 subsetter 自动闭包收进子集（见模块注释）。
pub fn subset(font: &LoadedFont, gids: &[u16]) -> Result<SubsetResult> {
    let mut unique_sorted: Vec<u16> = gids.to_vec();
    unique_sorted.sort_unstable();
    unique_sorted.dedup();
    if unique_sorted.first() != Some(&0) {
        unique_sorted.insert(0, 0);
    }
    let remapper = subsetter::GlyphRemapper::new_from_glyphs_sorted(&unique_sorted);
    let data = subsetter::subset(&font.data, font.face_index, &remapper)
        .map_err(|e| FontError::Subset(e.to_string()))?;
    // subsetter 内部对 TrueType 字体做 glyph closure（复合字形部件也加入
    // remapper），因此映射表以 remapper 的最终状态为准。
    let gid_map: std::collections::BTreeMap<u16, u16> = remapper
        .remapped_gids()
        .enumerate()
        .map(|(new, old)| (old, new as u16))
        .collect();
    Ok(SubsetResult {
        data,
        gids: gid_map.keys().copied().collect(),
        gid_map,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::loader::{FontQuery, FontStore, Script};

    fn store() -> Option<FontStore> {
        let dir = syncpdf_core::fixtures::fonts_dir()?;
        FontStore::load_builtin(&dir).ok()
    }

    #[test]
    fn subset_inter_maps_gids() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&FontQuery {
                family: Some("Inter".into()),
                ..Default::default()
            })
            .unwrap();
        let f = s.get(id).unwrap();
        let text = "Hello, World";
        let glyphs = crate::shape::shape(f, text, 12.0, false, &[]);
        let gids: Vec<u16> = glyphs.iter().map(|g| g.gid).collect();

        let sub = subset(f, &gids).unwrap();
        assert!(sub.data.len() < f.data.len(), "子集应显著变小");

        // 新 gid 从 0 开始连续。
        let news: Vec<u16> = sub.gid_map.values().copied().collect();
        assert_eq!(news, (0..news.len() as u16).collect::<Vec<_>>());
        assert_eq!(sub.gid_map.get(&0), Some(&0), "notdef 映射到 0");

        // 子集字体可解析，字形数与映射表一致。
        let face = skrifa::FontRef::new(&sub.data).unwrap();
        use skrifa::MetadataProvider;
        let m = face.metrics(
            skrifa::instance::Size::unscaled(),
            skrifa::instance::LocationRef::default(),
        );
        assert_eq!(
            m.glyph_count as usize,
            sub.gid_map.len(),
            "子集字形数应等于映射表大小"
        );

        // 新 gid 的 advance 与原 gid 一致（hmtx 重排后仍正确）。
        let gm = face.glyph_metrics(
            skrifa::instance::Size::new(1000.0),
            skrifa::instance::LocationRef::default(),
        );
        for (&old, &new) in &sub.gid_map {
            let w_orig = crate::metrics::advance(f, old, 1000.0);
            let w_sub = gm
                .advance_width(skrifa::GlyphId::from(new as u32))
                .unwrap_or(0.0);
            assert!(
                (w_orig - w_sub).abs() < 1.5,
                "gid {old}->{new}: orig {w_orig} vs sub {w_sub}"
            );
        }
    }

    #[test]
    fn subset_cjk_minimal() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&FontQuery {
                script: Script::HanSC,
                ..Default::default()
            })
            .unwrap();
        let f = s.get(id).unwrap();
        let glyphs = crate::shape::shape(f, "中文排版", 12.0, false, &[]);
        let gids: Vec<u16> = glyphs.iter().map(|g| g.gid).collect();
        let sub = subset(f, &gids).unwrap();
        assert!(
            sub.data.len() < 100_000,
            "CJK 4 字子集不应超 100KB，实际 {} 字节",
            sub.data.len()
        );
        assert!(sub.gids.contains(&0));
        // 全部输入 gid 都有映射。
        for g in &gids {
            assert!(sub.gid_map.contains_key(g), "gid {g} 缺映射");
        }
    }

    #[test]
    fn subset_dedup_and_sorted() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&FontQuery {
                family: Some("Inter".into()),
                ..Default::default()
            })
            .unwrap();
        let f = s.get(id).unwrap();
        let sub = subset(f, &[100, 50, 100, 50, 0]).unwrap();
        assert_eq!(sub.gids, vec![0, 50, 100]);
        assert_eq!(sub.gid_map.get(&50), Some(&1));
        assert_eq!(sub.gid_map.get(&100), Some(&2));
    }
}
