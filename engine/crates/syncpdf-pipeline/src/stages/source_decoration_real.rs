//! Opt-in evidence that a paragraph's own source underline no longer blocks its
//! translation. Reads an immutable run's cached source IR and cached translation
//! (copies both databases) and replays attribution + frame + typeset; no model
//! calls, no writes to the run.
//!
//! ```text
//! source ../../tmp/paper-iteration/env.sh
//! DECOR_DB=<run>/tmp/syncpdf-pipeline-store.db \
//! DECOR_CACHE=<run>/cache/translate.db \
//! DECOR_PDF=<paper>.pdf DECOR_OUTPUT=<dir> \
//!   cargo test -p syncpdf-pipeline --lib source_decoration_real -- --ignored --nocapture
//! ```
use std::path::PathBuf;

use syncpdf_core::hash::Sha256Hash;
use syncpdf_core::ir::{PageIR, Region, Translatable};
use syncpdf_pdf::bind::bind_page;
use syncpdf_pdf::pdfium::PdfiumWorker;
use syncpdf_store::Store;

use super::{anchored, claim, mark_styles, owned_ops};
use crate::stages;
use crate::stages::typeset;

/// Page (0-based) → the paragraphs known to have failed on their own underline.
const SUBJECTS: &[(u32, &[&str])] = &[
    (10, &["P11-014", "P11-015"]),
    (24, &["P25-004"]),
    (47, &["P48-010"]),
];

