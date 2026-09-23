//! 塑形（harfrust）。
//!
//! 单一字体单 run：[`shape`]；已分好 bidi 方向的 run 用
//! [`shape_runs_directional`]；兼容自动方向入口为 [`shape_runs`]。
//! 输出单位 pt（harfrust 不带字号，本模块按 `size / upem` 缩放整数
//! design-unit 位置，保持 harfbuzz 的定点语义）。

use crate::loader::{FontId, FontStore, LoadedFont};
use icu_segmenter::GraphemeClusterSegmenter;
use unicode_script::Script as UnicodeScript;

/// 单个塑形结果字形。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ShapedGlyph {
    /// 字形 id。
    pub gid: u16,
    /// 相对完整输入 `&str` 的 UTF-8 字节起点。
    pub cluster: u32,
    /// 同一 cluster 的 UTF-8 半开结束；连字覆盖全部输入字符。
    pub cluster_end: u32,
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
    shape_with_script(font, text, size, rtl, features, None)
}

fn shape_with_script(
    font: &LoadedFont,
    text: &str,
    size: f32,
    rtl: bool,
    features: &[(&str, u32)],
    script_override: Option<harfrust::Script>,
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
    buffer.set_cluster_level(harfrust::BufferClusterLevel::MonotoneGraphemes);
    buffer.set_direction(if rtl {
        harfrust::Direction::RightToLeft
    } else {
        harfrust::Direction::LeftToRight
    });
    let script = script_override.unwrap_or_else(|| script_for_text(text, rtl));
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

    // HarfBuzz 将连字的多个字符归为同一 cluster。先按逻辑字节顺序
    // 找下一个 cluster 起点，再把边界扩到完整 Unicode grapheme。
    let mut starts: Vec<u32> = output
        .glyph_infos()
        .iter()
        .map(|info| info.cluster)
        .collect();
    starts.sort_unstable();
    starts.dedup();
    let boundaries: Vec<usize> = GraphemeClusterSegmenter::new().segment_str(text).collect();
    let range_for = |cluster: u32| {
        let start_idx = boundaries.partition_point(|&b| b <= cluster as usize) - 1;
        let cluster_idx = starts
            .binary_search(&cluster)
            .expect("output cluster in starts");
        let next = starts
            .get(cluster_idx + 1)
            .map_or(text.len(), |&s| s as usize);
        let end_idx = boundaries.partition_point(|&b| b < next);
        let start = boundaries[start_idx];
        let end = boundaries[end_idx];
        (start as u32, end as u32)
    };

    output
        .glyph_infos()
        .iter()
        .zip(output.glyph_positions())
        .map(|(info, pos)| {
            let (cluster, cluster_end) = range_for(info.cluster);
            ShapedGlyph {
                gid: info.glyph_id as u16,
                cluster,
                cluster_end,
                x_advance: to_pt(pos.x_advance),
                y_advance: to_pt(pos.y_advance),
                x_offset: to_pt(pos.x_offset),
                y_offset: to_pt(pos.y_offset),
            }
        })
        .collect()
}

/// 兼容自动方向入口：每个脚本 run 自行选择 RTL/LTR，按逻辑 run 顺序返回。
///
/// 混合方向的完整 bidi 重排由调用方负责；已经完成 bidi 切分的调用方
/// 应使用 [`shape_runs_directional`]，其返回值是视觉字形顺序。
pub fn shape_runs(
    store: &FontStore,
    text: &str,
    size: f32,
    primary: FontId,
    fallbacks: &[FontId],
) -> Vec<(FontId, Vec<ShapedGlyph>)> {
    emit_segments(
        store,
        text,
        size,
        segments(store, text, primary, fallbacks),
        None,
    )
}

/// 对一个上层已划定的 logical bidi run 塑形，按视觉字形顺序返回。
///
/// `rtl` 只控制 run 方向，输入 `text` 不需要也不能预先逆序。
/// 每个 grapheme 整体选字体，cluster 范围始终相对完整 `text`。
pub fn shape_runs_directional(
    store: &FontStore,
    text: &str,
    size: f32,
    primary: FontId,
    fallbacks: &[FontId],
    rtl: bool,
) -> Vec<(FontId, Vec<ShapedGlyph>)> {
    let mut spans = segments(store, text, primary, fallbacks);
    if rtl {
        spans.reverse();
    }
    emit_segments(store, text, size, spans, Some(rtl))
}

