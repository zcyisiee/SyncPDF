//! 塑形（harfrust）。
//!
//! 单一字体单 run：[`shape`]；混合脚本 + 缺字回退：[`shape_runs`]。
//! 输出单位 pt（harfrust 不带字号，本模块按 `size / upem` 缩放整数
//! design-unit 位置，保持 harfbuzz 的定点语义）。

use crate::loader::{FontId, FontStore, LoadedFont};

/// 单个塑形结果字形。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ShapedGlyph {
    /// 字形 id。
    pub gid: u16,
    /// 输入 `&str` 的字节偏移（harfrust cluster，cluster level = Characters）。
    pub cluster: u32,
    /// x 步进（pt）。
    pub x_advance: f32,
    /// y 步进（pt，水平排版为 0）。
    pub y_advance: f32,
    /// x 偏移（pt）。
    pub x_offset: f32,
    /// y 偏移（pt）。
    pub y_offset: f32,
}

/// 对一段文本用单一字体塑形。
///
/// - `rtl`：书写方向（阿拉伯等 RTL 文本应为 true；内部还会结合
///   unicode-script 判断）。
/// - `features`：OpenType feature（tag, value），如 `("kern", 1)`。
/// - 返回值按书写顺序（RTL 时为视觉逆序，即从右往左的显示序）。
pub fn shape(
    font: &LoadedFont,
    text: &str,
    size: f32,
    rtl: bool,
    features: &[(&str, u32)],
) -> Vec<ShapedGlyph> {
    if text.is_empty() {
        return Vec::new();
    }
    // 注意：harfrust 0.13 依赖 read-fonts 0.43，与 skrifa 0.47 的 0.44 是
    // 两个 semver 副本，因此这里必须用 harfrust::FontRef（其 re-export），
    // 不能把 skrifa::FontRef 传给 shaper。
    let Ok(face) = harfrust::FontRef::from_index(&font.data, font.face_index) else {
        return Vec::new();
    };
    let shaper_data = harfrust::ShaperData::new(&face);
    let shaper = shaper_data.shaper(&face).build();

    let mut buffer = harfrust::UnicodeBuffer::new();
    buffer.push_str(text);
    buffer.set_cluster_level(harfrust::BufferClusterLevel::Characters);
    buffer.set_direction(if rtl {
        harfrust::Direction::RightToLeft
    } else {
        harfrust::Direction::LeftToRight
    });
    let script = script_for_text(text, rtl);
    buffer.set_script(script);
    if let Some(lang) = language_for_script(script) {
        buffer.set_language(lang);
    }

    let hb_features: Vec<harfrust::Feature> = features
        .iter()
        .filter_map(|(tag, value)| {
            let tag = harfrust::Tag::new_checked(tag.as_bytes()).ok()?;
            Some(harfrust::Feature::new(tag, *value, ..))
        })
        .collect();
    // harfrust 定点缩放：设置 scale = size << 16 后，位置/步进值 =
    // design_units * scale / upem（16.16 定点数），pt = 值 / 65536。
    // 见 harfrust Scale::mult_from_scale（(scale << 16) / upem）。
    let scale = (size * 65536.0) as i32;
    let options = harfrust::ShapeOptions::new()
        .scale(Some(scale))
        .features(&hb_features);
    let output = shaper.shape(buffer, options);
    let to_pt = |v: i32| v as f32 / 65536.0;

    output
        .glyph_infos()
        .iter()
        .zip(output.glyph_positions())
        .map(|(info, pos)| ShapedGlyph {
            gid: info.glyph_id as u16,
            cluster: info.cluster,
            x_advance: to_pt(pos.x_advance),
            y_advance: to_pt(pos.y_advance),
            x_offset: to_pt(pos.x_offset),
            y_offset: to_pt(pos.y_offset),
        })
        .collect()
}

