//! preflight 阶段：打开输入、加密/签名检查、页数与每页几何。
//!
//! 设计基准：02-技术路径与架构.md §3（preflight 行）。
//! 加密 PDF 直接拒绝（`PipelineError::Encrypted`）；签名 PDF 只记 warning 继续
//! （改文件会破坏已有签名，但不阻断翻译——由上层决定是否提示用户）。

use std::path::Path;

use syncpdf_core::hash::Sha256Hash;
use syncpdf_pdf::pdfium::{DocId, PageInfo, PdfiumWorker};

use super::PipelineError;

/// preflight 的产物。
#[derive(Debug, Clone)]
pub struct Preflight {
    /// 文档总页数。
    pub pages: u32,
    /// 每页几何（0 基，与页号同序）。
    pub page_infos: Vec<PageInfo>,
    /// 是否加密（为 true 时 `preflight` 已返回错误，不会产出该结构）。
    pub encrypted: bool,
    /// 是否带数字签名。
    pub signed: bool,
    /// 输入文件字节的 sha256（阶段缓存键）。
    pub source_sha: Sha256Hash,
    /// pdfium worker 侧的文档句柄（调用方负责 `close`）。
    pub doc: DocId,
}

/// 打开输入并做前置检查。
///
/// 失败时若 `doc` 已打开会先 `close`，不把句柄漏给调用方。
pub fn preflight(worker: &PdfiumWorker, input: &Path) -> Result<Preflight, PipelineError> {
    let bytes = std::fs::read(input).map_err(|e| PipelineError::io(input, e))?;
    let source_sha = Sha256Hash::of(&bytes);

    let doc = worker.open(input)?;
    let result = preflight_open(worker, doc, source_sha);
    if result.is_err() {
        worker.close(doc);
    }
    result
}

fn preflight_open(
    worker: &PdfiumWorker,
    doc: DocId,
    source_sha: Sha256Hash,
) -> Result<Preflight, PipelineError> {
    if worker.is_encrypted(doc)? {
        return Err(PipelineError::Encrypted(source_sha.to_hex()));
    }
    let signed = worker.has_signatures(doc)?;
    if signed {
        // 不阻断：输出会写入新文件，原文件不动，但用户应知道签名不复存在。
        tracing::warn!("输入 PDF 带数字签名；输出将是未签名的新文件");
    }
    let pages = worker.page_count(doc)?;
    let mut page_infos = Vec::with_capacity(pages as usize);
    for p in 0..pages {
        page_infos.push(worker.page_info(doc, p)?);
    }
    Ok(Preflight {
        pages,
        page_infos,
        encrypted: false,
        signed,
        source_sha,
        doc,
    })
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

    #[test]
    fn ci_test_preflight_reports_pages_and_geometry() {
        let path = require_fixture!("ci-test.pdf");
        let Some(w) = worker() else { return };
        let pf = preflight(&w, &path).unwrap();
        assert!(pf.pages >= 1, "页数 {}", pf.pages);
        assert_eq!(pf.page_infos.len(), pf.pages as usize);
        assert!(!pf.encrypted);
        for info in &pf.page_infos {
            assert!(info.width > 0.0 && info.height > 0.0);
            assert!(info.rotation % 90 == 0);
            assert!(!info.media_box.is_empty());
        }
        w.close(pf.doc);
    }

    #[test]
    fn preflight_source_sha_matches_file_bytes() {
        let path = require_fixture!("ci-test.pdf");
        let Some(w) = worker() else { return };
        let expected = Sha256Hash::of(std::fs::read(&path).unwrap());
        let pf = preflight(&w, &path).unwrap();
        assert_eq!(pf.source_sha, expected);
        w.close(pf.doc);
    }

    #[test]
    fn missing_input_is_an_io_error() {
        let Some(w) = worker() else { return };
        let err = preflight(&w, Path::new("/nonexistent/nope.pdf")).unwrap_err();
        assert!(matches!(err, PipelineError::Io { .. }), "{err:?}");
        assert_eq!(err.code(), "io");
    }

    #[test]
    fn up_vns_page_infos_are_consistent() {
        let path = require_fixture!("up-vns.pdf");
        let Some(w) = worker() else { return };
        let pf = preflight(&w, &path).unwrap();
        assert_eq!(pf.pages, 12, "up-vns 应为 12 页");
        assert_eq!(pf.page_infos.len(), 12);
        w.close(pf.doc);
    }
}
