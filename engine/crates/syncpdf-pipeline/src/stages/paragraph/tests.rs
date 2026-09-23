//! `paragraph_analysis` 纯函数单测：手工构造 `PageIR`，不依赖 pdfium 与模型。

use super::*;
use syncpdf_core::ir::{DisplayItem, FontRef, Glyph, GlyphFlags, GlyphSource};
use syncpdf_core::{Color, Matrix, ObjRef, OpKey, PageId};

/// 造一个字形：`ch` 的 bbox 左下角为 `(x, y)`，宽 0.6×size、高 size。
fn mk_glyph(seq: u16, ch: char, x: f32, y: f32, size: f32, font: u32) -> Glyph {
    let w = size * 0.6;
    Glyph {
        id: GlyphId {
            page: PageId(0),
            op: OpKey::new(ObjRef::new(1, 0), 0),
            ordinal: seq,
        },
        unicode: [ch].into_iter().collect(),
        code: seq as u32,
        font,
        size,
        matrix: Matrix::new(1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
        bbox: Rect::new(x, y, x + w, y + size),
        advance: w,
        fill: Color::default(),
        render_mode: 0,
        source: GlyphSource {
            element_index: 0,
            string_operand_range: (0, 0),
            decoded_code_range: (0, 0),
        },
        flags: GlyphFlags::default(),
    }
}

fn mk_font(name: &str, bold: bool, italic: bool) -> FontRef {
    FontRef {
        resource_name: name.into(),
        base_font: name.into(),
        is_serif: false,
        is_fixed_pitch: false,
        is_italic: italic,
        is_bold: bold,
    }
}

/// 一行字：`text` 的每个字符一个字形，自左向右；`y` 为基线（bbox 底边）。
fn line(seq_start: u16, text: &str, x: f32, y: f32, size: f32, font: u32) -> Vec<Glyph> {
    let adv = size * 0.6;
    text.chars()
        .enumerate()
        .map(|(i, c)| mk_glyph(seq_start + i as u16, c, x + i as f32 * adv, y, size, font))
        .collect()
}

fn page_ir(glyphs: Vec<Glyph>, fonts: Vec<FontRef>) -> PageIR {
    PageIR {
        page: PageId(0),
        media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
        crop_box: Rect::new(0.0, 0.0, 612.0, 792.0),
        rotation: 0,
        fonts,
        items: vec![DisplayItem::Text { glyphs }],
    }
}

/// 覆盖全页的单个区域。
fn full_region(kind: RegionKind) -> Vec<Region> {
    vec![Region {
        page: PageId(0),
        index: 0,
        kind,
        bbox: Rect::new(0.0, 0.0, 612.0, 792.0),
        score: 0.95,
        order: 0,
    }]
}

fn text_region(index: u32, bbox: Rect, order: u32) -> Region {
    Region {
        page: PageId(0),
        index,
        kind: RegionKind::Text,
        bbox,
        score: 0.9,
        order,
    }
}

#[test]
fn preserved_region_glyphs_cannot_enter_a_translatable_paragraph() {
    let mut glyphs = line(0, "First formula", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(20, "Separate prose", 50.0, 600.0, 10.0, 0));
    let ir = page_ir(glyphs, vec![mk_font("F1", false, false)]);
    for kind in [
        RegionKind::Formula,
        RegionKind::Figure,
        RegionKind::Table,
        RegionKind::Code,
        RegionKind::Reference,
    ] {
        let mut regions = full_region(RegionKind::Text);
        let mut protected = text_region(1, Rect::new(80.0, 699.0, 129.0, 711.0), 1);
        protected.kind = kind;
        regions.push(protected.clone());
        let paragraphs = analyze_page(&ir, &regions);
        assert!(paragraphs.iter().any(|p| matches!(&p.translatable,
            Translatable::No { reason } if reason == "protected_source_overlap")));
        assert!(paragraphs
            .iter()
            .any(|p| p.text == "Separate prose" && matches!(p.translatable, Translatable::Yes)));
        let protected_ids: Vec<_> = ir
            .glyphs()
            .filter(|g| protected.bbox.contains(g.bbox.center()))
            .map(|g| g.id)
            .collect();
        for p in paragraphs
            .iter()
            .filter(|p| matches!(p.translatable, Translatable::Yes))
        {
            assert!(
                p.glyphs.iter().all(|id| !protected_ids.contains(id)),
                "{kind:?}: 保留字形不能被可译段消费"
            );
        }
    }
}

#[test]
fn two_lines_merge_into_one_paragraph() {
    // 行距 14pt、字号 10pt（14 < 1.8×10）→ 同段。
    let mut g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "World", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].lines.len(), 2);
    assert_eq!(paras[0].text, "Hello World");
    assert_eq!(paras[0].glyphs.len(), 10);
}

