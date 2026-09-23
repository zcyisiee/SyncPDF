//! 字体度量（skrifa）。
//!
//! 尺寸换算在本 crate 内完成：所有返回值均为 pt（按 `size / upem` 缩放）；
//! 仅 [`metrics`] 返回的设计空间值按 upem 归一（见字段注释）。

use crate::loader::LoadedFont;

/// 全局字体度量（设计空间，未乘 size）。
///
/// `ascent / descent / cap_height / x_height / italic_angle / bbox` 均为
/// **upem 归一值**（即 design units / upem），调用方乘字号即得 pt；
/// `cap_height / x_height` 缺失时按 ascent 的近似比例给出（OS/2 可能无
/// 该字段，但嵌入 PDF 的 FontDescriptor 需要数值）。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Metrics {
    /// units per em。
    pub upem: u16,
    /// 基线以上高度 / upem（正值，向上）。
    pub ascent: f32,
    /// 基线以下深度 / upem（负值，向下）。
    pub descent: f32,
    /// 大写字母高度 / upem。
    pub cap_height: f32,
    /// 小写 x 高度 / upem。
    pub x_height: f32,
    /// 斜体角（度，逆时针为正；右手倾斜为负）。
    pub italic_angle: f32,
    /// 字体包围盒 [x_min, y_min, x_max, y_max] / upem。
    pub bbox: [f32; 4],
    /// 是否含 CFF 表（决定 FontFile2 vs FontFile3）。
    pub is_cff: bool,
}

/// 计算全局度量。
///
/// 失败字体（无 head 表等）返回 upem=1000 的退化值，不 panic。
pub fn metrics(font: &LoadedFont) -> Metrics {
    use read_fonts::TableProvider;
    use skrifa::MetadataProvider;
    let Ok(face) = skrifa::FontRef::from_index(&font.data, font.face_index) else {
        return Metrics {
            upem: 1000,
            ascent: 0.8,
            descent: -0.2,
            cap_height: 0.7,
            x_height: 0.5,
            italic_angle: 0.0,
            bbox: [0.0, -0.2, 1.0, 0.8],
            is_cff: false,
        };
    };
    let upem = face
        .head()
        .ok()
        .and_then(|h| (h.units_per_em() != 0).then_some(h.units_per_em()))
        .unwrap_or(1000);
    let scale = 1.0 / upem as f32;

    // 用 skrifa Metrics 读 hhea/OS/2 行度量（Size::unscaled → 全部为比例值）。
    let m = face.metrics(
        skrifa::instance::Size::unscaled(),
        skrifa::instance::LocationRef::default(),
    );
    let ascent = m.ascent * scale;
    let descent = m.descent * scale;
    let cap_height = m.cap_height.map(|v| v * scale).unwrap_or(ascent * 0.72);
    let x_height = m.x_height.map(|v| v * scale).unwrap_or(ascent * 0.52);
    let bbox = match m.bounds {
        Some(b) => [
            b.x_min * scale,
            b.y_min * scale,
            b.x_max * scale,
            b.y_max * scale,
        ],
        None => [0.0, descent, 1.0, ascent],
    };
    let is_cff = face.cff().is_ok();

    Metrics {
        upem,
        ascent,
        descent,
        cap_height,
        x_height,
        italic_angle: m.italic_angle,
        bbox,
        is_cff,
    }
}

/// 字形步进宽度（pt）。
///
/// `size` 为字号（pt）。`gid` 超界返回 0。
pub fn advance(font: &LoadedFont, gid: u16, size: f32) -> f32 {
    use skrifa::MetadataProvider;
    let Ok(face) = skrifa::FontRef::from_index(&font.data, font.face_index) else {
        return 0.0;
    };
    let gm = face.glyph_metrics(
        skrifa::instance::Size::new(size),
        skrifa::instance::LocationRef::default(),
    );
    gm.advance_width(skrifa::GlyphId::from(gid as u32))
        .unwrap_or(0.0)
}

/// Actual unhinted glyph ink in PDF points, relative to the glyph baseline.
/// Empty glyphs (spaces) return an empty rectangle; invalid data returns None.
pub fn glyph_bounds(font: &LoadedFont, gid: u16, size: f32) -> Option<syncpdf_core::Rect> {
    use skrifa::MetadataProvider;
    if !size.is_finite() || size <= 0.0 {
        return None;
    }
    let face = skrifa::FontRef::from_index(&font.data, font.face_index).ok()?;
    let metrics = face.glyph_metrics(
        skrifa::instance::Size::new(size),
        skrifa::instance::LocationRef::default(),
    );
    let b = metrics.bounds(skrifa::GlyphId::from(gid as u32))?;
    Some(syncpdf_core::Rect::new(b.x_min, b.y_min, b.x_max, b.y_max))
}

/// 字体是否有某字符的 cmap 映射（glyph id 非 0）。
pub fn has_char(font: &LoadedFont, c: char) -> bool {
    gid_for(font, c).is_some()
}

/// 字符 → 名义 gid。
pub fn gid_for(font: &LoadedFont, c: char) -> Option<u16> {
    use skrifa::MetadataProvider;
    let face = skrifa::FontRef::from_index(&font.data, font.face_index).ok()?;
    let gid = face.charmap().map(c)?;
    let g = gid.to_u32();
    (g != 0).then_some(g as u16)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store() -> Option<crate::loader::FontStore> {
        let dir = syncpdf_core::fixtures::fonts_dir()?;
        crate::loader::FontStore::load_builtin(&dir).ok()
    }

    #[test]
    fn metrics_noto_cjk() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&crate::loader::FontQuery {
                script: crate::loader::Script::HanSC,
                ..Default::default()
            })
            .unwrap();
        let m = metrics(s.get(id).unwrap());
        assert_eq!(m.upem, 1000, "Noto CJK upem");
        assert!((0.5..1.2).contains(&m.ascent), "ascent {}", m.ascent);
        assert!((-1.2..-0.1).contains(&m.descent), "descent {}", m.descent);
        assert!(m.cap_height > 0.0 && m.cap_height < m.ascent + 0.01);
        assert!(m.is_cff, "Noto CJK ttc 是 CFF outline");
        // bbox 有意义。
        assert!(m.bbox[0] < m.bbox[2] && m.bbox[1] < m.bbox[3]);
    }

    #[test]
    fn metrics_inter() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&crate::loader::FontQuery {
                family: Some("Inter".into()),
                ..Default::default()
            })
            .unwrap();
        let m = metrics(s.get(id).unwrap());
        // Inter 的 .otf 内含 CFF 表（非 TrueType glyf）→ FontFile3/OpenType。
        assert!(m.is_cff, "Inter otf 应含 CFF 表");
        let f = s.get(id).unwrap();
        // Inter 的 'H' 应该有映射。
        assert!(has_char(f, 'H'));
        let gid = gid_for(f, 'H').unwrap();
        let w = advance(f, gid, 100.0);
        assert!(w > 40.0 && w < 110.0, "advance {} at 100pt", w);
        // size 线性。
        let w2 = advance(f, gid, 200.0);
        assert!((w2 - 2.0 * w).abs() < 0.6, "linear scale {} {}", w, w2);
    }

    #[test]
    fn gid_for_missing_char_is_none() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&crate::loader::FontQuery {
                family: Some("Inter".into()),
                ..Default::default()
            })
            .unwrap();
        let f = s.get(id).unwrap();
        // Inter 不含 CJK。
        assert!(gid_for(f, '中').is_none());
    }
}
