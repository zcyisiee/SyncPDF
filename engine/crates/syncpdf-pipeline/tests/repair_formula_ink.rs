//! Opt-in real-paper check: loose char boxes must not be mistaken for formula ink.
//!
//! pdfium 的 loose char box 含字体上下沿，因此同一行的邻字、甚至上下行都会
//! 与公式的 loose 盒「相交」，把整段误判成 `protected_source_overlap`。真实
//! 墨迹（tight char box）才是碰撞证据。本测试在指定论文上绑定真实页，再用
//! 已缓存的版面区域复跑段落分析。
//!
//! 运行：
//! ```text
//! source ../../tmp/paper-iteration/env.sh
//! FORMULA_INK_PDF=<paper.pdf> \
//! FORMULA_INK_INVENTORY=<inventory dir with regions-N.json> \
//!   cargo test -p syncpdf-pipeline --test repair_formula_ink -- --ignored --nocapture
//! ```
//! `regions-N.json` 的 N 是 0 基页号（与 `PageIR.page.0` 一致）。

use std::path::{Path, PathBuf};

use syncpdf_core::ir::{AtomKind, PageIR, Region, Translatable};
use syncpdf_pdf::bind::bind_page;
use syncpdf_pdf::pdfium::PdfiumWorker;
use syncpdf_pipeline::stages;

fn worker() -> Option<PdfiumWorker> {
    if syncpdf_core::fixtures::pdfium_lib_dir().is_none() {
        eprintln!("SKIP: pdfium 动态库缺失");
        return None;
    }
    Some(PdfiumWorker::spawn().expect("pdfium worker"))
}

fn inventory_dir() -> PathBuf {
    PathBuf::from(std::env::var("FORMULA_INK_INVENTORY").expect("FORMULA_INK_INVENTORY"))
}

fn regions_of(dir: &Path, page: u32) -> Vec<Region> {
    serde_json::from_slice(
        &std::fs::read(dir.join(format!("regions-{page}.json"))).expect("regions json"),
    )
    .expect("regions decode")
}

fn paragraphs_for(worker: &PdfiumWorker, pdf: &str, page: u32) -> Vec<syncpdf_core::ir::Paragraph> {
    let lo = lopdf::Document::load(pdf).expect("lopdf");
    let doc = worker.open(Path::new(pdf)).expect("pdfium open");
    // `bind_page` 收 1 基页号；`PageIR::page` 是 0 基。
    let bound = bind_page(worker, doc, &lo, page + 1).expect("bind_page");
    bound.check_replacement().expect("replacement gate");
    let regions = regions_of(&inventory_dir(), page);
    let mut paras = stages::analyze_page(&bound.ir, &regions);
    stages::source_policy::protect_front_matter(&bound.ir, &regions, &mut paras);
    // 让段落绑定活到断言之后（PageIR 由 bound 持有）。
    drop(bound);
    paras
}

/// 三个实际被 loose 盒误判的段落：修复后必须有公式原子且可译。
#[test]
#[ignore = "opt-in real paper ink evidence; requires FORMULA_INK_PDF and FORMULA_INK_INVENTORY"]
fn loose_box_overlap_does_not_block_real_inline_formulas() {
    let Some(worker) = worker() else { return };
    let pdf = std::env::var("FORMULA_INK_PDF").expect("FORMULA_INK_PDF");
    // (0 基页, 段 id, 该段公式原子应有的源文本)
    let cases: &[(u32, &str, &[&str])] = &[
        (8, "P09-003", &["𝑙>𝐿/2)", "𝐻/2", "(𝑊/2", "𝑊/2)"]),
        (14, "P15-012", &[]),
        (19, "P20-011", &["/2"]),
    ];
    let mut checked = 0;
    for &(page, id, _) in cases {
        let Some(p) = paragraphs_for(&worker, &pdf, page)
            .into_iter()
            .find(|p| p.id.to_string() == id)
        else {
            panic!("{id} 未出现在第 {} 页", page + 1);
        };
        let formulas: Vec<String> = p
            .atoms
            .iter()
            .filter(|a| a.kind == AtomKind::Formula)
            .map(|a| a.text.clone())
            .collect();
        println!(
            "{id}: translatable={:?} formula_atoms={formulas:?}",
            p.translatable
        );
        assert!(
            matches!(p.translatable, Translatable::Yes),
            "{id} 仍被源冲突阻断：{:?}",
            p.translatable
        );
        assert!(
            !formulas.is_empty(),
            "{id} 应至少有一个公式原子：{formulas:?}"
        );
        assert!(
            p.atoms.iter().all(|a| a.source.is_some()),
            "{id} 公式原子必须带源绘制证据"
        );
        checked += 1;
    }
    assert_eq!(checked, cases.len());
}