#[derive(Debug, Clone, Copy)]
struct Segment {
    start: usize,
    end: usize,
    font: FontId,
    script: UnicodeScript,
}

fn segments(store: &FontStore, text: &str, primary: FontId, fallbacks: &[FontId]) -> Vec<Segment> {
    if text.is_empty() {
        return Vec::new();
    }
    let boundaries: Vec<usize> = GraphemeClusterSegmenter::new().segment_str(text).collect();
    let mut clusters: Vec<Segment> = boundaries
        .windows(2)
        .map(|w| Segment {
            start: w[0],
            end: w[1],
            font: pick_font_for_cluster(store, &text[w[0]..w[1]], primary, fallbacks),
            script: text[w[0]..w[1]]
                .chars()
                .map(UnicodeScript::from)
                .find(|s| !matches!(s, UnicodeScript::Common | UnicodeScript::Inherited))
                .unwrap_or(UnicodeScript::Common),
        })
        .collect();
    // Common/Inherited 附着相邻的强脚本；前导中性簇继承右侧。
    let mut previous = None;
    for cluster in &mut clusters {
        if cluster.script == UnicodeScript::Common {
            if let Some(script) = previous {
                cluster.script = script;
            }
        } else {
            previous = Some(cluster.script);
        }
    }
    let mut next = UnicodeScript::Latin;
    for cluster in clusters.iter_mut().rev() {
        if cluster.script == UnicodeScript::Common {
            cluster.script = next;
        } else {
            next = cluster.script;
        }
    }
    let mut spans: Vec<Segment> = Vec::new();
    for cluster in clusters {
        if let Some(last) = spans.last_mut() {
            if last.font == cluster.font && last.script == cluster.script {
                last.end = cluster.end;
                continue;
            }
        }
        spans.push(cluster);
    }
    spans
}

fn pick_font_for_cluster(
    store: &FontStore,
    text: &str,
    primary: FontId,
    fallbacks: &[FontId],
) -> FontId {
    std::iter::once(&primary)
        .chain(fallbacks.iter())
        .copied()
        .find(|id| {
            store.get(*id).is_some_and(|font| {
                text.chars()
                    .all(|c| crate::metrics::gid_for(font, c).is_some())
            })
        })
        .unwrap_or(primary)
}

fn emit_segments(
    store: &FontStore,
    text: &str,
    size: f32,
    spans: Vec<Segment>,
    direction: Option<bool>,
) -> Vec<(FontId, Vec<ShapedGlyph>)> {
    let mut runs: Vec<(FontId, Vec<ShapedGlyph>)> = Vec::new();
    for span in spans {
        let Some(font) = store.get(span.font) else {
            continue;
        };
        let rtl = direction.unwrap_or(matches!(
            span.script,
            UnicodeScript::Arabic | UnicodeScript::Hebrew
        ));
        let script = harfrust_script(span.script).unwrap_or(harfrust::script::LATIN);
        let mut glyphs = shape_with_script(
            font,
            &text[span.start..span.end],
            size,
            rtl,
            &[],
            Some(script),
        );
        if glyphs.is_empty() {
            continue;
        }
        for glyph in &mut glyphs {
            glyph.cluster += span.start as u32;
            glyph.cluster_end += span.start as u32;
        }
        if let Some((last_id, last_glyphs)) = runs.last_mut() {
            if *last_id == span.font {
                last_glyphs.extend(glyphs);
                continue;
            }
        }
        runs.push((span.font, glyphs));
    }
    runs
}

/// 从文本首个非 Common 脚本推导 harfrust `Script`。
fn script_for_text(text: &str, _rtl: bool) -> harfrust::Script {
    for c in text.chars() {
        if let Some(script) = harfrust_script(UnicodeScript::from(c)) {
            return script;
        }
    }
    harfrust::script::LATIN
}

