//! 译文流写入（M1-11）：把 [`TypesetParagraph`] 写成新的独立内容流。
//!
//! 设计基准：02-技术路径与架构.md §6.4。
//!
//! # 结构
//!
//! 每页追加一个内容流对象，字节形如：
//!
//! ```text
//! q
//! /Span <</ActualText <FEFF...>>> BDC
//! BT /SPF<n> <size> Tf <r> <g> <b> rg 1 0 0 1 <x> <y> Tm <gidhex> Tj ... ET
//! EMC
//! Q
//! ```
//!
//! `ActualText` 是该段译文的 UTF-16BE（带 BOM），保证复制/无障碍读取到译文。
//! 为正确优先，**每个字形单独 `Tm` + `Tj`**（`scale_x != 1` 时把缩放并入 `Tm`）。
//!
//! # cid 登记
//!
//! 内容流一旦写入就不该在子集化后改写。因此 Writer 为每个 `(font, gid)` 分配一个
//! **该字体内递增的 cid 序号**（从 1 起），内容流里写这个序号；`finalize` 时：
//!
//! 1. `subset(font, &原 gids)` 得到 `gid_map`；
//! 2. `/CIDToGIDMap` 写成显式流：`cid → gid_map[原 gid]`；
//! 3. ToUnicode 用 `cid → PlacedGlyph.text`。
//!
//! 这样内容流的字节永不需要回改。

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use lopdf::{Dictionary, Document, Object, ObjectId, Stream};
use syncpdf_core::ir::TypesetParagraph;
use syncpdf_font::{FontId, FontStore};

use crate::embed::embed_font_lopdf;

/// 字体统计。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct FontStats {
    /// 嵌入的字体数。
    pub fonts: u32,
    /// 写入的字形总数。
    pub glyphs: u32,
    /// 嵌入的字体文件总字节数。
    pub bytes: u64,
}

/// 写入错误。
#[derive(Debug, thiserror::Error)]
pub enum WriteError {
    /// lopdf 侧错误。
    #[error("lopdf: {0}")]
    Lopdf(#[from] lopdf::Error),
    /// 嵌入错误。
    #[error("embed: {0}")]
    Embed(#[from] crate::embed::EmbedError),
    /// 页号越界。
    #[error("page {page} out of range (1..={count})")]
    PageOutOfRange { page: u32, count: u32 },
    /// 字体 id 不在 store 里。
    #[error("font id {0} not in store")]
    UnknownFont(u32),
}

/// 结果别名。
pub type Result<T, E = WriteError> = std::result::Result<T, E>;

/// 单个 `font → (cid 登记表, ToUnicode 文本)` 的累积状态。
#[derive(Debug, Default)]
struct FontAcc {
    /// 原 gid → cid（1 基递增序号）。
    gid_to_cid: BTreeMap<u16, u16>,
    /// cid → 原 gid（登记顺序）。
    cid_to_gid_orig: Vec<u16>,
    /// cid → 文本（取首个非空）。
    to_unicode: BTreeMap<u16, String>,
}

/// 译文写入器。
#[derive(Debug)]
pub struct Writer<'a> {
    store: &'a FontStore,
    /// `FontId.0` → 累积状态。
    fonts: BTreeMap<u32, FontAcc>,
    /// 已占位的字体对象 id（finalize 时填充内容）。
    font_slots: BTreeMap<u32, ObjectId>,
    /// 已追加的流（页号 → 流对象 id），便于后续追加到 `/Contents`。
    page_streams: BTreeMap<u32, Vec<ObjectId>>,
    /// 触及的页对象 id（finalize 时把字体资源改指真对象）。
    page_ids: BTreeSet<ObjectId>,
    /// 字体文件字节合计。
    font_bytes: u64,
    /// 写入字形数。
    glyph_written: u32,
}

impl<'a> Writer<'a> {
    /// 新建写入器。
    pub fn new(store: &'a FontStore) -> Self {
        Self {
            store,
            fonts: BTreeMap::new(),
            font_slots: BTreeMap::new(),
            page_streams: BTreeMap::new(),
            page_ids: BTreeSet::new(),
            font_bytes: 0,
            glyph_written: 0,
        }
    }