/// 没有墨迹证据时必须继续保守：把本页墨迹全部拿掉后，同一个段必须重新被阻断。
///
/// 碰撞判定是「两侧都取墨迹」：公式侧 `𝑙>𝐿/2)` 的 loose 右缘 351.3896 与右侧
/// 逗号的 loose 左缘 351.38956 只差 0.00004pt，两边都缺墨迹时就回到修复前的
/// 行为（阻断整段）。这条证明新代码不是「无证据就默默跳过检查」。
#[test]
#[ignore = "opt-in real paper ink evidence; requires FORMULA_INK_PDF and FORMULA_INK_INVENTORY"]
fn removing_ink_evidence_restores_the_conservative_block() {
    let Some(worker) = worker() else { return };
    let pdf = std::env::var("FORMULA_INK_PDF").expect("FORMULA_INK_PDF");
    let lo = lopdf::Document::load(&pdf).expect("lopdf");
    let doc = worker.open(Path::new(&pdf)).expect("pdfium open");
    let bound = bind_page(&worker, doc, &lo, 9).expect("bind_page");
    let regions = regions_of(&inventory_dir(), 8);
    let mut stripped = bound.ir.clone();
    let mut stripped_glyphs = 0usize;
    for item in &mut stripped.items {
        if let syncpdf_core::ir::DisplayItem::Text { glyphs } = item {
            for g in glyphs {
                stripped_glyphs += 1;
                g.ink = None;
            }
        }
    }
    assert!(stripped_glyphs > 0, "应确实拿掉了本页墨迹");
    let paras = stages::analyze_page(&stripped, &regions);
    let p = paras
        .iter()
        .find(|p| p.id.to_string() == "P09-003")
        .expect("P09-003 未出现");
    println!(
        "stripped {stripped_glyphs} glyph inks; P09-003={:?}",
        p.translatable
    );
    assert!(
        matches!(&p.translatable, Translatable::No { reason } if reason == "protected_source_overlap"),
        "缺墨迹证据时必须回到修复前的保守阻断，实际 {:?}",
        p.translatable
    );
}

fn overlap_fixture() -> (PageIR, Vec<Region>) {
    use syncpdf_core::ir::{DisplayItem, FontRef, Glyph, GlyphFlags, GlyphSource};
    use syncpdf_core::{Color, GlyphId, Matrix, ObjRef, OpKey, PageId, Rect};
    let mk = |ord: u16, ch: char, x: f32, ink: Option<Rect>| Glyph {
        id: GlyphId {
            page: PageId(0),
            op: OpKey::new(ObjRef::new(1, 0), 0),
            ordinal: ord,
        },
        unicode: [ch].into_iter().collect(),
        code: ord as u32,
        font: 0,
        size: 10.0,
        matrix: Matrix::new(1.0, 0.0, 0.0, 1.0, x, 700.0),
        bbox: Rect::new(x, 700.0, x + 6.0, 710.0),
        ink,
        advance: 6.0,
        fill: Color::BLACK,
        render_mode: 0,
        source: GlyphSource {
            element_index: 0,
            string_operand_range: (0, 1),
            decoded_code_range: (ord as u32, ord as u32 + 1),
        },
        flags: GlyphFlags::default(),
    };
    let font = |name: &str| FontRef {
        resource_name: name.into(),
        base_font: name.into(),
        is_serif: false,
        is_fixed_pitch: false,
        is_italic: false,
        is_bold: false,
    };
    // 公式字形 x（真实墨迹在 x=86..92）与紧邻字形 y（真实墨迹与它相交）。
    let mut glyphs = vec![mk(0, 'v', 80.0, Some(Rect::new(80.0, 702.0, 86.0, 708.0)))];
    glyphs.push(mk(1, 'x', 86.0, Some(Rect::new(86.0, 702.0, 92.0, 708.0))));
    glyphs.push(mk(2, 'y', 91.0, Some(Rect::new(91.0, 702.0, 97.0, 708.0))));
    let ir = PageIR {
        page: PageId(0),
        media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
        crop_box: Rect::new(0.0, 0.0, 612.0, 792.0),
        rotation: 0,
        fonts: vec![font("F1")],
        items: vec![DisplayItem::Text { glyphs }],
    };
    let mut regions = vec![Region {
        page: PageId(0),
        index: 0,
        kind: syncpdf_core::ir::RegionKind::Text,
        bbox: Rect::new(0.0, 690.0, 612.0, 720.0),
        score: 0.9,
        order: 0,
    }];
    let mut formula = regions[0].clone();
    formula.index = 1;
    formula.kind = syncpdf_core::ir::RegionKind::Formula;
    formula.bbox = Rect::new(85.0, 701.0, 93.0, 709.0);
    regions.push(formula);
    (ir, regions)
}

#[test]
fn source_clip_cannot_erase_neighbor_ink_outside_formula_ink() {
    let (mut ir, regions) = overlap_fixture();
    let syncpdf_core::ir::DisplayItem::Text { glyphs } = &mut ir.items[0] else {
        unreachable!()
    };
    glyphs[1].ink = Some(syncpdf_core::Rect::new(86.5, 702., 90., 708.));
    let neighbor = glyphs[2].ink.unwrap();
    let paras = stages::analyze_page(&ir, &regions);
    let atom = paras
        .iter()
        .flat_map(|p| &p.atoms)
        .find(|a| a.source.is_some())
        .unwrap();
    assert!(
        atom.source.unwrap().bbox.x1 <= neighbor.x0,
        "the erase/replay clip, not just the collision box, must exclude neighboring ink"
    );
}

/// 真正与公式墨迹相交的邻字仍必须拒绝整段（不能靠 ink 放行真碰撞）。
#[test]
fn overlap_with_real_ink_still_blocks_the_paragraph() {
    let (ir, regions) = overlap_fixture();
    let paras = stages::analyze_page(&ir, &regions);
    let p = paras
        .iter()
        .find(|p| p.kind == syncpdf_core::ir::RegionKind::Text)
        .unwrap();
    println!("real-ink overlap: {:?}", p.translatable);
    assert!(
        matches!(&p.translatable, Translatable::No { reason } if reason == "protected_source_overlap"),
        "真实墨迹相交仍须保守阻断，实际 {:?}",
        p.translatable
    );
}
