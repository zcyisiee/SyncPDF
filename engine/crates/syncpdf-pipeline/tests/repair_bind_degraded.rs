//! O4 源绑定失败粒度下沉：操作级不可证明不再中止整份文档。
//!
//! 夹具全部在测试内合成（不复制论文）：
//! * 混合字体页：正常 Courier `(text) Tj` + Type0 空 ToUnicode `<00010002> Tj`
//!   → 后者是操作级降级（有 pdfium 对象、几何可取）。
//! * `/Contents` 数组重复引用同一流 → duplicate source operation → 页级结构
//!   错误（门禁仍拒绝，整页保留原文）。
//!
//! 断言：
//! * 操作级：check_replacement 通过、降级操作有几何记录、相交段落保留原文
//!   （reason `unmapped_source_glyph`）、不相交段落正常删除写回、降级操作
//!   字节在写回后逐字节保留。
//! * 页级：该页整体保留原文，其它页正常，source_analysis 不再中止。
//! * 安全：删除请求含降级操作字形时被拒绝。
use lopdf::{dictionary, Document, Object, Stream};
use syncpdf_core::ir::{Paragraph, RegionKind, Translatable};
use syncpdf_core::{GlyphId, ParagraphId};
use syncpdf_pdf::bind::{bind_page, BoundPage};
use syncpdf_pdf::pdfium::PdfiumWorker;
use syncpdf_pipeline::cancel::CancellationToken;
use syncpdf_pipeline::stages::paragraph::analyze_page;
use syncpdf_pipeline::stages::{delete_translated, source_analysis, PipelineError};

/// 两页文档。每页内容流由 `contents_for` 生成（0 基页号）。
///
/// 混合页（页 0）：两个 BT 块——Courier 正常文字 + Type0 空 ToUnicode 操作；
/// `degraded_over_normal` 控制降级操作叠在正常文字上方（true）或放在页面
/// 远离正常文字的角落（false，模拟图内刻度）。
fn document(pages: &[PageSpec]) -> Document {
    let mut doc = Document::with_version("1.7");
    let pages_id = doc.new_object_id();
    let courier = doc.add_object(dictionary! {
        "Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Courier",
        "Encoding" => "WinAnsiEncoding", "FirstChar" => 32, "LastChar" => 126,
        "Widths" => vec![Object::Integer(600); 95]
    });
    let type0 = type0_empty_mapping_font(&mut doc);
    let mut kids = Vec::new();
    for spec in pages {
        let content = spec.content();
        let stream = doc.add_object(Stream::new(dictionary! {}, content.into_bytes()));
        let contents = if matches!(spec, PageSpec::Structural) {
            // 页级结构错误：Contents 数组引用同一流两次 → 同一 OpKey 出现两遍
            // → duplicate source operation（门禁页级拒绝）。
            Object::Array(vec![Object::Reference(stream), Object::Reference(stream)])
        } else {
            Object::Reference(stream)
        };
        let page = doc.add_object(dictionary! {
            "Type" => "Page", "Parent" => pages_id,
            "MediaBox" => vec![0.into(), 0.into(), 612.into(), 792.into()],
            "Resources" => dictionary! {
                "Font" => dictionary! {"F1" => courier, "F2" => type0}
            },
            "Contents" => contents,
        });
        kids.push(Object::Reference(page));
    }
    doc.objects.insert(
        pages_id,
        Object::Dictionary(
            dictionary! {"Type" => "Pages", "Kids" => kids, "Count" => pages.len() as i64},
        ),
    );
    let root = doc.add_object(dictionary! {"Type" => "Catalog", "Pages" => pages_id});
    doc.trailer.set("Root", root);
    doc
}