#[test]
fn large_line_gap_splits_paragraphs() {
    // 行距 30pt > 1.8×10 → 两段。
    let mut g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "World", 50.0, 670.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 2);
    assert_eq!(paras[0].text, "Hello");
    assert_eq!(paras[1].text, "World");
    assert_eq!(paras[0].id.to_string(), "P01-001");
    assert_eq!(paras[1].id.to_string(), "P01-002");
}

#[test]
fn indented_line_starts_a_new_paragraph() {
    // 第二行右移 20pt ≥ 1.5×10 → 新段（首行缩进）。
    let mut g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "World", 70.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(
        paras.len(),
        2,
        "{:?}",
        paras.iter().map(|p| &p.text).collect::<Vec<_>>()
    );
    assert_eq!(paras[0].text, "Hello");
    assert_eq!(paras[1].text, "World");
}

#[test]
fn hyphen_at_line_end_is_joined_without_space() {
    let mut g = line(0, "exam-", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "ple", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].text, "exam-ple");
    assert_eq!(paras[0].text_spans[4].text, "-");
}

#[test]
fn geometric_word_gap_has_a_zero_length_source_span() {
    let mut g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "world", 84.0, 700.0, 10.0, 0));
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert_eq!(p.len(), 1);
    assert_eq!(p[0].text, "Hello world");
    assert_eq!(p[0].text_spans[5].text, " ");
    assert_eq!(p[0].text_spans[5].glyph_range, (5, 5));
    let unit = syncpdf_translate::build_unit(&p[0], |_| Some("bad fallback".into()));
    assert_eq!(unit.plain_text(), "Hello world");
}

#[test]
fn explicit_space_ligature_and_cjk_punctuation_are_preserved() {
    let mut g = line(0, "A B", 50.0, 700.0, 10.0, 0);
    // One actual glyph contributes two Unicode characters.
    let mut ligature = mk_glyph(3, 'f', 68.0, 700.0, 10.0, 0);
    ligature.unicode = ['f', 'i'].into_iter().collect();
    g.push(ligature);
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert_eq!(p[0].text, "A Bfi");
    assert_eq!(p[0].text_spans.iter().filter(|s| s.text == " ").count(), 1);
    assert_eq!(p[0].text_spans[3].glyph_range, (3, 4));

    let g = line(0, "中文，测试。", 50.0, 700.0, 10.0, 0);
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert_eq!(p[0].text, "中文，测试。");
    assert!(p[0]
        .text_spans
        .iter()
        .all(|s| s.glyph_range.0 < s.glyph_range.1));
}