    /// 为某字体登记一个原 gid，返回内容流里应写的 cid。
    fn cid_for(&mut self, font: u32, gid: u16, text: &str) -> u16 {
        let acc = self.fonts.entry(font).or_default();
        if let Some(&cid) = acc.gid_to_cid.get(&gid) {
            if let Some(slot) = acc.to_unicode.get_mut(&cid) {
                if slot.is_empty() && !text.is_empty() {
                    *slot = text.to_string();
                }
            }
            return cid;
        }
        let cid = acc.cid_to_gid_orig.len() as u16 + 1;
        acc.gid_to_cid.insert(gid, cid);
        acc.cid_to_gid_orig.push(gid);
        acc.to_unicode.insert(cid, text.to_string());
        cid
    }

    /// 把段落写入第 `page`（1 基）页；`page_h` 为页高（用户空间，暂未用于翻转）。
    pub fn write_paragraphs(
        &mut self,
        doc: &mut Document,
        page: u32,
        paras: &[TypesetParagraph],
        page_h: f32,
    ) -> Result<()> {
        let pages = doc.get_pages();
        let count = pages.len() as u32;
        let Some(&page_id) = pages.get(&page) else {
            return Err(WriteError::PageOutOfRange { page, count });
        };
        let _ = page_h;

        let mut bytes: Vec<u8> = Vec::new();
        let mut any = false;

        for para in paras {
            for line in &para.lines {
                if line.glyphs.is_empty() {
                    continue;
                }
                any = true;
                // BDC /Span <</ActualText <FEFF...>>>
                let text: String = line.glyphs.iter().map(|g| g.text.as_str()).collect();
                bytes.extend_from_slice(b"/Span <</ActualText ");
                bytes.extend_from_slice(utf16be_bom(&text).as_slice());
                bytes.extend_from_slice(b">> BDC\n");

                for g in &line.glyphs {
                    let cid = self.cid_for(g.font, g.gid, &g.text);
                    self.glyph_written += 1;
                    let name = self.resource_name(g.font);
                    let [r, gg, b] = para.color.to_rgb8();
                    bytes.push(b'q');
                    bytes.push(b'\n');
                    // 字体：不同字形可能来自不同字体，每字形重设 Tf。
                    let size = g.size;
                    // scale_x != 1 → 把缩放并入 Tm 的 a。
                    let (a, d) = if (g.scale_x - 1.0).abs() > 1e-4 {
                        (size * g.scale_x, size)
                    } else {
                        (size, size)
                    };
                    let tf_size = if (g.scale_x - 1.0).abs() > 1e-4 {
                        1.0
                    } else {
                        size
                    };
                    bytes.extend_from_slice(
                        format!(
                            "BT /{} {} Tf {} {} {} rg {} 0 0 {} {} {} Tm <{:04X}> Tj ET\n",
                            name,
                            fmt_num(tf_size),
                            fmt_num(f32::from(r) / 255.0),
                            fmt_num(f32::from(gg) / 255.0),
                            fmt_num(f32::from(b) / 255.0),
                            fmt_num(a),
                            fmt_num(d),
                            fmt_num(g.x),
                            fmt_num(g.y),
                            cid
                        )
                        .as_bytes(),
                    );
                    bytes.extend_from_slice(b"Q\n");
                }
                bytes.extend_from_slice(b"EMC\n");
            }
        }

        if !any {
            return Ok(());
        }

        // 追加为新的独立流。
        let content_id = doc.add_object(Stream::new(Dictionary::new(), bytes));
        self.page_streams.entry(page).or_default().push(content_id);
        self.page_ids.insert(page_id);
        append_to_contents(doc, page_id, content_id);
        // 字体资源名（页级占位，finalize 时填充对象）。
        self.ensure_font_resources(doc, page_id);
        Ok(())
    }

    /// 资源名：`SPF<font_id>`。
    fn resource_name(&self, font: u32) -> String {
        format!("SPF{font}")
    }