/// Type0 字体：ToUnicode 把 <0001> 映射到空串、<0002> 映射到 'y'（不可证明）。
fn type0_empty_mapping_font(doc: &mut Document) -> lopdf::ObjectId {
    let cid = doc.add_object(dictionary! {
        "Type" => "Font", "Subtype" => "CIDFontType2", "BaseFont" => "FakeCJK",
        "CIDSystemInfo" => dictionary! {
            "Registry" => Object::string_literal("Adobe"),
            "Ordering" => Object::string_literal("Identity"),
            "Supplement" => 0
        },
        "CIDToGIDMap" => "Identity", "DW" => 1000
    });
    let cmap = b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /Adobe-Identity-UCS def /CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange 2 beginbfchar <0001> <> <0002> <0079> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end";
    let unicode = doc.add_object(Stream::new(dictionary! {}, cmap.to_vec()));
    doc.add_object(dictionary! {
        "Type" => "Font", "Subtype" => "Type0", "BaseFont" => "FakeCJK",
        "Encoding" => "Identity-H",
        "DescendantFonts" => vec![Object::Reference(cid)],
        "ToUnicode" => unicode
    })
}

/// 一页的内容流规格。
enum PageSpec {
    /// 正常页：单段 Courier 文字（起点 `(72, 700)`）。
    Normal,
    /// 混合页：`(72,700)` 的 Courier 正常文字 + Type0 降级操作。
    /// `overlay` = true 时降级操作与正常文字同位置（页坐标 72,700），
    /// false 时放在 (500, 60)（远离正常文字，模拟图内区域）。
    Mixed { overlay: bool },
    /// 页级结构错误：`/Contents` 数组重复引用同一内容流 → duplicate source
    /// operation（门禁页级拒绝）。
    Structural,
}

impl PageSpec {
    fn content(&self) -> String {
        match self {
            PageSpec::Normal => "BT /F1 12 Tf 72 700 Td (ABC) Tj ET".into(),
            PageSpec::Mixed { overlay } => {
                // 降级操作绘制两个 code：<0001>（ToUnicode 空）与 <0002>。
                let (x, y) = if *overlay { (72, 700) } else { (500, 60) };
                format!(
                    "BT /F1 12 Tf 72 700 Td (ABC) Tj ET BT /F2 12 Tf {x} {y} Td <00010002> Tj ET"
                )
            }
            // 内容本身正常；结构错误来自 `document()` 里的重复 Contents 引用。
            PageSpec::Structural => "BT /F1 12 Tf 72 700 Td (ABC) Tj ET".into(),
        }
    }
}

/// 找到某页所有段落里 bbox 与 `ink` 相交的第一个段落下标。
fn paragraph_overlapping(paragraphs: &[Paragraph], ink: syncpdf_core::Rect) -> Option<usize> {
    paragraphs.iter().position(|p| {
        p.bbox.x0 < ink.x1 && ink.x0 < p.bbox.x1 && p.bbox.y0 < ink.y1 && ink.y0 < p.bbox.y1
    })
}

/// 用 `analyze_page` 切段，再过不可证明墨迹接入点（与 run.rs 段落循环同序）。
fn paragraphs_of(bound: &BoundPage) -> Vec<Paragraph> {
    let bbox = bound.ir.crop_box;
    let mut paragraphs = analyze_page(
        &bound.ir,
        &[syncpdf_core::ir::Region {
            page: bound.ir.page,
            index: 0,
            kind: RegionKind::Text,
            bbox,
            score: 1.0,
            order: 0,
        }],
    );
    syncpdf_pipeline::stages::source_unproven::protect(
        &mut paragraphs,
        bound.reliability,
        bound.unproven_source_ops(),
    );
    paragraphs
}

// ---------------------------------------------------------------------------
// 操作级：check_replacement 通过 + 几何记录
// ---------------------------------------------------------------------------

