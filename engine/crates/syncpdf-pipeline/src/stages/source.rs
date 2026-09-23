//! source_analysis 阶段：每页 `bind_page` → [`BoundPage`]（含 [`PageIR`]）。
//!
//! 设计基准：02-技术路径与架构.md §3（source_analysis 行）。
//!
//! pdfium 调用必须串行（`PdfiumWorker` 内部是单线程 worker + 通道），因此这里
//! 逐页串行绑定，不做 rayon 并行；要并行的是每个页面里不碰 pdfium 的部分，
//! 目前 `bind_page` 已把两者合在一起，收益有限（见回报「已知缺口」）。
//!
//! 每页开始前检查取消令牌；已知不可信绑定在返回翻译输入前作为 Protocol 拒绝。

use syncpdf_core::ir::PageIR;
use syncpdf_pdf::bind::{bind_page, BindError, BoundPage};
use syncpdf_pdf::pdfium::{DocId, PdfiumWorker};

use crate::cancel::CancellationToken;

use super::{check_cancelled, PipelineError};

/// 逐页绑定，返回与 `pages` 同序的 [`BoundPage`]。
///
/// `pages` 是 **0 基**页号（与 `run.rs` 的 `selected`、preflight 的 `page_infos`
/// 一致）；`syncpdf_pdf::bind::bind_page` 要 1 基页号，内部会 `+1`。
/// `on_progress(done, total)` 在每页完成后调用一次。
/// 取消命中时返回 [`PipelineError::Cancelled`]（已绑定的页丢弃）。
pub fn source_analysis(
    worker: &PdfiumWorker,
    doc: DocId,
    lo: &lopdf::Document,
    pages: &[u32],
    cancel: &CancellationToken,
    mut on_progress: impl FnMut(u32, u32),
) -> Result<Vec<BoundPage>, PipelineError> {
    let total = pages.len() as u32;
    let mut out: Vec<BoundPage> = Vec::with_capacity(pages.len());
    for (i, page) in pages.iter().copied().enumerate() {
        check_cancelled(cancel)?;
        // `bind_page` 收 1 基页号（`PageIR.page` 是 0 基，见 `bind.rs`）。
        let bound = bind_page(worker, doc, lo, page + 1).map_err(bind_error)?;
        bound.check_replacement().map_err(|e| {
            PipelineError::Protocol(format!("page {} replacement rejected: {e}", page + 1))
        })?;
        out.push(bound);
        on_progress(i as u32 + 1, total);
    }
    Ok(out)
}

/// 从 `Vec<BoundPage>` 里取每页的 [`PageIR`]（供 stage 缓存 / 段落分析用）。
pub fn page_irs(bound: &[BoundPage]) -> Vec<PageIR> {
    bound.iter().map(|b| b.ir.clone()).collect()
}

/// `BindError` → [`PipelineError`]：pdfium 侧透传，其余按协议错误处理。
fn bind_error(e: BindError) -> PipelineError {
    match e {
        BindError::Pdfium(e) => PipelineError::Pdfium(e),
        other => PipelineError::Protocol(format!("bind_page 失败：{other}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::require_fixture;

    fn worker() -> Option<PdfiumWorker> {
        match PdfiumWorker::spawn() {
            Ok(w) => Some(w),
            Err(e) => {
                eprintln!("SKIP: pdfium 不可用：{e}");
                None
            }
        }
    }

    fn run_pages(path: &std::path::Path, pages: &[u32]) -> Option<Vec<BoundPage>> {
        let w = worker()?;
        let lo = lopdf::Document::load(path).expect("lopdf 载入");
        let doc = w.open(path).expect("pdfium 打开");
        let cancel = CancellationToken::new();
        let mut progress = Vec::new();
        let out = source_analysis(&w, doc, &lo, pages, &cancel, |d, t| progress.push((d, t)));
        w.close(doc);
        let out = out.expect("source_analysis");
        assert_eq!(progress.len(), pages.len(), "每页应报一次进度");
        assert_eq!(
            progress.last().copied(),
            Some((pages.len() as u32, pages.len() as u32))
        );
        Some(out)
    }

    #[test]
    fn ci_test_first_page_binds() {
        let path = require_fixture!("ci-test.pdf");
        let Some(bound) = run_pages(&path, &[0]) else {
            return;
        };
        assert_eq!(bound.len(), 1);
        assert_eq!(bound[0].ir.page.0, 0, "PageIR.page 是 0 基");
        assert!(bound[0].ir.media_box.width() > 0.0);
    }

    #[test]
    fn up_vns_first_two_pages_bind_in_order() {
        let path = require_fixture!("up-vns.pdf");
        let Some(bound) = run_pages(&path, &[0, 1]) else {
            return;
        };
        assert_eq!(bound.len(), 2);
        assert_eq!(bound[0].ir.page.0, 0);
        assert_eq!(bound[1].ir.page.0, 1);
        // 前两页都有文本（绑定出的字形数应为正）。
        for b in &bound {
            let glyphs = b.ir.glyphs().count();
            assert!(glyphs > 0, "第 {} 页应绑定出字形", b.ir.page.number());
        }
    }

    #[test]
    fn cancelling_before_start_returns_cancelled() {
        let path = require_fixture!("up-vns.pdf");
        let Some(w) = worker() else { return };
        let lo = lopdf::Document::load(&path).unwrap();
        let doc = w.open(&path).unwrap();
        let cancel = CancellationToken::new();
        cancel.cancel();
        let e = source_analysis(&w, doc, &lo, &[0, 1], &cancel, |_, _| {}).unwrap_err();
        w.close(doc);
        assert!(matches!(e, PipelineError::Cancelled));
        assert_eq!(e.code(), "cancelled");
    }

    #[test]
    fn page_out_of_range_is_a_protocol_error() {
        let path = require_fixture!("ci-test.pdf");
        let Some(w) = worker() else { return };
        let lo = lopdf::Document::load(&path).unwrap();
        let doc = w.open(&path).unwrap();
        let e = source_analysis(&w, doc, &lo, &[9999], &CancellationToken::new(), |_, _| {})
            .unwrap_err();
        w.close(doc);
        assert_eq!(e.code(), "protocol", "{e:?}");
    }

    #[test]
    fn page_irs_matches_input_order() {
        let path = require_fixture!("up-vns.pdf");
        let Some(bound) = run_pages(&path, &[1, 0]) else {
            return;
        };
        let irs = page_irs(&bound);
        assert_eq!(irs.len(), 2);
        assert_eq!(irs[0].page.0, 1);
        assert_eq!(irs[1].page.0, 0);
    }
}