    /// 页 `/Resources/Font` 里为已用字体加键（指向占位对象）。
    fn ensure_font_resources(&mut self, doc: &mut Document, page_id: ObjectId) {
        let used: Vec<u32> = self.fonts.keys().copied().collect();
        for fid in used {
            // 占位对象（finalize 时覆盖）。
            let slot = match self.font_slots.get(&fid) {
                Some(&id) => id,
                None => {
                    let id = doc.add_object(Dictionary::new());
                    self.font_slots.insert(fid, id);
                    id
                }
            };
            let name = self.resource_name(fid);
            // 解析 `/Resources/Font` 的落点（内联或引用）。
            let font_dict_id = doc.get_or_create_resources(page_id).ok().and_then(|res| {
                let rd = res.as_dict_mut().ok()?;
                match rd.get_mut(b"Font") {
                    Ok(Object::Dictionary(d)) => {
                        if d.get(name.as_bytes()).is_err() {
                            d.set(name.as_bytes().to_vec(), Object::Reference(slot));
                        }
                        None
                    }
                    Ok(Object::Reference(r)) => Some(*r),
                    _ => {
                        rd.set("Font", Dictionary::new());
                        if let Ok(Object::Dictionary(d)) = rd.get_mut(b"Font") {
                            d.set(name.as_bytes().to_vec(), Object::Reference(slot));
                        }
                        None
                    }
                }
            });
            if let Some(fdid) = font_dict_id {
                if let Ok(d) = doc.get_object_mut(fdid).and_then(Object::as_dict_mut) {
                    if d.get(name.as_bytes()).is_err() {
                        d.set(name.as_bytes().to_vec(), Object::Reference(slot));
                    }
                }
            }
        }
    }