/// 混合页（降级操作与正常文字重叠）：绑定统计降级 1，但门禁不再拒绝；
/// 降级操作有几何记录；source_analysis 全程成功。
#[test]
fn op_level_degraded_page_passes_gate_and_analysis() {
    let Some(worker) = worker() else { return };
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("mixed.pdf");
    document(&[PageSpec::Mixed { overlay: true }])
        .save(&path)
        .unwrap();
    let lo = Document::load(&path).unwrap();
    let doc = worker.open(&path).unwrap();

    let bound = bind_page(&worker, doc, &lo, 1).unwrap();
    assert_eq!(bound.stats.degraded, 1, "应恰有一个操作级降级");
    assert_eq!(bound.stats.unbound_glyphs, 1);
    // O4：操作级降级不再让门禁失败。
    bound.check_replacement().expect("操作级降级不应再拒绝整页");
    // 几何记录：与该操作关联、落在页面坐标内。
    let ink = bound.unproven_source_ops();
    assert_eq!(ink.len(), 1, "降级操作应有几何记录：{:?}", bound.issues);
    assert!(
        ink[0].ink.width() > 0.0 && ink[0].ink.height() > 0.0,
        "几何非退化"
    );
    let expected = expected_overlay_ink(&worker, doc);
    assert_eq!(ink[0].ink, expected, "记录的几何应来自 pdfium 对象边界");
    assert!(
        bound.ir.glyphs().any(|g| g.id.op == ink[0].op),
        "记录的操作应是该页内容流操作之一"
    );

    // source_analysis 不再中止。
    let mut progress = 0;
    let pages = source_analysis(
        &worker,
        doc,
        &lo,
        &[0],
        &CancellationToken::new(),
        |_, _| progress += 1,
    )
    .expect("操作级降级不应中止 source_analysis");
    assert_eq!(progress, 1);
    assert_eq!(pages.len(), 1);
    // overlay 布局下降级墨迹与正常文字重叠：Yes 段落被翻成 No{unmapped_source_glyph}。
    let paragraphs = paragraphs_of(&bound);
    // overlay 时降级字形与正常文字并成一段（ToUnicode 的 'y' 混入文本）。
    let merged = paragraphs
        .iter()
        .find(|p| {
            p.glyphs.iter().any(|id| id.op == ink[0].op)
                && ['A', 'B', 'C'].iter().all(|c| p.text.contains(*c))
        })
        .expect("overlay 时降级操作与正常文字应同段");
    assert!(
        matches!(&merged.translatable, Translatable::No { reason } if reason == "unmapped_source_glyph"),
        "含不可证明墨迹的可译段落应保留原文并上报原因，实际 {:?}",
        merged.translatable
    );
    worker.close(doc);
}

/// pdfium 对混合页里 Type0 对象的边界（直接对照记录值）。
fn expected_overlay_ink(
    worker: &PdfiumWorker,
    doc: syncpdf_pdf::pdfium::DocId,
) -> syncpdf_core::Rect {
    let objs = worker.page_text_objects(doc, 0).unwrap();
    let obj = objs
        .iter()
        .find(|o| o.chars.iter().any(|c| c.unicode.as_deref() == Some("y")))
        .expect("Type0 对象应存在");
    obj.object_bounds.expect("对象边界应可取").bbox
}

/// 混合页（降级操作远离正常文字）：正常段落照常可译、降级段落保留原文。
#[test]
fn op_level_ink_excludes_only_overlapping_paragraph() {
    let Some(worker) = worker() else { return };
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("apart.pdf");
    document(&[PageSpec::Mixed { overlay: false }])
        .save(&path)
        .unwrap();
    let lo = Document::load(&path).unwrap();
    let doc = worker.open(&path).unwrap();
    let bound = bind_page(&worker, doc, &lo, 1).unwrap();
    bound.check_replacement().expect("门禁应通过");

    let unproven = bound.unproven_source_ops();
    assert_eq!(unproven.len(), 1);
    let ink = unproven[0].ink;

    let paragraphs = paragraphs_of(&bound);
    // 正常文字段照常可译。
    let abc = paragraphs
        .iter()
        .find(|p| p.text.contains("ABC"))
        .expect("正常文字段");
    assert!(
        matches!(abc.translatable, Translatable::Yes),
        "远离墨迹的段落应正常可译"
    );
    // 含降级操作字形（或与其墨迹相交）的段落全部保留原文。
    // 注：单字形段可能已被 judge_translatable 判 too_short——排除事实不变，
    // overlay 场景（下个测试）验证 Yes 段落被翻成 unmapped_source_glyph。
    let hit: Vec<&Paragraph> = paragraphs
        .iter()
        .filter(|p| p.glyphs.iter().any(|id| id.op == unproven[0].op))
        .collect();
    assert!(!hit.is_empty(), "降级操作的字形应落在段落里");
    for p in &hit {
        assert!(
            !matches!(p.translatable, Translatable::Yes),
            "含不可证明墨迹/字形的段落不得送译，实际 {:?}",
            p.translatable
        );
    }
    let overlap_hit = paragraph_overlapping(&paragraphs, ink);
    assert!(overlap_hit.is_some(), "墨迹应命中含它字形的段落");
    worker.close(doc);
}