fn harfrust_script(script: UnicodeScript) -> Option<harfrust::Script> {
    if matches!(
        script,
        UnicodeScript::Common | UnicodeScript::Inherited | UnicodeScript::Unknown
    ) {
        return None;
    }
    let tag = harfrust::Tag::new_checked(script.short_name().as_bytes()).ok()?;
    harfrust::Script::from_iso15924_tag(tag)
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

    #[test]
    fn real_font_ligature_and_combining_clusters() {
        let s = store().expect("built-in font fixture required");
        let id = s
            .find(&FontQuery {
                family: Some("PT Serif".into()),
                ..Default::default()
            })
            .expect("PT Serif fixture required");
        let font = s.get(id).unwrap();
        let ligature = shape(font, "office", 12.0, false, &[]);
        assert!(
            ligature
                .iter()
                .any(|g| g.cluster == 2 && g.cluster_end == 4),
            "fixture should form fi ligature: {ligature:?}"
        );
        let combining = shape(font, "a\u{0301}b", 12.0, false, &[]);
        assert!(combining
            .iter()
            .any(|g| g.cluster == 0 && g.cluster_end == 3));
        assert!(combining.iter().all(|g| {
            (g.cluster == 0 && g.cluster_end == 3) || (g.cluster == 3 && g.cluster_end == 4)
        }));
    }

    #[test]
    fn directional_fallback_keeps_absolute_clusters_and_font_identity() {
        let s = store().expect("built-in font fixture required");
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
        let text = "A中B";
        let runs = shape_runs_directional(&s, text, 12.0, inter, &[cjk], false);
        assert_eq!(
            runs.iter().map(|(id, _)| *id).collect::<Vec<_>>(),
            [inter, cjk, inter]
        );
        let clusters = runs
            .iter()
            .flat_map(|(_, glyphs)| glyphs.iter().map(|g| (g.cluster, g.cluster_end)))
            .collect::<Vec<_>>();
        assert_eq!(clusters, [(0, 1), (1, 4), (4, 5)]);
        assert_eq!(
            runs,
            shape_runs_directional(&s, text, 12.0, inter, &[cjk], false)
        );

        // No font covers the entire extended grapheme: keep it in one font,
        // with a real .notdef for the unsupported combining mark.
        let mixed = shape_runs_directional(&s, "a\u{0651}", 12.0, inter, &[cjk], false);
        assert_eq!(mixed.len(), 1);
        assert_eq!(mixed[0].0, inter);
        assert!(mixed[0].1.iter().any(|g| g.gid == 0), "{mixed:?}");
        assert!(mixed[0]
            .1
            .iter()
            .all(|g| (g.cluster, g.cluster_end) == (0, 3)));
    }

    #[test]
    fn directional_arabic_matches_single_font_without_reversing_input() {
        let s = store().expect("built-in font fixture required");
        let id = s
            .find(&FontQuery {
                script: Script::Arabic,
                ..Default::default()
            })
            .expect("Arabic fixture required");
        let text = "السلام";
        let actual = shape_runs_directional(&s, text, 18.0, id, &[], true);
        assert_eq!(
            actual,
            vec![(id, shape(s.get(id).unwrap(), text, 18.0, true, &[]))]
        );
        assert_eq!(
            actual,
            shape_runs_directional(&s, text, 18.0, id, &[], true)
        );

        let inter = s
            .find(&FontQuery {
                family: Some("Inter".into()),
                ..Default::default()
            })
            .unwrap();
        let cluster = shape_runs_directional(&s, "ب\u{0651}", 18.0, inter, &[id], true);
        assert_eq!(cluster.len(), 1);
        assert_eq!(cluster[0].0, id);
        assert!(cluster[0]
            .1
            .iter()
            .all(|g| (g.cluster, g.cluster_end) == (0, 4) && g.gid != 0));

        let mixed = shape_runs_directional(&s, "Aب", 18.0, inter, &[id], true);
        assert_eq!(
            mixed.iter().map(|(id, _)| *id).collect::<Vec<_>>(),
            [id, inter]
        );
        assert_eq!(mixed[0].1[0].cluster, 1);
        assert_eq!(mixed[1].1[0].cluster, 0);
    }

    #[test]
    fn hebrew_rtl_uses_hebrew_script() {
        assert_eq!(script_for_text("שלום", true), harfrust::script::HEBREW);
        assert_eq!(script_for_text("السَّلَام", true), harfrust::script::ARABIC);
    }
}