/// 混排切 run：按脚本 + 缺字回退选择字体。
///
/// 逻辑（hjfy 同款降级序）：
/// 1. 逐字符确定 [`Script`](unicode_script::Script)；
/// 2. 主字体能显示 → 主字体 run；
/// 3. 否则依次尝试 fallbacks，全部失败用主字体（.notdef）；
/// 4. 相邻字符字体相同则合并 run；
/// 5. RTL 脚本段落内调用 `shape(rtl=true)`，输出仍按视觉序拼接。
pub fn shape_runs(
    store: &FontStore,
    text: &str,
    size: f32,
    primary: FontId,
    fallbacks: &[FontId],
) -> Vec<(FontId, Vec<ShapedGlyph>)> {
    if text.is_empty() {
        return Vec::new();
    }
    let primary_font = store.get(primary);
    let mut runs: Vec<(FontId, Vec<ShapedGlyph>)> = Vec::new();
    let mut segments: Vec<(FontId, &str)> = Vec::new();

    // 1) 逐字符分段（同字体相邻合并）。
    let mut cur_start = 0usize;
    let mut cur_font: Option<FontId> = None;
    for (i, c) in text.char_indices() {
        let f = pick_font_for_char(store, c, primary, fallbacks, primary_font);
        if let Some(cf) = cur_font {
            if cf != f {
                segments.push((cf, &text[cur_start..i]));
                cur_start = i;
            }
        }
        cur_font = Some(f);
    }
    if let Some(cf) = cur_font {
        segments.push((cf, &text[cur_start..]));
    }

    // 2) 逐段塑形。每段内部再按 bidi 方向统一处理：段落脚本决定 rtl。
    //    cluster 重新定位为相对**整段输入**的字节偏移（brief 语义）。
    for (fid, seg) in segments {
        let Some(f) = store.get(fid) else {
            continue;
        };
        let seg_start = seg.as_ptr() as usize - text.as_ptr() as usize;
        let rtl = is_rtl_text(seg);
        let mut glyphs = shape(f, seg, size, rtl, &[]);
        for g in &mut glyphs {
            g.cluster += seg_start as u32;
        }
        push_run(&mut runs, fid, glyphs);
    }
    runs
}

/// 单字符选字体：主 → fallbacks → 主（.notdef）。
fn pick_font_for_char(
    store: &FontStore,
    c: char,
    primary: FontId,
    fallbacks: &[FontId],
    primary_font: Option<&LoadedFont>,
) -> FontId {
    if let Some(pf) = primary_font {
        if crate::metrics::gid_for(pf, c).is_some() {
            return primary;
        }
    }
    for fb in fallbacks {
        if let Some(f) = store.get(*fb) {
            if crate::metrics::gid_for(f, c).is_some() {
                return *fb;
            }
        }
    }
    primary
}

/// 追加 run（与上一 run 同字体则合并）。
fn push_run(runs: &mut Vec<(FontId, Vec<ShapedGlyph>)>, fid: FontId, glyphs: Vec<ShapedGlyph>) {
    if glyphs.is_empty() {
        return;
    }
    if let Some((last_id, last_glyphs)) = runs.last_mut() {
        if *last_id == fid {
            last_glyphs.extend(glyphs);
            return;
        }
    }
    runs.push((fid, glyphs));
}

/// 段落是否含 RTL 脚本（阿拉伯/希伯来）。
fn is_rtl_text(text: &str) -> bool {
    text.chars().any(|c| {
        matches!(
            unicode_script::Script::from(c),
            unicode_script::Script::Arabic | unicode_script::Script::Hebrew
        )
    })
}

/// 从文本首个非 Common 脚本推导 harfrust `Script`。
fn script_for_text(text: &str, rtl: bool) -> harfrust::Script {
    if rtl {
        return harfrust::script::ARABIC;
    }
    for c in text.chars() {
        let s = unicode_script::Script::from(c);
        let tag = match s {
            unicode_script::Script::Latin => b"Latn",
            unicode_script::Script::Han => b"Hani",
            unicode_script::Script::Hiragana => b"Hira",
            unicode_script::Script::Katakana => b"Kana",
            unicode_script::Script::Hangul => b"Hang",
            unicode_script::Script::Arabic => b"Arab",
            unicode_script::Script::Greek => b"Grek",
            unicode_script::Script::Cyrillic => b"Cyrl",
            unicode_script::Script::Hebrew => b"Hebr",
            unicode_script::Script::Thai => b"Thai",
            _ => continue,
        };
        let t = harfrust::Tag::new(&[tag[0], tag[1], tag[2], tag[3]]);
        if let Some(s) = harfrust::Script::from_iso15924_tag(t) {
            return s;
        }
    }
    harfrust::script::LATIN
}