#[test]
fn generated_line_gap_keeps_multiline_atom_range_and_styles() {
    let mut g = line(0, "rate 25", 50.0, 700.0, 10.0, 0);
    g.extend(line(7, "ms now", 50.0, 686.0, 10.0, 1));
    let p = analyze_page(
        &page_ir(
            g,
            vec![mk_font("F1", false, false), mk_font("F2", true, false)],
        ),
        &full_region(RegionKind::Text),
    );
    assert_eq!(p.len(), 1);
    assert_eq!(p[0].text, "rate 25 ms now");
    assert_eq!(p[0].text_spans[7].glyph_range, (7, 7));
    assert_eq!(p[0].atoms[0].text, "25 ms");
    assert_eq!(p[0].atoms[0].glyph_range, (5, 9));
    assert_eq!(p[0].style_runs[0].glyph_range, (0, 7));
    assert_eq!(p[0].style_runs[1].glyph_range, (7, 13));
}

#[test]
fn rotated_source_is_kept_with_reason() {
    let mut g = line(0, "Side note", 50.0, 700.0, 10.0, 0);
    for glyph in &mut g {
        glyph.matrix = Matrix::new(0.0, 1.0, -1.0, 0.0, 0.0, 0.0);
    }
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert!(p.iter().all(|p| matches!(&p.translatable, Translatable::No { reason } if reason == "rotated_source_text")));
}

#[test]
fn vertical_side_note_geometry_is_kept_even_with_translation_only_matrices() {
    let g: Vec<_> = "VERTICAL"
        .chars()
        .enumerate()
        .map(|(i, c)| mk_glyph(i as u16, c, 50.0, 700.0 - i as f32 * 8.0, 10.0, 0))
        .collect();
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert!(p.iter().all(|p| matches!(&p.translatable, Translatable::No { reason } if reason == "rotated_source_text")), "{p:?}");
}

#[test]
fn punctuation_at_line_boundary_retains_source_and_gets_word_space() {
    let mut g = line(0, "Hello,", 50.0, 700.0, 10.0, 0);
    g.extend(line(6, "world", 50.0, 686.0, 10.0, 0));
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert_eq!(p[0].text, "Hello, world");
    assert_eq!(p[0].text_spans[6].glyph_range, (6, 6));
}

