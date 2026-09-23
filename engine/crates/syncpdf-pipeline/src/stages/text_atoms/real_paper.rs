//! Opt-in inventory/fit evidence from an immutable real run's cached source IR.
//! No inference, rebinding or model calls. Copies databases before opening them.
use std::path::PathBuf;

use crate::stages;
use syncpdf_core::hash::Sha256Hash;
use syncpdf_core::ir::{PageIR, Region, Translatable};
use syncpdf_store::Store;
use syncpdf_typeset::{Obstacles, Shaper};

#[test]
#[ignore = "manual cached-paper evidence; requires R4_PDF, R4_BASELINE, R4_OUTPUT"]
fn cached_paper_atom_inventory_and_fixed_size_fit() {
    let input = PathBuf::from(std::env::var_os("R4_PDF").expect("R4_PDF"));
    let baseline = PathBuf::from(std::env::var_os("R4_BASELINE").expect("R4_BASELINE"));
    let output = PathBuf::from(std::env::var_os("R4_OUTPUT").expect("R4_OUTPUT"));
    // Optional explicit typography; defaults retain the original fixed-size inventory.
    let font_scale: f32 = std::env::var("R4_FONT_SCALE").map_or(1.0, |v| v.parse().unwrap());
    let line_height = std::env::var("R4_LINE_HEIGHT")
        .ok()
        .map(|v| v.parse().unwrap());
    let typography = stages::typeset::Typography::new(font_scale, line_height).unwrap();
    std::fs::create_dir_all(&output).unwrap();
    let private = tempfile::tempdir().unwrap();
    let source_copy = private.path().join("stages.db");
    let translation_copy = private.path().join("translations.db");
    std::fs::copy(baseline.join("tmp/syncpdf-pipeline-store.db"), &source_copy).unwrap();
    std::fs::copy(baseline.join("cache/translate.db"), &translation_copy).unwrap();
    let store = Store::open(&source_copy).unwrap();
    let cache = syncpdf_translate::Cache::open(&translation_copy).unwrap();
    let hash = Sha256Hash::of(std::fs::read(&input).unwrap());
    let pages: Vec<PageIR> = store.get_stage(&hash, "source_analysis").unwrap().unwrap();
    assert!(!pages.is_empty());
    let doc = lopdf::Document::load(&input).unwrap();
    let (fonts, profile) =
        stages::load_fonts(&syncpdf_core::fixtures::fonts_dir().unwrap(), "zh-CN").unwrap();
    let shaper = stages::StoreShaper::new(&fonts, &profile);
    let mut records = Vec::new();
    for ir in pages {
        let key = format!(
            "layout-v3:fe3bc78476c982401caf389a8e8e928cb94cc0dbb89be73a363838f19fcaf271:CoreMl:150:0.4:{}",
            ir.page.0
        );
        let mut regions: Vec<Region> = store.get_stage(&hash, &key).unwrap().unwrap();
        stages::apply_coverage_fallback(
            &mut regions,
            &ir,
            ir.page.0,
            stages::LayoutOpts::default().coverage_limit,
        );
        let mut paras = stages::analyze_page(&ir, &regions);
        stages::source_policy::protect_front_matter(&ir, &regions, &mut paras);
        let frames = stages::frame::page_frames(&ir, &regions, &paras);
        for para in paras {
            if !matches!(para.translatable, Translatable::Yes) {
                continue;
            }
            let unit = syncpdf_translate::build_unit(&para, |id| {
                ir.glyphs()
                    .find(|g| g.id == id)
                    .map(|g| g.unicode.iter().collect())
            });
            let html = cache.get("auto", "zh-CN", &unit.html).unwrap();
            let resolved = html
                .as_deref()
                .and_then(|h| syncpdf_translate::parse_unit_html(h).ok())
                .and_then(|p| stages::link_text::prepare(&para, &ir, &doc, &p));
            let result = resolved.as_ref().and_then(|target| {
                frames.get(&para.id).map(|frame| {
                    stages::typeset::typeset_with_typography(
                        &shaper,
                        &target.para,
                        &target.parsed,
                        &Obstacles::default(),
                        Some(frame),
                        typography,
                    )
                })
            });
            if let Some(result) = &result {
                assert_eq!(result.scale, 1.0);
                for g in result.paragraph.lines.iter().flat_map(|l| &l.glyphs) {
                    let size = resolved
                        .as_ref()
                        .unwrap()
                        .para
                        .style_runs
                        .iter()
                        .find(|s| s.id == g.style)
                        .map_or(stages::typeset::dominant_font_size(&para), |s| s.size);
                    assert!((g.size - size * font_scale).abs() < 0.0001);
                }
                if let Some(multiplier) = line_height {
                    let expected =
                        stages::typeset::dominant_font_size(&para) * font_scale * multiplier;
                    assert!((result.paragraph.line_height - expected).abs() < 0.0001);
                }
            }
            let ink = result.as_ref().map(|r| {
                r.paragraph
                    .lines
                    .iter()
                    .map(|line| {
                        line.glyphs
                            .iter()
                            .filter(|g| !g.text.chars().all(char::is_whitespace))
                            .filter_map(|g| {
                                shaper.glyph_bounds(g.font, g.gid, g.size).map(|b| {
                                    syncpdf_core::Rect::new(
                                        g.x + b.x0,
                                        g.y + b.y0,
                                        g.x + b.x1,
                                        g.y + b.y1,
                                    )
                                })
                            })
                            .collect::<Vec<_>>()
                    })
                    .collect::<Vec<_>>()
            });
            records.push(serde_json::json!({
                "paragraph": para, "cache_hit": html.is_some(),
                "source_html": unit.html, "translated_html": html,
                "resolved": resolved.is_some(), "frame": frames.get(&para.id).map(|f| f.bbox),
                "collision_obstacles": frames.get(&para.id).zip(result.as_ref()).map(|(f,r)| f.obstacles.iter().filter(|b| r.paragraph.lines.iter().any(|l| l.bbox.x1.min(b.x1)-l.bbox.x0.max(b.x0)>0.01 && l.bbox.y1.min(b.y1)-l.bbox.y0.max(b.y0)>0.01)).collect::<Vec<_>>()),
                "typeset": result.as_ref().map(|r| &r.paragraph), "glyph_ink": ink,
                "link_geometry": resolved.as_ref().zip(result.as_ref()).and_then(|(t,r)| stages::link_text::geometry(t, &r.paragraph, &shaper)),
                "source_collision": result.as_ref().and_then(|r| frames.get(&para.id)
                    .map(|f| stages::frame::collides(f, &r.paragraph.lines))),
            }));
        }
    }
    std::fs::write(
        output.join("atom-inventory.json"),
        serde_json::to_vec_pretty(&records).unwrap(),
    )
    .unwrap();
    println!("inventoried {} translatable paragraphs", records.len());
}