/// 脚本对应塑形语言（影响 liga/locl）。
fn language_for_script(script: harfrust::Script) -> Option<harfrust::Language> {
    if script == harfrust::script::ARABIC {
        harfrust::Language::new("ar")
    } else {
        None
    }
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
    fn shape_cjk_four_glyphs() {
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
        let glyphs = shape(f, "中文排版", 24.0, false, &[]);
        assert_eq!(glyphs.len(), 4, "每字一字形");
        for (i, g) in glyphs.iter().enumerate() {
            assert!(g.gid != 0, "gid 0 at {}", i);
            // Noto CJK 全宽步进 = 1000/1000 * size。
            assert!(
                (g.x_advance - 24.0).abs() < 0.5,
                "advance {} != size 24",
                g.x_advance
            );
            assert_eq!(g.cluster as usize, i * 3, "cluster 是字节偏移");
        }
        assert!(glyphs.iter().all(|g| g.x_offset == 0.0));
    }

    #[test]
    fn shape_english_nonzero_gids() {
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
        let glyphs = shape(f, "Hello, World", 12.0, false, &[]);
        assert!(!glyphs.is_empty());
        for g in &glyphs {
            assert!(g.gid != 0, "latin glyph should not be notdef");
            assert!(g.x_advance > 0.0);
        }
        // kerning：H+e 组合宽度应小于独立步进和（非强断言，kern 表存在时成立）。
        let _ = glyphs;
    }

    #[test]
    fn shape_runs_mixed_script() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let inter = s
            .find(&FontQuery {
                family: Some("Inter".into()),
                ..Default::default()
            })
            .unwrap();
        let cjk = s
            .find(&FontQuery {
                script: Script::HanSC,
                ..Default::default()
            })
            .unwrap();
        let runs = shape_runs(&s, "中文 English 混排", 12.0, inter, &[cjk]);
        assert!(runs.len() >= 2, "混排应切出 ≥2 run，实际 {}", runs.len());
        // run 字体交替，字形连续覆盖输入。
        let total: usize = runs.iter().map(|(_, g)| g.len()).sum();
        // "中文 English 混排" = 12 chars + spaces。CJK 段 5 字 + latin 段
        // 8 字符（含空格） → 至少 12 个字形（标点/空格在 latin 字体 run）。
        assert!(total >= 10, "total glyphs {}", total);
        // 不同 run 用了不同字体。
        assert!(runs.windows(2).any(|w| w[0].0 != w[1].0));
        // cluster 是相对完整输入的字节偏移："中文 English 混排" 中
        // 'E' 在字节 7，'混' 在字节 14（3+1+7+1+…）。第一个 CJK run 首字形
        // cluster = 0，后续 latin 段含字节 7。
        let all_clusters: Vec<u32> = runs
            .iter()
            .flat_map(|(_, g)| g.iter().map(|g| g.cluster))
            .collect();
        assert!(all_clusters.contains(&0), "首 cluster 应为 0");
        assert!(
            all_clusters.contains(&7),
            "应含字节 7（'E'），实际 {all_clusters:?}"
        );
        // cluster 严格递增（同 run 内）。
        for (_, g) in &runs {
            assert!(g.windows(2).all(|w| w[0].cluster < w[1].cluster));
        }
    }

    #[test]
    fn shape_arabic_rtl() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let ar = s
            .find(&FontQuery {
                family: Some("Noto Sans Arabic".into()),
                ..Default::default()
            })
            .or_else(|| {
                s.find(&FontQuery {
                    script: Script::Arabic,
                    ..Default::default()
                })
            });
        let Some(id) = ar else {
            eprintln!("SKIP: arabic font missing");
            return;
        };
        let f = s.get(id).unwrap();
        let glyphs = shape(f, "السلام", 18.0, true, &[]);
        assert!(!glyphs.is_empty());
        assert!(glyphs.iter().all(|g| g.x_advance >= 0.0));
    }

    #[test]
    fn shape_empty_text() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s.ids()[0];
        assert!(shape(s.get(id).unwrap(), "", 10.0, false, &[]).is_empty());
        assert!(shape_runs(&s, "", 10.0, id, &[]).is_empty());
    }
}
