//! writeback 阶段：删除被翻译段落的原字形 + 生成文档快照。
//!
//! 设计基准：02-技术路径与架构.md §6.4（回写）与 §3（publishing 行）。
//!
//! # 快照策略（关键设计 #3）
//!
//! run 期间**只有一份主文档** `lopdf::Document`：原文减去已删字形（只做
//! [`delete_translated`]，**不写译文**）。每次某页就绪时，克隆主文档 →
//! 用临时 [`Writer`] 把**到目前为止所有就绪页**的译文重放一遍 →
//! `finalize` → 原子 `save`。这样：
//!
//! * 中间快照与最终产物走完全相同的代码路径，快照永远是「完整可读的 PDF」；
//! * 不需要给 `Writer` 增加「快照 finalize」API（它持有累积状态，无法克隆）；
//! * 代价是重复嵌入字体（12 页文档重放 12 次，实测见回报）。
//!
//! 主文档不在本阶段写译文，因此 `render_snapshot` 的 `base` 是**已删字形**的主
//! 文档；译文流全部由重放产生。
//!
//! # 页号约定
//!
//! `syncpdf_pipeline` 内部页号是 **0 基**（与 `run.rs::selected`、`PageIR.page`
//! 一致），而 `syncpdf_pdf::{patch, writer}` 收 **1 基**页号。本模块的公开函数
//! 统一收 0 基，内部 `+1` 后再调用（`self_check` 的 `expect_cjk_on_pages` 则要
//! 1 基，调用方负责）。

use std::collections::BTreeMap;
use std::path::Path;

use lopdf::Document;
use syncpdf_core::ir::{Paragraph, TypesetParagraph};
use syncpdf_font::FontStore;
use syncpdf_pdf::bind::BoundPage;
use syncpdf_pdf::patch::{PatchSet, PatchStats};
use syncpdf_pdf::writer::{FontStats, Writer};

use super::PipelineError;

/// 删除 `paras` 里所有成功段落（`typeset`/`translated`）的原字形。
///
/// 调用方（`run.rs`）只应传入**已就绪且译文校验通过**的段落；这里不再过滤状态，
/// 只删除 `Paragraph::glyphs`。`bound` 提供页内的 Form `Do` 记录（
/// `PatchSet::delete_glyphs`），使 Form XObject 内的字形也能删除（克隆 Form 流）。
///
/// 页号取 `bound.ir.page.number()`（1 基）。
/// 无删除时返回全零 [`PatchStats`] 且**不碰文档**。
pub fn delete_translated(
    doc: &mut Document,
    bound: &BoundPage,
    paras: &[&Paragraph],
) -> Result<PatchStats, PipelineError> {
    let ids: Vec<_> = paras
        .iter()
        .flat_map(|para| para.glyphs.iter().copied())
        .collect();
    if ids.is_empty() {
        return Ok(PatchStats::default());
    }
    let mut ps = PatchSet::new();
    ps.delete_glyphs(bound, &ids).map_err(patch_error)?;
    let page = bound.ir.page.number();
    ps.apply(doc, page).map_err(patch_error)
}

/// 生成一份快照：克隆 `base` → 重放全部就绪页译文 → `finalize` → 原子保存。
///
/// * `typeset_by_page`：页（**0 基**）→ 该页译文段落（按到达顺序）。
/// * `page_heights`：页（0 基）→ 页高（pt，来自 `PageIR.media_box`）；
///   当前 `Writer::write_paragraphs` 不用于翻页，但签名要求，故照传。
/// * 页按 key 升序重放，保证同一个 `cid` 登记顺序稳定、快照可复现。
///
/// 返回嵌入字体统计（最终发布时供 `Stats` 使用）。
pub fn render_snapshot(
    base: &Document,
    store: &FontStore,
    typeset_by_page: &BTreeMap<u32, Vec<TypesetParagraph>>,
    page_heights: &BTreeMap<u32, f32>,
    output: &Path,
) -> Result<FontStats, PipelineError> {
    let mut doc = base.clone();
    let stats = replay_into(&mut doc, store, typeset_by_page, page_heights)?;
    syncpdf_pdf::writer::save(&mut doc, output).map_err(write_error)?;
    Ok(stats)
}