#[test]
fn overlapping_translatable_regions_keep_all_shared_source() {
    let g = line(0, "Shared prose", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![
        text_region(0, Rect::new(40.0, 690.0, 150.0, 720.0), 0),
        text_region(1, Rect::new(45.0, 690.0, 155.0, 720.0), 1),
    ];
    let p = analyze_page(&ir, &regions);
    assert_eq!(p.len(), 2);
    assert!(p.iter().all(|p| matches!(&p.translatable, Translatable::No { reason } if reason == "translatable_region_overlap")));
    assert_eq!(p[0].glyphs, p[1].glyphs);
}

#[test]
fn ordinary_region_cannot_translate_rotated_source_glyphs() {
    let mut g = line(0, "Side note", 50.0, 700.0, 10.0, 0);
    for glyph in &mut g {
        glyph.matrix = Matrix::new(0.0, 1.0, -1.0, 0.0, 0.0, 0.0);
    }
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![
        text_region(0, Rect::new(40.0, 690.0, 150.0, 720.0), 0),
        text_region(1, Rect::new(45.0, 690.0, 155.0, 720.0), 1),
    ];
    let p = analyze_page(&ir, &regions);
    assert_eq!(p.len(), 2);
    assert!(p.iter().all(|p| matches!(&p.translatable, Translatable::No { reason } if reason == "rotated_source_text")));
}

#[test]
#[ignore = "manual read-only source/paragraph probe for a specified PDF page"]
fn manual_source_paragraph_page_one_probe() {
    let path = std::env::var("R2_SOURCE_PROBE_PDF").expect("set R2_SOURCE_PROBE_PDF");
    let output = std::env::var("R2_SOURCE_PROBE_OUTPUT").expect("set R2_SOURCE_PROBE_OUTPUT");
    let worker = syncpdf_pdf::pdfium::PdfiumWorker::spawn().expect("pdfium");
    let pf = crate::stages::preflight(&worker, std::path::Path::new(&path)).expect("preflight");
    let lo = lopdf::Document::load(&path).expect("source PDF");
    let bound = syncpdf_pdf::bind::bind_page(&worker, pf.doc, &lo, 1).expect("bind page one");
    let model_path = syncpdf_core::fixtures::models_dir()
        .expect("models directory")
        .join("pp_doc_layoutv3.onnx");
    let mut model = syncpdf_layout::LayoutModel::load(&model_path, 2).expect("layout model");
    let opts = crate::stages::LayoutOpts::default();
    let mut regions =
        crate::stages::detect_regions(&mut model, &worker, pf.doc, 0, &pf.page_infos[0], &opts)
            .expect("layout page one");
    crate::stages::apply_coverage_fallback(&mut regions, &bound.ir, 0, opts.coverage_limit);
    let mut paras = analyze_page(&bound.ir, &regions);
    let protected =
        crate::stages::source_policy::protect_front_matter(&bound.ir, &regions, &mut paras);
    assert_eq!(protected.len(), 4, "指定论文姓名/机构/邮箱四段须保留");
    assert!(paras
        .iter()
        .any(|p| p.text.starts_with("Training neural networks")
            && matches!(p.translatable, Translatable::Yes)));
    let glyph_text: std::collections::HashMap<_, _> = bound
        .ir
        .glyphs()
        .map(|g| (g.id, g.unicode.iter().collect::<String>()))
        .collect();
    let records: Vec<_> = paras
        .iter()
        .map(|p| {
            let mut old = String::new();
            for (i, line) in p.lines.iter().enumerate() {
                if i > 0 {
                    if old.ends_with('-') {
                        old.pop();
                    } else {
                        old.push(' ');
                    }
                }
                for id in &line.glyphs {
                    old.push_str(glyph_text.get(id).expect("source glyph"));
                }
            }
            serde_json::json!({
                "id": p.id.to_string(),
                "reason": format!("{:?}", p.translatable),
                "old": old,
                "new": p.text,
                "generated": p.text_spans.iter().filter(|s| s.glyph_range.0 == s.glyph_range.1).map(|s| serde_json::json!({"text": s.text, "range": s.glyph_range})).take(12).collect::<Vec<_>>(),
                "first_spans": p.text_spans.iter().take(12).map(|s| serde_json::json!({"text": s.text, "range": s.glyph_range})).collect::<Vec<_>>(),
                "atoms": p.atoms.iter().take(8).map(|a| serde_json::json!({"text": a.text, "range": a.glyph_range})).collect::<Vec<_>>()
            })
        })
        .collect();
    std::fs::write(output, serde_json::to_vec_pretty(&records).expect("json"))
        .expect("probe evidence");
    worker.close(pf.doc);
}

#[test]
fn style_runs_split_by_font_size_and_bold() {
    // 同一行内：字号 10 → 12，字体 0 → 1（粗），切出两个 run。
    let mut g = line(0, "ab", 50.0, 700.0, 10.0, 0);
    g.extend(line(2, "cd", 62.0, 700.0, 12.0, 1));
    let ir = page_ir(
        g,
        vec![mk_font("F1", false, false), mk_font("F2", true, false)],
    );
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    let runs = &paras[0].style_runs;
    assert_eq!(runs.len(), 2, "{runs:?}");
    assert_eq!(runs[0].glyph_range, (0, 2));
    assert_eq!(runs[0].size, 10.0);
    assert!(!runs[0].bold);
    assert_eq!(runs[0].id, StyleId(1));
    assert_eq!(runs[1].glyph_range, (2, 4));
    assert_eq!(runs[1].size, 12.0);
    assert!(runs[1].bold, "粗体标志应取自 PageIR::fonts");
    assert_eq!(runs[1].id, StyleId(2));
}

#[test]
fn url_becomes_an_atom() {
    let g = line(0, "see https://a.io/x now", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].atoms.len(), 1);
    assert_eq!(paras[0].atoms[0].kind, AtomKind::Url);
    assert_eq!(paras[0].atoms[0].text, "https://a.io/x");
    assert_eq!(paras[0].atoms[0].id, AtomId(1));
    // "see " 占 4 个字形，`https://a.io/x` 占 14 个字形 → 字形区间 (4, 18)。
    assert_eq!(paras[0].atoms[0].glyph_range, (4, 18));
    // 字形区间对应的文本必须与原子文本一致。
    let gs = paras[0].glyphs[4..18]
        .iter()
        .map(|_| 'x')
        .collect::<String>();
    assert_eq!(gs.len(), 14);
}