    /// 子集化并嵌入所有用过的字体，填充占位对象。
    pub fn finalize(mut self, doc: &mut Document) -> Result<FontStats> {
        let mut stats = FontStats::default();
        for (fid, acc) in &self.fonts {
            let Some(font) = self.store.get(FontId(*fid)) else {
                return Err(WriteError::UnknownFont(*fid));
            };
            let orig_gids: Vec<u16> = acc.cid_to_gid_orig.clone();
            let tu: Vec<(u16, String)> = acc
                .to_unicode
                .iter()
                .map(|(c, t)| (*c, t.clone()))
                .collect();
            let em = embed_font_lopdf(doc, font, &orig_gids, &tu, &self.resource_name(*fid), None)?;
            // 把真正的 Type0 字典搬进占位槽，槽本身即字体对象。
            // （内容流的 Tf 资源名不变，页资源引用也不变。）
            let slot = self.font_slots[fid];
            if slot != em.font_dict {
                if let Ok(obj) = doc.get_object(em.font_dict).cloned() {
                    doc.set_object(slot, obj);
                    doc.objects.remove(&em.font_dict);
                }
            }
            stats.fonts += 1;
            stats.bytes += font.data.len() as u64;
        }
        stats.glyphs = self.glyph_written;
        self.font_bytes = stats.bytes;
        Ok(stats)
    }
}

/// 把新流追加到页 `/Contents`（单引用 → 数组）。
fn append_to_contents(doc: &mut Document, page_id: ObjectId, content_id: ObjectId) {
    let Ok(page) = doc.get_object_mut(page_id).and_then(Object::as_dict_mut) else {
        return;
    };
    match page.get_mut(b"Contents") {
        Ok(Object::Array(a)) => a.push(Object::Reference(content_id)),
        Ok(o) => {
            let existing = o.clone();
            if matches!(existing, Object::Reference(_)) {
                *o = Object::Array(vec![existing, Object::Reference(content_id)]);
            } else if matches!(existing, Object::Null) {
                *o = Object::Reference(content_id);
            } else {
                *o = Object::Array(vec![Object::Reference(content_id)]);
            }
        }
        Err(_) => {
            page.set("Contents", Object::Reference(content_id));
        }
    }
}

/// 浮点最简写法"" —— 整数直出，保留 4 位小数。
fn fmt_num(v: f32) -> String {
    if !v.is_finite() {
        return "0".to_string();
    }
    if (v - v.round()).abs() < 1e-6 {
        return format!("{}", v.round() as i64);
    }
    let s = format!("{v:.4}");
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}

/// UTF-16BE 带 BOM 的十六进制字符串形式：`<FEFF....>`。
fn utf16be_bom(text: &str) -> Vec<u8> {
    let mut hex = String::from("FEFF");
    for u in text.encode_utf16() {
        hex.push_str(&format!("{u:04X}"));
    }
    let mut out = Vec::with_capacity(hex.len() + 2);
    out.push(b'<');
    out.extend_from_slice(hex.as_bytes());
    out.push(b'>');
    out
}

/// 保存文档：先压缩再写临时文件后改名（避免半成品）。
pub fn save(doc: &mut Document, path: &Path) -> Result<()> {
    doc.compress();
    // 先写同目录临时文件再 rename，避免半截文件。
    let mut tmp = path.to_path_buf();
    let tmp_name = format!(
        ".{}.tmp",
        path.file_name()
            .map_or_else(|| "out.pdf".into(), |n| n.to_string_lossy().into_owned())
    );
    tmp.set_file_name(tmp_name);
    doc.save(&tmp)
        .map_err(|e| WriteError::Lopdf(lopdf::Error::IO(e)))?;
    std::fs::rename(&tmp, path).map_err(|e| WriteError::Lopdf(lopdf::Error::IO(e)))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use lopdf::Stream;
    use syncpdf_core::ir::{LineBox, PlacedGlyph};
    use syncpdf_core::{Color, ParagraphId, Rect, StyleId};

    fn store() -> Option<FontStore> {
        let dir = syncpdf_core::fixtures::fonts_dir()?;
        FontStore::load_builtin(&dir).ok()
    }

    fn doc_with_page() -> (Document, ObjectId) {
        let mut doc = Document::with_version("1.7");
        let pages_id = doc.new_object_id();
        let mut page = Dictionary::new();
        page.set("Type", Object::Name(b"Page".to_vec()));
        page.set("Parent", Object::Reference(pages_id));
        page.set("MediaBox", vec![0.into(), 0.into(), 595.into(), 842.into()]);
        page.set("Resources", Dictionary::new());
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
        (doc, page_id)
    }

    fn para(glyphs: Vec<PlacedGlyph>) -> TypesetParagraph {
        TypesetParagraph {
            id: "P01-001".parse::<ParagraphId>().unwrap(),
            lines: vec![LineBox {
                bbox: Rect::new(0.0, 0.0, 100.0, 20.0),
                baseline_y: 10.0,
                glyphs,
                kept_atoms: Vec::new(),
            }],
            font_scale: 1.0,
            line_height: 20.0,
            color: Color::BLACK,
            used_bbox: Rect::new(0.0, 0.0, 100.0, 20.0),
            overflow: false,
        }
    }

    #[test]
    fn utf16be_bom_shape() {
        let b = utf16be_bom("A中");
        assert_eq!(b, b"<FEFF00414E2D>".to_vec());
        assert_eq!(utf16be_bom(""), b"<FEFF>".to_vec());
    }

    #[test]
    fn fmt_num_trims() {
        assert_eq!(fmt_num(12.0), "12");
        assert_eq!(fmt_num(12.5), "12.5");
        assert_eq!(fmt_num(0.0), "0");
        assert_eq!(fmt_num(f32::NAN), "0");
    }

    #[test]
    fn writer_appends_stream_with_markers() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let fid = s.find(&syncpdf_font::FontQuery {
            family: Some("Inter".into()),
            ..Default::default()
        });
        let Some(fid) = fid else {
            eprintln!("SKIP: no Inter");
            return;
        };
        let f = s.get(fid).unwrap();
        let shaped = syncpdf_font::shape(f, "Hi", 12.0, false, &[]);
        let glyphs: Vec<PlacedGlyph> = shaped
            .iter()
            .enumerate()
            .map(|(i, g)| PlacedGlyph {
                font: fid.0,
                gid: g.gid,
                text: "H".to_string(),
                x: 50.0 + i as f32 * 10.0,
                y: 700.0,
                size: 12.0,
                scale_x: 1.0,
                style: StyleId(1),
            })
            .collect();

        let (mut doc, page_id) = doc_with_page();
        let mut w = Writer::new(&s);
        w.write_paragraphs(&mut doc, 1, &[para(glyphs)], 842.0)
            .unwrap();
        let stats = w.finalize(&mut doc).unwrap();
        assert_eq!(stats.fonts, 1);
        assert_eq!(stats.glyphs, 2);

        // 页 /Contents 现在应有内容（单引用或数组）。
        let page = doc.get_object(page_id).unwrap().as_dict().unwrap();
        let content = page.get(b"Contents").unwrap();
        let streams: Vec<u8> = match content {
            Object::Array(a) => {
                let id = a[0].as_reference().unwrap();
                doc.get_object(id)
                    .unwrap()
                    .as_stream()
                    .unwrap()
                    .content
                    .clone()
            }
            Object::Reference(id) => doc
                .get_object(*id)
                .unwrap()
                .as_stream()
                .unwrap()
                .content
                .clone(),
            _ => panic!("no contents"),
        };
        let text = String::from_utf8_lossy(&streams);
        assert!(text.contains("BDC"), "{text}");
        assert!(text.contains("EMC"), "{text}");
        assert!(text.contains("Tj"), "{text}");
        assert!(text.contains("/Span"), "{text}");
        assert!(text.contains("ActualText"), "{text}");
        assert!(text.contains("SPF"), "{text}");

        // 字体资源已建立。
        let res = page.get(b"Resources").unwrap().as_dict().unwrap();
        let fd = res.get(b"Font").unwrap().as_dict().unwrap();
        assert!(fd.get(format!("SPF{}", fid.0).as_bytes()).is_ok());
    }