/// 把 `typeset_by_page` 全部重放进 `doc` 并 `finalize`（不保存）。
///
/// 单独暴露给测试与 `run.rs`（最终发布与中间快照共用同一条路径）。
pub fn replay_into(
    doc: &mut Document,
    store: &FontStore,
    typeset_by_page: &BTreeMap<u32, Vec<TypesetParagraph>>,
    page_heights: &BTreeMap<u32, f32>,
) -> Result<FontStats, PipelineError> {
    let mut writer = Writer::new(store);
    for (page, paras) in typeset_by_page {
        if paras.is_empty() {
            continue;
        }
        let h = page_heights.get(page).copied().unwrap_or(792.0);
        writer
            .write_paragraphs(doc, page + 1, paras, h)
            .map_err(write_error)?;
    }
    writer.finalize(doc).map_err(write_error)
}

/// `PatchError` → [`PipelineError`]（协议层，不可继续）。
fn patch_error(e: syncpdf_pdf::patch::PatchError) -> PipelineError {
    PipelineError::Protocol(format!("patch 失败：{e}"))
}

/// `WriteError` → [`PipelineError`]。
fn write_error(e: syncpdf_pdf::writer::WriteError) -> PipelineError {
    match e {
        syncpdf_pdf::writer::WriteError::Lopdf(e) => PipelineError::Protocol(format!("lopdf：{e}")),
        other => PipelineError::Protocol(format!("writer 失败：{other}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::fixtures::fonts_dir;
    use syncpdf_core::ir::{DisplayItem, Region, RegionKind, StyleRun, Translatable};
    use syncpdf_core::require_fixture;
    use syncpdf_core::{Color, GlyphId, ParagraphId, Rect, StyleId};
    use syncpdf_font::{FontId, FontQuery, FontStore};
    use syncpdf_pdf::bind::bind_page;
    use syncpdf_pdf::pdfium::{DocId, PdfiumWorker};
    use syncpdf_pdf::validate::self_check;
    use syncpdf_translate::parse_unit_html;

    use crate::stages::paragraph::analyze_page;
    use crate::stages::typeset::{inlines_from_parsed, spec_for, StoreShaper};

    const TRANSLATION: &str = "测试译文";

    /// pdfium + 字体包都可用时的 (worker, store, 中文字体 id)。
    fn env() -> Option<(PdfiumWorker, FontStore, FontId)> {
        let worker = match PdfiumWorker::spawn() {
            Ok(w) => w,
            Err(e) => {
                eprintln!("SKIP: pdfium 不可用：{e}");
                return None;
            }
        };
        let dir = fonts_dir()?;
        let store = match FontStore::load_builtin(&dir) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("SKIP: 字体包缺失：{e}");
                return None;
            }
        };
        let q = FontQuery {
            script: syncpdf_font::loader::Script::HanSC,
            ..Default::default()
        };
        let id = store.find(&q).or_else(|| store.ids().first().copied())?;
        Some((worker, store, id))
    }

    fn bind_first(path: &Path, worker: &PdfiumWorker) -> (lopdf::Document, DocId, BoundPage) {
        let lo = lopdf::Document::load(path).expect("lopdf 载入");
        let doc = worker.open(path).expect("pdfium 打开");
        let bound = bind_page(worker, doc, &lo, 1).expect("bind page 1");
        (lo, doc, bound)
    }

    /// 全部非空文本 item 的并集框。
    fn text_bbox(ir: &syncpdf_core::ir::PageIR) -> Option<Rect> {
        let mut bbox: Option<Rect> = None;
        for g in ir.glyphs() {
            bbox = Some(match bbox {
                None => g.bbox,
                Some(b) => b.union(&g.bbox),
            });
        }
        bbox
    }

    /// 手工构造一个段落（不依赖 region 切分，ci-test / up-vns 通用）。
    fn manual_paragraph(
        ir: &syncpdf_core::ir::PageIR,
        bbox: Rect,
        glyphs: Vec<GlyphId>,
    ) -> Paragraph {
        let size = ir.glyphs().next().map(|g| g.size.max(10.0)).unwrap_or(10.0);
        Paragraph {
            id: ParagraphId::new(ir.page, 1),
            page: ir.page,
            region: 0,
            kind: RegionKind::Text,
            bbox,
            lines: Vec::new(),
            glyphs,
            style_runs: vec![StyleRun {
                id: StyleId(0),
                glyph_range: (0, 0),
                font: 0,
                size,
                color: Color::BLACK,
                bold: false,
                italic: false,
            }],
            atoms: Vec::new(),
            text: "original".into(),
            align: syncpdf_core::ir::Align::Left,
            first_indent: 0.0,
            line_height: size * 1.2,
            is_rtl: false,
            translatable: Translatable::Yes,
        }
    }

    /// 页上第一个非空文本 item 的 (bbox, glyph ids)。
    fn first_text_item(ir: &syncpdf_core::ir::PageIR) -> Option<(Rect, Vec<GlyphId>)> {
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

    /// 用 `analyze_page` 走一遍真实区域切分（若切得出段落就用它）。
    fn analyzed_paragraph(ir: &syncpdf_core::ir::PageIR) -> Option<Paragraph> {
        let bbox = text_bbox(ir)?;
        let region = Region {
            page: ir.page,
            index: 0,
            kind: RegionKind::Text,
            bbox,
            score: 1.0,
            order: 0,
        };
        analyze_page(ir, &[region]).into_iter().next()
    }

    /// 把 `text` 排进 `para.bbox`。
    fn typeset_text(
        store: &FontStore,
        fid: FontId,
        text: &str,
        para: &Paragraph,
    ) -> TypesetParagraph {
        // env() 给的中文字体必须真的在 store 里（profile 会据此挑 Body 字体）。
        assert!(store.get(fid).is_some(), "env() 返回的字体句柄应可解析");
        let shaper = StoreShaper {
            store,
            profile: &syncpdf_font::default_profile(store, "zh-CN"),
            role: syncpdf_font::Role::Body,
        };
        let parsed =
            parse_unit_html(&format!("<p id=\"{}\">{text}</p>", para.id)).expect("解析译文 html");
        let ts = syncpdf_typeset::Typeset::new(
            &shaper,
            syncpdf_typeset::FitOptions {
                min_scale: 0.5,
                scale_step: 0.05,
                line_height_steps: vec![1.2, 1.15, 1.1, 1.05, 1.0],
                allow_overflow_lines: 2,
            },
        );
        ts.layout(
            para.id.clone(),
            &spec_for(para),
            &inlines_from_parsed(&parsed, para),
            &syncpdf_typeset::Obstacles::default(),
        )
        .paragraph
    }

    fn page_text(worker: &PdfiumWorker, doc: DocId, page: u32) -> String {
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

    #[test]
    fn delete_and_snapshot_up_vns_page_1() {
        let Some((worker, store, fid)) = env() else {
            return;
        };
        let path = require_fixture!("up-vns.pdf");
        let (mut lo, docid, bound) = bind_first(&path, &worker);

        let (bbox, ids) = first_text_item(&bound.ir).expect("页 1 应有文本 item");
        let para = analyzed_paragraph(&bound.ir)
            .unwrap_or_else(|| manual_paragraph(&bound.ir, bbox, ids.clone()));
        assert!(!para.glyphs.is_empty(), "段落应含字形");

        let tp = typeset_text(&store, fid, TRANSLATION, &para);
        assert!(!tp.lines.is_empty(), "排出的段落应有行");
        assert!(
            tp.lines
                .iter()
                .flat_map(|l| l.glyphs.iter())
                .all(|g| store.get(FontId(g.font)).is_some()),
            "PlacedGlyph.font 必须是 FontId.0 且能在 store 里查到"
        );

        let stats = delete_translated(&mut lo, &bound, &[&para]).expect("删除应成功");
        assert!(stats.streams_touched > 0, "应改动到内容流：{stats:?}");

        let mut by_page: BTreeMap<u32, Vec<TypesetParagraph>> = BTreeMap::new();
        by_page.insert(0, vec![tp]);
        let mut heights: BTreeMap<u32, f32> = BTreeMap::new();
        heights.insert(0, bound.ir.media_box.height());

        let tmp = tempfile::tempdir().unwrap();
        let out = tmp.path().join("snap.pdf");
        let fs = render_snapshot(&lo, &store, &by_page, &heights, &out).expect("快照应成功");
        assert!(fs.fonts >= 1, "应嵌入至少一种字体：{fs:?}");
        assert!(out.exists());

        let report = self_check(&out, &[1]).expect("self_check 可跑");
        assert!(
            report.ok,
            "self_check 应通过：problems={:?} warnings={:?}",
            report.problems, report.warnings
        );

        let outid = worker.open(&out).expect("pdfium 重开快照");
        let text = page_text(&worker, outid, 0);
        assert!(
            text.contains(TRANSLATION),
            "页 1 文本应含 {TRANSLATION:?}，实际前 200 字符：{}",
            text.chars().take(200).collect::<String>()
        );
        worker.close(outid);

        // 主文档（只删字形、不写译文）不含译文。
        let base_out = tmp.path().join("base.pdf");
        syncpdf_pdf::writer::save(&mut lo.clone(), &base_out).expect("存 base");
        let baseid = worker.open(&base_out).expect("pdfium 开 base");
        let base_text = page_text(&worker, baseid, 0);
        assert!(
            !base_text.contains(TRANSLATION),
            "主文档 base 不应含译文：{}",
            base_text.chars().take(200).collect::<String>()
        );
        worker.close(baseid);
        worker.close(docid);
    }

    #[test]
    fn delete_and_snapshot_ci_test_page_1() {
        let Some((worker, store, fid)) = env() else {
            return;
        };
        let path = require_fixture!("ci-test.pdf");
        let (mut lo, docid, bound) = bind_first(&path, &worker);

        // ci-test 是合成页：即使没有可译文本，也要有一条可跑通的路径。
        let (bbox, ids) = match first_text_item(&bound.ir) {
            Some(v) => v,
            None => (Rect::new(72.0, 600.0, 400.0, 700.0), Vec::new()),
        };
        let para = manual_paragraph(&bound.ir, bbox, ids);
        let tp = typeset_text(&store, fid, TRANSLATION, &para);
        assert!(!tp.lines.is_empty());

        if !para.glyphs.is_empty() {
            let stats = delete_translated(&mut lo, &bound, &[&para]).expect("删除");
            assert!(stats.streams_touched > 0);
        } else {
            let stats = delete_translated(&mut lo, &bound, &[&para]).expect("空删除");
            assert_eq!(stats, PatchStats::default());
        }

        let mut by_page: BTreeMap<u32, Vec<TypesetParagraph>> = BTreeMap::new();
        by_page.insert(0, vec![tp]);
        let mut heights: BTreeMap<u32, f32> = BTreeMap::new();
        heights.insert(0, bound.ir.media_box.height());

        let tmp = tempfile::tempdir().unwrap();
        let out = tmp.path().join("ci.out.pdf");
        render_snapshot(&lo, &store, &by_page, &heights, &out).expect("快照");
        let report = self_check(&out, &[1]).expect("self_check");
        assert!(report.ok, "problems={:?}", report.problems);
        let outid = worker.open(&out).expect("pdfium 重开");
        let text = page_text(&worker, outid, 0);
        assert!(
            text.contains(TRANSLATION),
            "输出应含译文，实际：{:?}",
            text.chars().take(200).collect::<String>()
        );
        worker.close(outid);
        worker.close(docid);
    }

    #[test]
    fn empty_paragraph_list_is_a_noop() {
        let Some((worker, store, _)) = env() else {
            return;
        };
        let path = require_fixture!("up-vns.pdf");
        let (mut lo, docid, bound) = bind_first(&path, &worker);
        let before = lo.clone();
        let stats = delete_translated(&mut lo, &bound, &[]).expect("空列表");
        assert_eq!(stats, PatchStats::default());
        assert_eq!(lo.objects.len(), before.objects.len(), "文档不应被改动");

        let mut doc = lo.clone();
        let fs = replay_into(&mut doc, &store, &BTreeMap::new(), &BTreeMap::new()).expect("空重放");
        assert_eq!(fs.fonts, 0);
        assert_eq!(fs.glyphs, 0);
        worker.close(docid);
    }

    #[test]
    fn snapshot_replay_is_deterministic_and_leaves_no_temp_files() {
        let Some((worker, store, fid)) = env() else {
            return;
        };
        let path = require_fixture!("up-vns.pdf");
        let (mut lo, docid, bound) = bind_first(&path, &worker);
        let (bbox, ids) = first_text_item(&bound.ir).expect("文本 item");
        let para =
            analyzed_paragraph(&bound.ir).unwrap_or_else(|| manual_paragraph(&bound.ir, bbox, ids));
        let tp = typeset_text(&store, fid, TRANSLATION, &para);
        delete_translated(&mut lo, &bound, &[&para]).expect("删除");

        let mut by_page: BTreeMap<u32, Vec<TypesetParagraph>> = BTreeMap::new();
        by_page.insert(0, vec![tp]);
        let mut heights: BTreeMap<u32, f32> = BTreeMap::new();
        heights.insert(0, bound.ir.media_box.height());

        let tmp = tempfile::tempdir().unwrap();
        let a = tmp.path().join("a.pdf");
        let b = tmp.path().join("b.pdf");
        let sa = render_snapshot(&lo, &store, &by_page, &heights, &a).expect("快照 a");
        let sb = render_snapshot(&lo, &store, &by_page, &heights, &b).expect("快照 b");
        assert_eq!(sa, sb, "同输入重放两次统计应一致");
        let leftovers: Vec<String> = std::fs::read_dir(tmp.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.starts_with('.'))
            .collect();
        assert!(leftovers.is_empty(), "残留临时文件：{leftovers:?}");
        worker.close(docid);
    }

    #[test]
    fn multi_page_replay_writes_every_ready_page() {
        let Some((worker, store, fid)) = env() else {
            return;
        };
        let path = require_fixture!("up-vns.pdf");
        let lo = lopdf::Document::load(&path).unwrap();
        let docid = worker.open(&path).unwrap();
        let mut bounds = Vec::new();
        for page in 1..=2u32 {
            bounds.push(bind_page(&worker, docid, &lo, page).expect("bind"));
        }
        let mut main_doc = lo.clone();
        let mut by_page: BTreeMap<u32, Vec<TypesetParagraph>> = BTreeMap::new();
        let mut heights: BTreeMap<u32, f32> = BTreeMap::new();
        for (i, bound) in bounds.iter().enumerate() {
            let (bbox, ids) = first_text_item(&bound.ir)
                .unwrap_or((Rect::new(72.0, 600.0, 400.0, 700.0), Vec::new()));
            let para = manual_paragraph(&bound.ir, bbox, ids);
            let tp = typeset_text(&store, fid, TRANSLATION, &para);
            delete_translated(&mut main_doc, bound, &[&para]).expect("删除");
            by_page.insert(i as u32, vec![tp]);
            heights.insert(i as u32, bound.ir.media_box.height());
        }
        let tmp = tempfile::tempdir().unwrap();
        let out = tmp.path().join("two.pdf");
        render_snapshot(&main_doc, &store, &by_page, &heights, &out).expect("快照");
        let report = self_check(&out, &[1, 2]).expect("self_check");
        assert!(report.ok, "problems={:?}", report.problems);
        let outid = worker.open(&out).unwrap();
        for page in 0..2u32 {
            let t = page_text(&worker, outid, page);
            assert!(
                t.contains(TRANSLATION),
                "第 {} 页应含译文：{:?}",
                page + 1,
                t.chars().take(120).collect::<String>()
            );
        }
        worker.close(outid);
        worker.close(docid);
    }
}