#[test]
#[ignore = "manual cached-paper evidence; requires DECOR_DB, DECOR_CACHE, DECOR_PDF, DECOR_OUTPUT"]
fn source_underline_no_longer_blocks_its_own_paragraph() {
    let db = PathBuf::from(std::env::var_os("DECOR_DB").expect("DECOR_DB"));
    let cache_path = PathBuf::from(std::env::var_os("DECOR_CACHE").expect("DECOR_CACHE"));
    let pdf = PathBuf::from(std::env::var_os("DECOR_PDF").expect("DECOR_PDF"));
    let output = PathBuf::from(std::env::var_os("DECOR_OUTPUT").expect("DECOR_OUTPUT"));
    std::fs::create_dir_all(&output).unwrap();

    // Never open the run's live databases.
    let private = tempfile::tempdir().unwrap();
    let db_copy = private.path().join("stages.db");
    let cache_copy = private.path().join("translate.db");
    std::fs::copy(&db, &db_copy).unwrap();
    std::fs::copy(&cache_path, &cache_copy).unwrap();
    let store = Store::open(&db_copy).unwrap();
    let cache = syncpdf_translate::Cache::open(&cache_copy).unwrap();

    let hash = Sha256Hash::of(std::fs::read(&pdf).unwrap());
    let lo = lopdf::Document::load(&pdf).unwrap();
    let worker = PdfiumWorker::spawn().expect("pdfium");
    let docid = worker.open(&pdf).expect("open paper");
    let (fonts, profile) =
        stages::load_fonts(&syncpdf_core::fixtures::fonts_dir().unwrap(), "zh-CN").unwrap();
    let shaper = stages::StoreShaper::new(&fonts, &profile);
    let typography = typeset::Typography::new(1.0, Some(1.5)).unwrap();

    let mut report: Vec<serde_json::Value> = Vec::new();
    let mut claims_per_page: std::collections::BTreeMap<u32, usize> =
        std::collections::BTreeMap::new();
    let mut changed_units: Vec<(u32, Vec<String>)> = Vec::new();
    let all = std::env::var_os("DECOR_ALL").is_some();
    let mut pages: Vec<PageIR> = Vec::new();
    let targets: Vec<u32> = if all {
        (0..worker.page_count(docid).expect("page count")).collect()
    } else {
        SUBJECTS.iter().map(|(p, _)| *p).collect()
    };
    for page in &targets {
        if pages.iter().any(|p| p.page.0 == *page) {
            continue;
        }
        // Rebind the real page with this tree's binder: the cached IR was
        // produced before stroke binding existed and cannot show the claim.
        pages.push(bind_page(&worker, docid, &lo, page + 1).expect("bind").ir);
    }
    for page in &targets {
        let ir = pages
            .iter()
            .find(|p| p.page.0 == *page)
            .expect("bound page");
        let key = format!(
            "layout-v3:fe3bc78476c982401caf389a8e8e928cb94cc0dbb89be73a363838f19fcaf271:CoreMl:150:0.4:{}",
            ir.page.0
        );
        let mut regions: Vec<Region> = store.get_stage(&hash, &key).unwrap().unwrap();
        stages::apply_coverage_fallback(
            &mut regions,
            ir,
            ir.page.0,
            stages::LayoutOpts::default().coverage_limit,
        );
        let mut base_paras = stages::analyze_page(ir, &regions);
        stages::source_policy::protect_front_matter(ir, &regions, &mut base_paras);
        let base_frames = stages::frame::page_frames(ir, &regions, &base_paras);
        let mut paras = base_paras.clone();
        let attributed = claim(ir, &mut paras);
        mark_styles(ir, &mut paras);
        let frames = stages::frame::page_frames(ir, &regions, &paras);
        claims_per_page.insert(ir.page.0, attributed);
        // Real translation scope: only units whose source unit actually changed
        // need a new model call; everything else still hits the cache.
        let unit_of = |p: &syncpdf_core::ir::Paragraph| {
            syncpdf_translate::build_unit(p, |gid| {
                ir.glyphs()
                    .find(|g| g.id == gid)
                    .map(|g| g.unicode.iter().collect())
            })
            .html
        };
        let changed: Vec<String> = paras
            .iter()
            .filter(|p| matches!(p.translatable, Translatable::Yes))
            .filter(|p| {
                let base = base_paras.iter().find(|b| b.id == p.id).unwrap();
                unit_of(base) != unit_of(p)
            })
            .map(|p| p.id.to_string())
            .collect();
        if !changed.is_empty() {
            println!(
                "page {}: {} units change -> {:?}",
                ir.page.0 + 1,
                changed.len(),
                changed
            );
        }
        changed_units.push((ir.page.0, changed));

        let ids: Vec<String> = if all {
            paras
                .iter()
                .filter(|p| !p.decorations.is_empty())
                .map(|p| p.id.to_string())
                .collect()
        } else {
            SUBJECTS
                .iter()
                .find(|(p, _)| p == page)
                .unwrap()
                .1
                .iter()
                .map(|s| (*s).to_owned())
                .collect()
        };
        for id in &ids {
            let para = paras
                .iter()
                .find(|p| p.id.to_string() == *id)
                .unwrap_or_else(|| panic!("{id} not on page {}", page + 1));
            // The cached translation was produced from the *old* source unit (no
            // underline run split), so look it up under the unclaimed unit as
            // well: this isolates the frame change from the source-unit change.
            let base_unit = syncpdf_translate::build_unit(
                base_paras.iter().find(|p| p.id == para.id).unwrap_or(para),
                |gid| {
                    ir.glyphs()
                        .find(|g| g.id == gid)
                        .map(|g| g.unicode.iter().collect())
                },
            );
            let prior_html = cache.get("auto", "zh-CN", &base_unit.html).unwrap();
            let prior_fit = prior_html
                .as_deref()
                .and_then(|h| syncpdf_translate::parse_unit_html(h).ok())
                .and_then(|parsed| {
                    let f = frames.get(&para.id)?;
                    Some(typeset::typeset_with_typography(
                        &shaper,
                        para,
                        &parsed,
                        &syncpdf_typeset::Obstacles::default(),
                        Some(f),
                        typography,
                    ))
                });
            let unit = syncpdf_translate::build_unit(para, |gid| {
                ir.glyphs()
                    .find(|g| g.id == gid)
                    .map(|g| g.unicode.iter().collect())
            });
            let html = cache.get("auto", "zh-CN", &unit.html).unwrap();
            let parsed = html
                .as_deref()
                .and_then(|h| syncpdf_translate::parse_unit_html(h).ok());
            let frame = frames.get(&para.id);
            let result = parsed.as_ref().and_then(|p| {
                frame.map(|f| {
                    typeset::typeset_with_typography(
                        &shaper,
                        para,
                        p,
                        &syncpdf_typeset::Obstacles::default(),
                        Some(f),
                        typography,
                    )
                })
            });
            report.push(serde_json::json!({
                "id": id,
                "source_text": para.text,
                "decorated_source": para.decorations.iter().map(|d| {
                    para.glyphs[d.glyph_range.0 as usize..d.glyph_range.1 as usize].iter()
                        .filter_map(|id| ir.glyphs().find(|g| g.id == *id))
                        .flat_map(|g| g.unicode.iter()).collect::<String>()
                }).collect::<Vec<_>>(),
                "translatable": matches!(para.translatable, Translatable::Yes),
                "attributed_on_page": attributed,
                "own_ops": owned_ops(para).count(),
                "decorations": para.decorations.iter().map(|d| serde_json::json!({
                    "bbox": d.bbox, "range": d.glyph_range, "offset": d.offset,
                    "width": d.stroke.width,
                })).collect::<Vec<_>>(),
                "underlined_runs": para.style_runs.iter().filter(|r| r.underline)
                    .map(|r| (r.glyph_range, r.id.0, r.underline)).collect::<Vec<_>>(),
                "cache_hit": html.is_some(),
                "anchored": parsed.as_ref().map(|p| anchored(para, p)),
                "prior_translation_cached": prior_html.is_some(),
                "prior_translation_overflow_on_new_frame": prior_fit.as_ref()
                    .map(|r| r.paragraph.overflow),
                "prior_translation_underlines_drawn": prior_fit.as_ref().map(|r| r
                    .paragraph.lines.iter().flat_map(|l| l.underlines.clone()).count()),
                "obstacles_before": base_frames.get(&para.id).map(|f| f.obstacles.len()),
                "obstacles_after": frame.map(|f| f.obstacles.len()),
                "obstacles_dropped": base_frames.get(&para.id).zip(frame).map(|(b, a)| {
                    b.obstacles.iter().filter(|r| !a.obstacles.contains(r)).count()
                }),
                "overflow": result.as_ref().map(|r| r.paragraph.overflow),
                "collides": frame.zip(result.as_ref())
                    .map(|(f, r)| stages::frame::collides(f, &r.paragraph.lines)),
                "redrawn_underlines": result.as_ref().map(|r| r.paragraph.lines.iter()
                    .flat_map(|l| l.underlines.clone()).map(|u| u.bbox).collect::<Vec<_>>()),
                "glyphs": result.as_ref().map(|r| r.paragraph.lines.iter()
                    .map(|l| l.glyphs.iter().filter(|g| !g.text.trim().is_empty()).count())
                    .sum::<usize>()),
            }));
        }
    }
    println!("claims per page: {claims_per_page:?}");
    let total_changed: usize = changed_units.iter().map(|(_, v)| v.len()).sum();
    println!(
        "total attributed lines: {}; units requiring a genuine new translation: {}",
        claims_per_page.values().sum::<usize>(),
        total_changed
    );
    std::fs::write(
        output.join("decoration-evidence.json"),
        serde_json::to_string_pretty(&report).unwrap(),
    )
    .unwrap();
    for entry in &report {
        println!(
            "{}: own_ops={} decorations={} underlined_runs={} anchored={} prior_cached={} prior_overflow_on_new_frame={} overflow={} collides={} redrawn={}",
            entry["id"].as_str().unwrap(),
            entry["own_ops"],
            entry["decorations"].as_array().map_or(0, Vec::len),
            entry["underlined_runs"].as_array().map_or(0, Vec::len),
            entry["anchored"],
            entry["prior_translation_cached"],
            entry["prior_translation_overflow_on_new_frame"],
            entry["overflow"],
            entry["collides"],
            entry["redrawn_underlines"].as_array().map_or(0, Vec::len),
        );
    }
}