    #[test]
    fn save_roundtrip_and_qpdf() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let Some(fid) = s.find(&syncpdf_font::FontQuery {
            family: Some("Inter".into()),
            ..Default::default()
        }) else {
            eprintln!("SKIP: no Inter");
            return;
        };
        let f = s.get(fid).unwrap();
        let shaped = syncpdf_font::shape(f, "AB", 12.0, false, &[]);
        let glyphs: Vec<PlacedGlyph> = shaped
            .iter()
            .enumerate()
            .map(|(i, g)| PlacedGlyph {
                font: fid.0,
                gid: g.gid,
                text: if i == 0 { "A" } else { "B" }.to_string(),
                x: 50.0 + i as f32 * 8.0,
                y: 700.0,
                size: 12.0,
                scale_x: 1.0,
                style: StyleId(1),
            })
            .collect();
        let (mut doc, _page) = doc_with_page();
        let mut w = Writer::new(&s);
        w.write_paragraphs(&mut doc, 1, &[para(glyphs)], 842.0)
            .unwrap();
        w.finalize(&mut doc).unwrap();
        let tmp = tempfile::tempdir().unwrap();
        let out = tmp.path().join("o.pdf");
        save(&mut doc, &out).unwrap();

        let back = Document::load(&out).unwrap();
        assert_eq!(back.get_pages().len(), 1);

        let q = Path::new("/opt/homebrew/bin/qpdf");
        if q.is_file() {
            let r = std::process::Command::new(q)
                .arg("--check")
                .arg(&out)
                .output()
                .unwrap();
            assert!(
                r.status.success(),
                "qpdf: {}{}",
                String::from_utf8_lossy(&r.stdout),
                String::from_utf8_lossy(&r.stderr)
            );
        } else {
            eprintln!("SKIP qpdf");
        }
    }

    #[test]
    fn empty_paragraphs_noop() {
        let Some(s) = store() else {
            eprintln!("SKIP");
            return;
        };
        let (mut doc, page_id) = doc_with_page();
        let mut w = Writer::new(&s);
        w.write_paragraphs(&mut doc, 1, &[], 842.0).unwrap();
        let stats = w.finalize(&mut doc).unwrap();
        assert_eq!(stats.glyphs, 0);
        let page = doc.get_object(page_id).unwrap().as_dict().unwrap();
        assert!(page.get(b"Contents").is_err(), "不应新建 Contents");
    }

    #[test]
    fn unknown_font_errors() {
        let Some(s) = store() else {
            eprintln!("SKIP");
            return;
        };
        let (mut doc, _page) = doc_with_page();
        let mut w = Writer::new(&s);
        let p = para(vec![PlacedGlyph {
            font: 9999,
            gid: 5,
            text: "x".into(),
            x: 0.0,
            y: 0.0,
            size: 10.0,
            scale_x: 1.0,
            style: StyleId(1),
        }]);
        w.write_paragraphs(&mut doc, 1, &[p], 842.0).unwrap();
        assert!(matches!(
            w.finalize(&mut doc),
            Err(WriteError::UnknownFont(9999))
        ));
    }

    #[test]
    fn append_to_contents_promotes_reference_to_array() {
        let mut doc = Document::new();
        let old = doc.add_object(Stream::new(Dictionary::new(), b"old".to_vec()));
        let new = doc.add_object(Stream::new(Dictionary::new(), b"new".to_vec()));
        let mut page = Dictionary::new();
        page.set("Contents", Object::Reference(old));
        let page_id = doc.add_object(page);
        append_to_contents(&mut doc, page_id, new);
        let p = doc.get_object(page_id).unwrap().as_dict().unwrap();
        match p.get(b"Contents").unwrap() {
            Object::Array(a) => {
                assert_eq!(a.len(), 2);
                assert_eq!(a[1], Object::Reference(new));
            }
            _ => panic!("应为数组"),
        }
    }
}
