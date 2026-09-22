//! 输出自校验（M1-13）。
//!
//! [`self_check`] 对写出的 PDF 做端到端体检：
//!
//! 1. lopdf 能重开；
//! 2. 每页内容流能 `parse_content`；
//! 3. 每页 `/Resources/Font` 每项能解析为字典；
//! 4. `qpdf --check` 退出码 0（qpdf 不存在则记 `qpdf_checked = false`）；
//! 5. pdfium 能重开、每页可提取文本；`expect_cjk_on_pages` 指定的页需含 CJK。

use std::path::{Path, PathBuf};

use lopdf::{Dictionary, Document, Object, ObjectId};

use crate::content::parse_content;
use crate::pdfium::PdfiumWorker;

/// qpdf 可执行文件位置（缺失则跳过相应检查）。
const QPDF: &str = "/opt/homebrew/bin/qpdf";

/// 自校验报告。
#[derive(Debug, Clone)]
pub struct Report {
    /// 全部检查是否通过。
    pub ok: bool,
    /// 页数。
    pub pages: u32,
    /// 问题描述（会导致 `ok == false`）。
    pub problems: Vec<String>,
    /// 非致命告警（例如 qpdf 的 exit 3 对象号空洞警告），不影响 `ok`。
    pub warnings: Vec<String>,
    /// 是否实际跑了 qpdf。
    pub qpdf_checked: bool,
    /// 每页文本前 40 字符（pdfium 可读时）。
    pub text_sample: Vec<String>,
}