// ---------------------------------------------------------------------------
// 安全：降级操作的字形永不删除
// ---------------------------------------------------------------------------

/// 删除请求含降级操作字形 → PatchSet 拒绝；只删正常段成功且降级操作字节逐字节保留。
#[test]
fn op_level_unproven_glyphs_are_never_deleted() {
    let Some(worker) = worker() else { return };
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("safety.pdf");
    document(&[PageSpec::Mixed { overlay: false }])
        .save(&path)
        .unwrap();
    let mut lo = Document::load(&path).unwrap();
    let doc = worker.open(&path).unwrap();
    let bound = bind_page(&worker, doc, &lo, 1).unwrap();
    bound.check_replacement().expect("门禁应通过");

    let paragraphs = paragraphs_of(&bound);
    let normal = paragraphs.iter().find(|p| p.text == "ABC").unwrap().clone();
    let unproven = bound.unproven_source_ops();
    assert_eq!(unproven.len(), 1);
    let unproven_glyphs: Vec<GlyphId> = bound
        .ir
        .glyphs()
        .filter(|g| g.id.op == unproven[0].op)
        .map(|g| g.id)
        .collect();
    assert_eq!(
        unproven_glyphs.len(),
        2,
        "<00010002> 两个 code 各成一个字形"
    );

    // 混合批次（含降级操作字形）必须被整批拒绝，文档不动。
    let before = lo.clone();
    let unproven_para = unsafe_paragraph_with(&bound, &unproven_glyphs);
    let mixed: Vec<&Paragraph> = vec![&normal, &unproven_para];
    assert!(
        matches!(
            delete_translated(&mut lo, &bound, &mixed),
            Err(PipelineError::Protocol(_))
        ),
        "删除降级操作字形必须被拒绝"
    );
    assert_eq!(lo.objects.len(), before.objects.len());

    // 只删正常段成功。
    delete_translated(&mut lo, &bound, &[&normal]).expect("只删正常段应成功");
    let content = lo.get_page_content(bound.page_id);
    assert!(
        !content.windows(5).any(|w| w == b"(ABC)"),
        "正常段源字节应被删除"
    );
    // 降级操作的字面十六进制串逐字节保留（内容流里以 ASCII `<00010002>` 书写）。
    assert!(
        content
            .windows(b"<00010002>".len())
            .any(|w| w == b"<00010002>"),
        "降级操作 `<00010002>` 的源字节必须逐字节保留"
    );
    worker.close(doc);
}

/// 手工构造只含指定字形的段落（模拟段落归属降级字形的情况）。
fn unsafe_paragraph_with(bound: &BoundPage, ids: &[GlyphId]) -> Paragraph {
    Paragraph {
        id: ParagraphId::new(bound.ir.page, 2),
        page: bound.ir.page,
        region: 0,
        kind: RegionKind::Text,
        bbox: bound.ir.media_box,
        lines: vec![],
        glyphs: ids.to_vec(),
        text_spans: Vec::new(),
        style_runs: vec![],
        atoms: vec![],
        decorations: Vec::new(),
        text: "unproven".into(),
        align: syncpdf_core::ir::Align::Left,
        first_indent: 0.0,
        line_height: 12.0,
        is_rtl: false,
        translatable: Translatable::Yes,
    }
}

