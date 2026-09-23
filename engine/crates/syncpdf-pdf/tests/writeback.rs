//! 回写端到端集成测试（步骤 6）。
//!
//! 覆盖：bind → 删字形 → 排一段中文译文写入 → save → self_check 全通过；
//! pdfium 重开可提取到译文；页数不变；`/Annots` 数量一致；qpdf --check 通过。
//!
//! 缺夹具/pdfium 库时用 `require_fixture!` 与运行时探测跳过。

use std::path::Path;
use std::time::Instant;

use syncpdf_core::ir::{DisplayItem, PlacedGlyph, TypesetParagraph};
use syncpdf_core::require_fixture;
use syncpdf_core::{Color, Rect, StyleId};
use syncpdf_font::{FontId, FontStore, LoadedFont};
use syncpdf_pdf::bind::bind_page;
use syncpdf_pdf::patch::PatchSet;
use syncpdf_pdf::pdfium::PdfiumWorker;
use syncpdf_pdf::validate::self_check;
use syncpdf_pdf::writer::{save, Writer};
use syncpdf_typeset::breaks::Lang;
use syncpdf_typeset::fit::{FitOptions, Inline, Obstacles, ParagraphSpec, Typeset};
use syncpdf_typeset::shaper::{FontMetrics, ShapedGlyph, Shaper, StyleSpec};

/// 用真实字体（syncpdf-font）实现的塑形适配器。
struct StoreShaper<'a> {
    store: &'a FontStore,
    font: FontId,
}

impl Shaper for StoreShaper<'_> {
    fn shape(&self, _font: u32, text: &str, size: f32, rtl: bool) -> Vec<ShapedGlyph> {
        let Some(f) = self.store.get(self.font) else {
            return Vec::new();
        };
        syncpdf_font::shape(f, text, size, rtl, &[])
            .into_iter()
            .map(|g| ShapedGlyph {
                gid: g.gid,
                cluster: g.cluster,
                x_advance: g.x_advance,
                x_offset: g.x_offset,
                y_offset: g.y_offset,
            })
            .collect()
    }

    fn metrics(&self, _font: u32) -> FontMetrics {
        match self.store.get(self.font) {
            Some(f) => {
                let m = syncpdf_font::metrics(f);
                FontMetrics {
                    ascent: m.ascent,
                    descent: -m.descent,
                }
            }
            None => FontMetrics {
                ascent: 0.8,
                descent: 0.2,
            },
        }
    }

    fn font_for(&self, _style: &StyleSpec) -> u32 {
        self.font.0
    }
}

/// 载入内置字体包，挑一个覆盖中文的字体。
fn cjk_store() -> Option<(FontStore, FontId)> {
    let dir = syncpdf_core::fixtures::fonts_dir()?;
    let store = FontStore::load_builtin(&dir).ok()?;
    let q = syncpdf_font::FontQuery {
        script: syncpdf_font::loader::Script::HanSC,
        ..Default::default()
    };
    let id = store.find(&q).or_else(|| store.ids().first().copied())?;
    Some((store, id))
}

/// pdfium 可用性探测。
fn pdfium_available() -> bool {
    PdfiumWorker::spawn().is_ok()
}

/// 从一个 Text item 的字形里拼出文本（按 ordinal 顺序）。
fn item_text(glyphs: &[syncpdf_core::ir::Glyph]) -> String {
    glyphs
        .iter()
        .flat_map(|g| g.unicode.iter().copied())
        .collect()
}

/// 取第一个非空 Text item（一个文本区域）。
fn first_text_region(ir: &syncpdf_core::ir::PageIR) -> Option<(Rect, Vec<syncpdf_core::GlyphId>)> {
    for it in &ir.items {
        if let DisplayItem::Text { glyphs } = it {
            if glyphs.is_empty() {
                continue;
            }
            let mut bbox = glyphs[0].bbox;
            for g in glyphs.iter().skip(1) {
                bbox = bbox.union(&g.bbox);
            }
            return Some((bbox, glyphs.iter().map(|g| g.id).collect()));
        }
    }
    None
}

/// 用 typeset 排一段文本到给定区域，返回 `TypesetParagraph`。
fn typeset_into(
    store: &FontStore,
    fid: FontId,
    text: &str,
    region: Rect,
    size: f32,
) -> Option<TypesetParagraph> {
    let shaper = StoreShaper { store, font: fid };
    let ts = Typeset::new(
        &shaper,
        FitOptions {
            min_scale: 0.6,
            scale_step: 0.05,
            line_height_steps: vec![1.2, 1.15, 1.1, 1.05, 1.0],
            allow_overflow_lines: 2,
        },
    );
    // 用字体度量换算行高（1.2 倍）。
    let m = syncpdf_font::metrics(store.get(fid)?);
    let line_height = 1.2 * (m.ascent - m.descent);
    let spec = ParagraphSpec {
        bbox: region,
        font_size: size,
        line_height,
        align: syncpdf_core::ir::Align::Left,
        first_indent: 0.0,
        is_rtl: false,
        color: Color::BLACK,
        styles: vec![(StyleId(1), StyleSpec::default())],
        lang: Lang::Zh,
    };
    let inlines = [Inline::Text {
        text: text.to_string(),
        style: StyleId(1),
    }];
    let res = ts.layout(
        "P01-001".parse().unwrap(),
        &spec,
        &inlines,
        &Obstacles::default(),
    );
    Some(res.paragraph)
}