/// 校验错误（仅表示无法开始校验，例如文件打不开）。
#[derive(Debug, thiserror::Error)]
pub enum ValidateError {
    /// lopdf 打开失败。
    #[error("open {path}: {source}")]
    Open {
        /// 路径。
        path: PathBuf,
        /// 底层错误。
        source: lopdf::Error,
    },
    /// pdfium 不可用。
    #[error("pdfium: {0}")]
    Pdfium(#[from] crate::pdfium::PdfiumError),
}

/// 结果别名。
pub type Result<T, E = ValidateError> = std::result::Result<T, E>;

/// 对 `path` 做自校验；`expect_cjk_on_pages` 为 1 基页号列表。
pub fn self_check(path: &Path, expect_cjk_on_pages: &[u32]) -> Result<Report> {
    let mut problems = Vec::new();
    let doc = Document::load(path).map_err(|source| ValidateError::Open {
        path: path.to_path_buf(),
        source,
    })?;

    let pages = doc.get_pages();
    let page_count = pages.len() as u32;

    // 2) 内容流可解析；3) 字体资源可解析。
    for (num, id) in &pages {
        for sid in doc.get_page_contents(*id) {
            match doc.get_object(sid) {
                Ok(Object::Stream(s)) => {
                    let bytes = s
                        .decompressed_content()
                        .unwrap_or_else(|_| s.content.clone());
                    if let Err(e) = parse_content(&bytes) {
                        problems.push(format!("page {num}: content stream {} parse: {e}", sid.0));
                    }
                }
                _ => problems.push(format!("page {num}: stream {} missing", sid.0)),
            }
        }
        for (name, fid) in page_fonts(&doc, *id) {
            if !is_font_dict(&doc, fid) {
                problems.push(format!("page {num}: font /{name} ({}) unresolvable", fid.0));
            }
        }
    }

    // 4) qpdf --check。
    let mut warnings: Vec<String> = Vec::new();
    let mut qpdf_checked = false;
    let qpdf = Path::new(QPDF);
    if qpdf.is_file() {
        qpdf_checked = true;
        match std::process::Command::new(qpdf)
            .arg("--check")
            .arg(path)
            .output()
        {
            Ok(out) if out.status.success() => {}
            Ok(out) => {
                // qpdf 退出码 2 = 错误，3 = 仅警告（"operation succeeded with warnings"）。
                // lopdf 重写对象表后对象号会留空洞，qpdf 为此报 exit 3 警告但文件仍可正常读取，
                // 故仅退出码 2 视为硬失败，其余记入 `warnings`。
                let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
                if out.status.code() == Some(2) {
                    problems.push(format!("qpdf --check exit 2: {stderr}"));
                } else {
                    warnings.push(format!(
                        "qpdf --check exit {:?}: {stderr}",
                        out.status.code()
                    ));
                }
            }
            Err(e) => problems.push(format!("qpdf run failed: {e}")),
        }
    }

    // 5) pdfium 重开 + 文本提取。
    let mut text_sample = Vec::new();
    if let Ok(worker) = PdfiumWorker::spawn() {
        match worker.open(path) {
            Ok(docid) => {
                for page in 0..page_count {
                    match worker.page_text_objects(docid, page) {
                        Ok(objs) => {
                            let text: String = objs
                                .iter()
                                .flat_map(|o| o.chars.iter())
                                .filter_map(|c| c.unicode.as_deref())
                                .collect();
                            let sample: String = text.chars().take(40).collect();
                            let num = page + 1;
                            if expect_cjk_on_pages.contains(&num) && !text.chars().any(is_cjk) {
                                problems.push(format!("page {num}: no CJK text found"));
                            }
                            text_sample.push(sample);
                        }
                        Err(e) => problems.push(format!("page {}: pdfium text: {e}", page + 1)),
                    }
                }
            }
            Err(e) => problems.push(format!("pdfium reopen: {e}")),
        }
    } else {
        problems.push("pdfium unavailable".to_string());
    }

    Ok(Report {
        ok: problems.is_empty(),
        pages: page_count,
        problems,
        warnings,
        qpdf_checked,
        text_sample,
    })
}

/// 一个 code point 是否为 CJK 表意文字。
fn is_cjk(c: char) -> bool {
    matches!(c as u32, 0x4E00..=0x9FFF)
}

/// 页 `/Resources/Font` 的「名字 → 对象 id」表。
fn page_fonts(doc: &Document, page_id: ObjectId) -> Vec<(String, ObjectId)> {
    let mut out = Vec::new();
    let Ok(d) = dict_of(doc, page_id) else {
        return out;
    };
    let Some(res) = d.get(b"Resources").ok().and_then(|o| resolve(doc, o)) else {
        return out;
    };
    let Some(rd) = res.as_dict().ok() else {
        return out;
    };
    let Some(fonts) = rd.get(b"Font").ok().and_then(|o| resolve(doc, o)) else {
        return out;
    };
    let Some(fd) = fonts.as_dict().ok() else {
        return out;
    };
    for (k, v) in fd.iter() {
        if let Ok(id) = v.as_reference() {
            out.push((String::from_utf8_lossy(k).into_owned(), id));
        }
    }
    out
}

/// 对象是否为可解析的字体字典（字典或 Type0/简单字体字典均可）。
fn is_font_dict(doc: &Document, id: ObjectId) -> bool {
    match doc.get_object(id) {
        Ok(Object::Dictionary(d)) => {
            d.get(b"Type")
                .ok()
                .and_then(|o| o.as_name().ok())
                .map(|n| n == b"Font")
                .unwrap_or(false)
                || d.get(b"Subtype").is_ok()
        }
        Ok(Object::Stream(s)) => s.dict.get(b"Subtype").is_ok(),
        _ => false,
    }
}

fn resolve<'a>(doc: &'a Document, o: &'a Object) -> Option<&'a Object> {
    match o {
        Object::Reference(id) => doc.get_object(*id).ok(),
        other => Some(other),
    }
}

fn dict_of(doc: &Document, id: ObjectId) -> Result<&Dictionary, ()> {
    match doc.get_object(id) {
        Ok(Object::Dictionary(d)) => Ok(d),
        Ok(Object::Stream(s)) => Ok(&s.dict),
        _ => Err(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use lopdf::Stream;

    fn write_doc(path: &Path) {
        let mut doc = Document::with_version("1.7");
        let pages_id = doc.new_object_id();
        let content = doc.add_object(Stream::new(
            Dictionary::new(),
            b"BT /F1 12 Tf 50 700 Td (Hi) Tj ET".to_vec(),
        ));
        let mut font = Dictionary::new();
        font.set("Type", Object::Name(b"Font".to_vec()));
        font.set("Subtype", Object::Name(b"Type1".to_vec()));
        font.set("BaseFont", Object::Name(b"Helvetica".to_vec()));
        let fid = doc.add_object(font);
        let mut fdict = Dictionary::new();
        fdict.set("F1", Object::Reference(fid));
        let mut res = Dictionary::new();
        res.set("Font", fdict);
        let mut page = Dictionary::new();
        page.set("Type", Object::Name(b"Page".to_vec()));
        page.set("Parent", Object::Reference(pages_id));
        page.set("MediaBox", vec![0.into(), 0.into(), 595.into(), 842.into()]);
        page.set("Contents", Object::Reference(content));
        page.set("Resources", res);
        let page_id = doc.add_object(page);
        let mut pages = Dictionary::new();
        pages.set("Type", Object::Name(b"Pages".to_vec()));
        pages.set("Kids", vec![Object::Reference(page_id)]);
        pages.set("Count", 1);
        doc.objects.insert(pages_id, Object::Dictionary(pages));
        let cat = doc.add_object(Dictionary::new());
        let cd = doc.get_object_mut(cat).unwrap().as_dict_mut().unwrap();
        cd.set("Type", Object::Name(b"Catalog".to_vec()));
        cd.set("Pages", Object::Reference(pages_id));
        doc.trailer.set("Root", Object::Reference(cat));
        doc.save(path).unwrap();
    }

    #[test]
    fn self_check_clean_doc() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a.pdf");
        write_doc(&path);
        let r = self_check(&path, &[]).unwrap();
        assert_eq!(r.pages, 1);
        assert!(r.ok, "problems={:?}", r.problems);
    }

    #[test]
    fn self_check_missing_file_errors() {
        let e = self_check(Path::new("/nonexistent/x.pdf"), &[]);
        assert!(matches!(e, Err(ValidateError::Open { .. })));
    }

    #[test]
    fn is_cjk_ranges() {
        assert!(is_cjk('中'));
        assert!(is_cjk('一'));
        assert!(!is_cjk('A'));
        assert!(!is_cjk('\u{3000}'));
    }

    #[test]
    fn report_has_one_text_sample_per_page() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("b.pdf");
        write_doc(&path);
        let r = self_check(&path, &[]).unwrap();
        assert_eq!(r.text_sample.len(), r.pages as usize);
    }
}