// ---------------------------------------------------------------------------
// 页级：结构错误只保留该页
// ---------------------------------------------------------------------------

/// 一页结构错误 + 一页正常 → source_analysis 不中止，坏页整页保留原文，
/// 好页照常绑定。
#[test]
fn page_level_structural_error_keeps_only_that_page() {
    let Some(worker) = worker() else { return };
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("two.pdf");
    document(&[PageSpec::Structural, PageSpec::Normal])
        .save(&path)
        .unwrap();
    let lo = Document::load(&path).unwrap();
    let doc = worker.open(&path).unwrap();

    // 门禁仍拒绝（页级结构错误：重复源操作）。
    let bad = bind_page(&worker, doc, &lo, 1).unwrap();
    assert!(
        matches!(
            bad.check_replacement(),
            Err(syncpdf_pdf::bind::ReplacementError::DuplicateSourceOperation(_))
        ),
        "重复源操作必须按页级拒绝"
    );
    assert!(
        bad.issues
            .iter()
            .any(|i| i.starts_with("duplicate source operation")),
        "{:?}",
        bad.issues
    );

    // source_analysis 不再中止：坏页以「不可信」形式随结果返回。
    let pages = source_analysis(
        &worker,
        doc,
        &lo,
        &[0, 1],
        &CancellationToken::new(),
        |_, _| {},
    )
    .expect("页级结构错误不应中止 source_analysis");
    assert_eq!(pages.len(), 2);
    // 坏页：全部段落不可译、页标记为保留原文；好页：正常。
    assert!(pages[0].reliability.is_unreliable(), "坏页应标记为不可信");
    assert!(pages[1].reliability.is_reliable(), "好页应正常");
    let bad_paras = paragraphs_of(&pages[0]);
    assert!(
        bad_paras.iter().all(|p| matches!(&p.translatable, Translatable::No { reason } if reason == "bind_page_unreliable")),
        "坏页所有段落应保留原文，实际 {:?}",
        bad_paras.iter().map(|p| &p.translatable).collect::<Vec<_>>()
    );
    let good_paras = paragraphs_of(&pages[1]);
    assert!(
        good_paras
            .iter()
            .all(|p| matches!(p.translatable, Translatable::Yes)),
        "好页段落应正常可译"
    );
    worker.close(doc);
}

/// 页级结构错误（取不到几何 / 结构不一致）→ 整页保留（不猜）。
///
/// 重复 Contents 引用产生 duplicate source operation；无几何降级
/// （no_pdfium_object / 对象边界退化）由 `check_replacement` 的
/// `degraded == unproven_ops.len()` 恒等式同样 fail-closed 成页级。
#[test]
fn page_level_reject_falls_back_to_keep_whole_page() {
    let Some(worker) = worker() else { return };
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("nogeom.pdf");
    document(&[PageSpec::Structural, PageSpec::Normal])
        .save(&path)
        .unwrap();
    let lo = Document::load(&path).unwrap();
    let doc = worker.open(&path).unwrap();

    let bad = bind_page(&worker, doc, &lo, 1).unwrap();
    assert!(
        bad.issues
            .iter()
            .any(|i| i.starts_with("duplicate source operation")),
        "重复源操作应留 issue（页级）：{:?}",
        bad.issues
    );
    assert!(bad.check_replacement().is_err(), "页级结构错误必须拒绝");

    let pages = source_analysis(
        &worker,
        doc,
        &lo,
        &[0, 1],
        &CancellationToken::new(),
        |_, _| {},
    )
    .expect("不应中止");
    assert!(pages[0].reliability.is_unreliable());
    assert!(pages[1].reliability.is_reliable());
    worker.close(doc);
}

fn worker() -> Option<PdfiumWorker> {
    match PdfiumWorker::spawn() {
        Ok(w) => Some(w),
        Err(e) => {
            eprintln!("SKIP: pdfium 不可用：{e}");
            None
        }
    }
}