/// 提取输出文档某页的全部文本（pdfium）。
fn page_text(worker: &PdfiumWorker, doc: syncpdf_pdf::pdfium::DocId, page: u32) -> String {
    worker
        .page_text_objects(doc, page)
        .map(|objs| {
            objs.iter()
                .flat_map(|o| o.chars.iter())
                .filter_map(|c| c.unicode.as_deref())
                .collect::<String>()
        })
        .unwrap_or_default()
}

/// 页 /Annots 数量。
fn annot_count(doc: &lopdf::Document, page: u32) -> usize {
    let pages = doc.get_pages();
    let Some(&pid) = pages.get(&page) else {
        return 0;
    };
    let Ok(pd) = doc.get_object(pid).and_then(lopdf::Object::as_dict) else {
        return 0;
    };
    let Ok(a) = pd.get(b"Annots") else {
        return 0;
    };
    let arr = match a {
        lopdf::Object::Array(a) => a.clone(),
        lopdf::Object::Reference(r) => doc
            .get_object(*r)
            .ok()
            .and_then(|o| o.as_array().ok())
            .cloned()
            .unwrap_or_default(),
        _ => Vec::new(),
    };
    arr.len()
}

const TRANSLATION: &str = "这是一段用于测试回写的译文文本。";

#[test]
fn writeback_ci_test_and_up_vns() {
    if !pdfium_available() {
        eprintln!("SKIP: pdfium library unavailable");
        return;
    }
    let Some((store, fid)) = cjk_store() else {
        eprintln!("SKIP: font package missing");
        return;
    };
    let worker = PdfiumWorker::spawn().unwrap();

    for name in ["ci-test.pdf", "up-vns.pdf"] {
        let path = require_fixture!(name);
        run_one(&worker, &store, fid, &path, name);
    }
}

/// 对单个夹具跑完整回写链路。
fn run_one(worker: &PdfiumWorker, store: &FontStore, fid: FontId, path: &Path, name: &str) {
    let mut lo = lopdf::Document::load(path).expect("load in");
    let doc_in = lopdf::Document::load(path).expect("load in2");
    let docid = worker.open(path).expect("pdfium open");
    let page_count_in = lo.get_pages().len() as u32;
    let annots_in = annot_count(&doc_in, 1);
    let text_in = page_text(worker, docid, 0);
    eprintln!(
        "{name}: 输入首页文本前 80 字符 = {}",
        text_in.chars().take(80).collect::<String>()
    );

    let bound = bind_page(worker, docid, &lo, 1).expect("bind");
    eprintln!(
        "{name}: bind stats={:?} items={}",
        bound.stats,
        bound.ir.items.len()
    );

    // 若该页没有任何文本（例如 ci-test），退化为只写译文。
    let mut ps = PatchSet::new();
    let mut region = Rect::new(72.0, 600.0, 400.0, 700.0);
    // 首个 Text 区域内的文本，用于挑选一个"删除后应消失"的词。
    let mut region_word: Option<String> = None;
    if let Some((bbox, ids)) = first_text_region(&bound.ir) {
        if bbox.width() > 1.0 && bbox.height() > 1.0 {
            region = bbox;
        }
        // 该区域内出现、且在全页其它 Text 区域中不再出现的英文词（避免误判）。
        if let Some(DisplayItem::Text { glyphs }) = bound
            .ir
            .items
            .iter()
            .find(|it| matches!(it, DisplayItem::Text { glyphs } if !glyphs.is_empty()))
        {
            let inside: Vec<String> = item_text(glyphs)
                .split(|c: char| !c.is_ascii_alphabetic())
                .filter(|w| w.len() >= 4)
                .map(str::to_string)
                .collect();
            let rest: String = bound
                .ir
                .items
                .iter()
                .skip_while(|it| !matches!(it, DisplayItem::Text { glyphs } if !glyphs.is_empty()))
                .skip(1)
                .filter_map(|it| match it {
                    DisplayItem::Text { glyphs } => Some(item_text(glyphs)),
                    _ => None,
                })
                .collect();
            region_word = inside.into_iter().find(|w| !rest.contains(w.as_str()));
        }
        ps.delete_glyphs(&bound, &ids).expect("trusted binding");
    }
    eprintln!("{name}: 首个区域独有词 = {region_word:?}");
    let patch_stats = ps.apply(&mut lo, 1).expect("patch apply");
    eprintln!("{name}: patch stats={patch_stats:?}");

    // 排版译文并写入。
    let para =
        typeset_into(store, fid, TRANSLATION, region, 10.0).expect("typeset paragraph produced");
    let mut w = Writer::new(store);
    w.write_paragraphs(&mut lo, 1, &[para], 792.0)
        .expect("write");
    let font_stats = w.finalize(&mut lo).expect("finalize");
    eprintln!("{name}: font stats={font_stats:?}");

    let tmp = tempfile::tempdir().unwrap();
    let out = tmp.path().join(format!("{name}.out.pdf"));
    save(&mut lo, &out).expect("save");

    // ---- 断言 ----
    let report = self_check(&out, &[1]).expect("self_check runs");
    eprintln!(
        "{name}: self_check ok={} qpdf={} problems={:?}",
        report.ok, report.qpdf_checked, report.problems
    );
    assert_eq!(report.pages, page_count_in, "页数必须不变");
    assert!(report.ok, "self_check 应全通过：{:?}", report.problems);

    let doc_out = lopdf::Document::load(&out).expect("reopen");
    assert_eq!(
        annot_count(&doc_out, 1),
        annots_in,
        "第 1 页 /Annots 数量应一致"
    );

    let out_docid = worker.open(&out).expect("pdfium reopen out");
    let text = page_text(worker, out_docid, 0);
    assert!(
        text.contains("测试回写"),
        "{name}: 输出文本应含译文片段，实际前 200 字符：{}",
        text.chars().take(200).collect::<String>()
    );
    if let Some(w) = region_word {
        assert!(
            !text.contains(&w),
            "{name}: 删除后输出文本不应再含首个区域独有词 {w:?}，实际前 300 字符：{}",
            text.chars().take(300).collect::<String>()
        );
    }
}