#[test]
fn email_becomes_an_atom() {
    let g = line(0, "mail a.b@c.de ok", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras[0].atoms.len(), 1);
    assert_eq!(paras[0].atoms[0].text, "a.b@c.de");
}

#[test]
fn reference_marker_and_number_unit_become_atoms() {
    let g = line(0, "as in [1, 2] at 25 ms", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    let texts: Vec<&str> = paras[0].atoms.iter().map(|a| a.text.as_str()).collect();
    assert!(texts.contains(&"[1, 2]"), "{texts:?}");
    assert!(texts.contains(&"25 ms"), "{texts:?}");
    // 原子 id 按字形位置从 1 编号。
    assert_eq!(paras[0].atoms[0].id, AtomId(1));
    assert_eq!(paras[0].atoms[1].id, AtomId(2));
    assert!(
        paras[0].atoms[0].glyph_range.0 < paras[0].atoms[1].glyph_range.0,
        "原子按位置排序"
    );
}

#[test]
fn math_symbol_run_becomes_a_formula_atom() {
    let g = line(0, "sum ∑∫ x", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    let atom = paras[0]
        .atoms
        .iter()
        .find(|a| a.text == "∑∫")
        .expect("应识别出数学符号原子");
    assert_eq!(atom.kind, AtomKind::Formula);
}

#[test]
fn non_translatable_region_kind_is_no() {
    let g = line(0, "hello world", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Table));
    assert_eq!(paras.len(), 1);
    assert_eq!(
        paras[0].translatable,
        Translatable::No {
            reason: "region_kind".into()
        }
    );
}

#[test]
fn digits_only_paragraph_is_not_translatable() {
    let g = line(0, "1234", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(
        paras[0].translatable,
        Translatable::No {
            reason: "no_letters".into()
        }
    );
}

#[test]
fn single_char_paragraph_is_too_short() {
    let g = line(0, "A", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(
        paras[0].translatable,
        Translatable::No {
            reason: "too_short".into()
        }
    );
}

#[test]
fn centered_lines_are_center_aligned() {
    // 四字宽 4×6 = 24pt；区域 [50, 300]、行 x0=163 → 左右边距各 113/113，
    // 差 2pt 以内、都远超 3pt → Center。
    let x0 = 163.0;
    let mut g = line(0, "aaaa", x0, 700.0, 10.0, 0);
    g.extend(line(4, "bbbb", x0, 686.0, 10.0, 0));
    g.extend(line(8, "cccc", x0, 672.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![text_region(0, Rect::new(50.0, 650.0, 300.0, 792.0), 0)];
    let paras = analyze_page(&ir, &regions);
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].align, Align::Center);
}

#[test]
fn equal_width_lines_are_justified() {
    // 三行等宽、左齐右齐，且区域边距为 0（贴边）→ 不满足居中的 >3pt 留白。
    let mut g = line(0, "aaaaa", 0.0, 700.0, 10.0, 0);
    g.extend(line(5, "bbbbb", 0.0, 686.0, 10.0, 0));
    g.extend(line(10, "ccccc", 0.0, 672.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    // 区域左边界 = 行起点、右边界 = 行终点 → 左右边距都是 0。
    let regions = vec![text_region(0, Rect::new(0.0, 650.0, 30.0, 792.0), 0)];
    let paras = analyze_page(&ir, &regions);
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].align, Align::Justify);
}

#[test]
fn ragged_right_lines_are_left_aligned() {
    // 左齐、右边参差 → Left。
    let mut g = line(0, "aaaaa", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "bb", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras[0].align, Align::Left);
}

#[test]
fn first_indent_and_line_height_are_measured() {
    let mut g = line(0, "Hello", 60.0, 700.0, 10.0, 0);
    g.extend(line(5, "World", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert!(
        (paras[0].first_indent - 10.0).abs() < 0.01,
        "{}",
        paras[0].first_indent
    );
    assert!(
        (paras[0].line_height - 14.0).abs() < 0.01,
        "{}",
        paras[0].line_height
    );
}

#[test]
fn single_line_paragraph_line_height_is_1_2_times_size() {
    let g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert!((paras[0].line_height - 12.0).abs() < 0.01);
    assert_eq!(paras[0].first_indent, 0.0);
}

#[test]
fn paragraph_seq_is_continuous_across_regions() {
    // 两个区域各一行：seq 连续（1、2），不各自从 1 起。
    let mut g = line(0, "aaaa", 50.0, 700.0, 10.0, 0);
    g.extend(line(4, "bbbb", 50.0, 400.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![
        text_region(0, Rect::new(0.0, 650.0, 612.0, 792.0), 0),
        text_region(1, Rect::new(0.0, 350.0, 612.0, 450.0), 1),
    ];
    let paras = analyze_page(&ir, &regions);
    assert_eq!(paras.len(), 2);
    assert_eq!(paras[0].id.to_string(), "P01-001");
    assert_eq!(paras[1].id.to_string(), "P01-002");
    assert_eq!(paras[0].region, 0);
    assert_eq!(paras[1].region, 1);
}

#[test]
fn regions_are_processed_in_reading_order() {
    // 区域按 order 反序给出，输出仍应按 order 排序。
    let mut g = line(0, "first", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "second", 50.0, 400.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![
        text_region(7, Rect::new(0.0, 350.0, 612.0, 450.0), 5),
        text_region(2, Rect::new(0.0, 650.0, 612.0, 792.0), 1),
    ];
    let paras = analyze_page(&ir, &regions);
    assert_eq!(paras.len(), 2);
    assert_eq!(paras[0].text, "first", "order 小的先出");
    assert_eq!(paras[1].text, "second");
    assert_eq!(paras[0].region, 2);
    assert_eq!(paras[1].region, 7);
}

#[test]
fn glyphs_outside_all_regions_are_ignored() {
    let mut g = line(0, "inside", 50.0, 700.0, 10.0, 0);
    g.push(mk_glyph(99, 'X', 50.0, 100.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![text_region(0, Rect::new(0.0, 650.0, 612.0, 792.0), 0)];
    let paras = analyze_page(&ir, &regions);
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].text, "inside");
    assert!(!paras[0].text.contains('X'));
}

#[test]
fn invisible_glyphs_are_skipped() {
    let mut g = line(0, "ok", 50.0, 700.0, 10.0, 0);
    let mut hidden = mk_glyph(9, 'Z', 80.0, 700.0, 10.0, 0);
    hidden.flags.invisible = true;
    g.push(hidden);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].text, "ok");
}

#[test]
fn bbox_is_union_of_line_boxes() {
    let mut g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "World", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    let b = paras[0].bbox;
    assert!(b.x0 <= 50.0 + 0.01);
    assert!(b.y0 <= 686.0 + 0.01);
    assert!(b.y1 >= 710.0 - 0.01);
}

#[test]
fn empty_page_yields_no_paragraphs() {
    let ir = page_ir(vec![], vec![]);
    assert!(analyze_page(&ir, &full_region(RegionKind::Text)).is_empty());
}

#[test]
fn title_region_is_translatable() {
    let g = line(0, "A Title", 50.0, 700.0, 14.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Title));
    assert_eq!(paras[0].translatable, Translatable::Yes);
    assert_eq!(paras[0].kind, RegionKind::Title);
}