#[test]
fn writeback_up_vns_prebound_all_pages_delete_and_placeholder() {
    writeback_up_vns_all_pages(false);
}

#[test]
fn writeback_up_vns_rebinding_mutated_document_rejects_page_three() {
    writeback_up_vns_all_pages(true);
}

// The mutated-document case deliberately mixes original PDFium evidence with
// rewritten lopdf streams. Rejection is not a claim that original up-vns is unsafe.
fn writeback_up_vns_all_pages(rebind_mutated: bool) {
    if !pdfium_available() {
        eprintln!("SKIP: pdfium library unavailable");
        return;
    }
    let Some((store, fid)) = cjk_store() else {
        eprintln!("SKIP: font package missing");
        return;
    };
    let path = require_fixture!("up-vns.pdf");
    let worker = PdfiumWorker::spawn().unwrap();
    let mut lo = lopdf::Document::load(&path).unwrap();
    let page_count = lo.get_pages().len() as u32;
    let docid = worker.open(&path).unwrap();

    let original = lo.clone();
    let bounds: Vec<_> = (1..=page_count)
        .map(|page| {
            let b = bind_page(&worker, docid, &original, page).unwrap();
            b.check_replacement().unwrap();
            b
        })
        .collect();
    if rebind_mutated {
        // Deliberately create mismatched versions, independently of PatchSet:
        // PDFium keeps the original while lopdf loses two page-three shows.
        let page_id = lo.get_pages()[&3];
        let mut remaining = 2;
        for sid in lo.get_page_contents(page_id) {
            let stream = lo.get_object_mut(sid).unwrap().as_stream_mut().unwrap();
            let bytes = stream
                .decompressed_content()
                .unwrap_or_else(|_| stream.content.clone());
            let mut content = lopdf::content::Content::decode(&bytes).unwrap();
            content.operations.retain(|op| {
                if remaining > 0 && matches!(op.operator.as_str(), "Tj" | "TJ" | "'" | "\"") {
                    remaining -= 1;
                    false
                } else {
                    true
                }
            });
            stream.set_plain_content(content.encode().unwrap());
            if remaining == 0 {
                break;
            }
        }
        assert_eq!(remaining, 0);
    }
    let started = Instant::now();
    let mut total_glyphs = 0usize;
    for page in 1..=page_count {
        let bound = if rebind_mutated {
            bind_page(&worker, docid, &lo, page).expect("bind")
        } else {
            bounds[(page - 1) as usize].clone()
        };
        if rebind_mutated && page == 3 {
            use syncpdf_pdf::bind::ReplacementError;
            use syncpdf_pdf::patch::PatchError;
            assert_eq!(bounds[2].stats.text_ops, 512);
            assert_eq!(bound.stats.text_objects, 512);
            assert_eq!(bound.stats.text_ops, 510);
            let before = format!("{:?}", lo.objects);
            let mut ps = PatchSet::new();
            let ids: Vec<_> = bound.ir.glyphs().map(|g| g.id).collect();
            assert!(
                matches!(ps.delete_glyphs(&bound, &ids), Err(PatchError::UnsafeBinding(ReplacementError::Statistics(s))) if s == bound.stats)
            );
            assert!(ps.is_empty());
            ps.apply(&mut lo, page).unwrap();
            assert_eq!(format!("{:?}", lo.objects), before);
            worker.close(docid);
            return;
        }
        let mut ps = PatchSet::new();
        let mut regions: Vec<Rect> = Vec::new();
        for it in &bound.ir.items {
            if let DisplayItem::Text { glyphs } = it {
                if glyphs.is_empty() {
                    continue;
                }
                let mut bbox = glyphs[0].bbox;
                for g in glyphs.iter().skip(1) {
                    bbox = bbox.union(&g.bbox);
                }
                // 只对够大的区域写占位译文。
                if bbox.width() > 40.0 && bbox.height() > 5.0 {
                    regions.push(bbox);
                }
                total_glyphs += glyphs.len();
                let ids: Vec<_> = glyphs.iter().map(|g| g.id).collect();
                ps.delete_glyphs(&bound, &ids).expect("trusted binding");
            }
        }
        // Only the prebound success path promises cross-page isolation. The
        // rebinding case intentionally creates contaminated evidence to test rejection.
        let next_page_content = if !rebind_mutated && page < page_count {
            Some(lo.get_page_content(lo.get_pages()[&(page + 1)]))
        } else {
            None
        };
        ps.apply(&mut lo, page).expect("patch apply");
        if let Some(bytes) = next_page_content {
            let next_id = lo.get_pages()[&(page + 1)];
            let after = lo.get_page_content(next_id);
            assert!(after == bytes,
                "deleting page {page} changed page {} content; target streams={:?}; next streams={:?}; before prefix={:?}; after prefix={:?}",
                page + 1, lo.get_page_contents(bound.page_id), lo.get_page_contents(next_id),
                String::from_utf8_lossy(&bytes[..bytes.len().min(100)]),
                String::from_utf8_lossy(&after[..after.len().min(100)]));
        }

        // Reopen the deletion result before adding placeholders: isolation must
        // not turn this into a no-op that merely passes the next-page assertion.
        if !rebind_mutated {
            let check_dir = tempfile::tempdir().unwrap();
            let deleted_path = check_dir.path().join("deleted.pdf");
            lo.save(&deleted_path).unwrap();
            let deleted_doc = worker.open(&deleted_path).unwrap();
            assert!(
                page_text(&worker, deleted_doc, page - 1).trim().is_empty(),
                "page {page} still contains source text after deletion"
            );
            worker.close(deleted_doc);
        }

        // 每区域写一行占位译文。
        let mut paras: Vec<TypesetParagraph> = Vec::new();
        for (i, r) in regions.iter().take(3).enumerate() {
            let text = format!("第{i}段占位译文，用于回写耗时测试。");
            if let Some(p) = typeset_into(&store, fid, &text, *r, 8.0) {
                paras.push(p);
            }
        }
        if !paras.is_empty() {
            let mut w = Writer::new(&store);
            w.write_paragraphs(&mut lo, page, &paras, 792.0).unwrap();
            w.finalize(&mut lo).unwrap();
        }
    }
    let elapsed = started.elapsed();
    eprintln!(
        "up-vns 12 页：删除 + 占位译文耗时 {:.2}s（字形 {total_glyphs}）",
        elapsed.as_secs_f64()
    );

    let tmp = tempfile::tempdir().unwrap();
    let out = tmp.path().join("all.pdf");
    save(&mut lo, &out).unwrap();
    let report = self_check(&out, &[]).unwrap();
    eprintln!(
        "all-pages self_check ok={} qpdf={} problems={:?}",
        report.ok, report.qpdf_checked, report.problems
    );
    assert_eq!(report.pages, page_count, "页数不变");
    assert!(report.ok, "全页回写后自校验应通过：{:?}", report.problems);
}

/// 验证 `LoadedFont` 能被商店取到（编译期用，避免未使用导入告警）。
#[test]
fn loaded_font_is_debug() {
    fn _assert(f: &LoadedFont) -> String {
        format!("{f:?}")
    }
    let _ = _assert;
}

/// 占位：确保 `PlacedGlyph` 字段名与 writer 契约一致。
#[test]
fn placed_glyph_fields_contract() {
    let g = PlacedGlyph {
        font: 0,
        gid: 1,
        text: "A".into(),
        x: 0.0,
        y: 0.0,
        size: 10.0,
        scale_x: 1.0,
        style: StyleId(1),
    };
    assert_eq!(g.gid, 1);
}
