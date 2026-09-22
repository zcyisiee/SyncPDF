//! lopdf 版字体嵌入（M1-11 的支持模块）。
//!
//! 与 `syncpdf-font::embed`（pdf-writer 版）结构一致，但直接向 [`lopdf::Document`]
//! 里插对象，便于与已加载的文档合并：
//!
//! ```text
//! /Type0 Font ─ /Encoding → 自定义 CMap（codespace <0000>-<FFFF>，cidchar code→cid）
//!              ├─ /DescendantFonts [ CIDFontType2 ]
//!              │     ├─ /CIDToGIDMap（默认 /Identity；writer 用序号映射时给流）
//!              │     ├─ /DW 1000 + /W（advance × 1000 / upem）
//!              │     └─ /FontDescriptor（Flags/ItalicAngle/Ascent/Descent/
//!              │         CapHeight/StemV/FontBBox + FontFile2|3）
//!              └─ /ToUnicode → bfchar CMap
//! ```
//!
//! # cid 约定
//!
//! `to_unicode` 的键是**内容流里实际写的 cid**。当 `cid_to_gid` 为 `None` 时
//! cid = 子集内新 gid（`/CIDToGIDMap /Identity`）；当给出 `Some(map)` 时，
//! cid 是 writer 登记的序号，`map[cid]` 是子集内新 gid，`/CIDToGIDMap` 写成
//! 显式流（2 字节大端每 cid 一项）。这样内容流一经写入无需在子集化后改写。

use std::collections::BTreeMap;

use lopdf::{Dictionary, Document, Object, ObjectId, Stream};
use syncpdf_font::{metrics, subset, LoadedFont};

/// 嵌入产物。
#[derive(Debug, Clone)]
pub struct EmbeddedFontObj {
    /// Type0 字体字典的对象 id。
    pub font_dict: ObjectId,
    /// 页 `/Resources/Font` 里的资源名。
    pub resource_name: String,
    /// 原字体 gid → 子集内新 gid。
    pub gid_map: BTreeMap<u16, u16>,
}

/// 嵌入错误。
#[derive(Debug, thiserror::Error)]
pub enum EmbedError {
    /// 字体侧错误。
    #[error("font: {0}")]
    Font(#[from] syncpdf_font::FontError),
    /// lopdf 侧错误。
    #[error("lopdf: {0}")]
    Lopdf(#[from] lopdf::Error),
    /// 字体缺 head 表等无法嵌入。
    #[error("font {0} unusable: {1}")]
    Unusable(String, String),
}

/// 结果别名。
pub type Result<T, E = EmbedError> = std::result::Result<T, E>;

/// 把 `font` 的子集（含 `gids`）作为一个 Type0 字体对象组写进 `doc`。
///
/// - `gids`：**原字体 gid** 集合（`shape` 的输出即可）。
/// - `to_unicode`：**cid → Unicode 文本**（键语义见模块注释）。
/// - `cid_to_gid`：`None` → `/CIDToGIDMap /Identity`；`Some` → 显式映射流
///   （下标为 cid，值为子集内新 gid）。
pub fn embed_font_lopdf(
    doc: &mut Document,
    font: &LoadedFont,
    gids: &[u16],
    to_unicode: &[(u16, String)],
    resource_name: &str,
    cid_to_gid: Option<&[u16]>,
) -> Result<EmbeddedFontObj> {
    let m = metrics(font);
    if m.upem == 0 {
        return Err(EmbedError::Unusable(
            font.family.clone(),
            "no valid head table".to_string(),
        ));
    }

    // 排序去重的原 gid；`.notdef` 必须在内（subset 会补，但这里显式带上以便映射查找）。
    let mut sorted: Vec<u16> = gids.to_vec();
    sorted.push(0);
    sorted.sort_unstable();
    sorted.dedup();

    let sub = subset(font, &sorted)?;

    // 子集内新 gid 升序表（CMap / W 按它生成）。
    let new_gids: Vec<u16> = sub.gid_map.values().copied().collect();
    // 新 gid → 原 gid 反查（取宽度用）。
    let new_to_old: BTreeMap<u16, u16> = sub.gid_map.iter().map(|(o, n)| (*n, *o)).collect();

    let base_font = postscript_name(font);
    let widths = glyph_widths_1000(font);

    // ---- 1) FontFile2 / FontFile3 ----
    let file_key: &[u8] = if m.is_cff { b"FontFile3" } else { b"FontFile2" };
    let mut ff_dict = Dictionary::new();
    ff_dict.set("Length1", sub.data.len() as i64);
    if m.is_cff {
        ff_dict.set("Subtype", Object::Name(b"OpenType".to_vec()));
    }
    let font_file = doc.add_object(Stream::new(ff_dict, sub.data.clone()));

    // ---- 2) FontDescriptor ----
    let s1000 = 1000.0f32;
    let mut fd = Dictionary::new();
    fd.set("Type", Object::Name(b"FontDescriptor".to_vec()));
    fd.set("FontName", Object::Name(base_font.clone().into_bytes()));
    let mut flags: i64 = 4; // Symbolic
    if font.italic {
        flags |= 64;
    }
    fd.set("Flags", flags);
    fd.set("ItalicAngle", round1(m.italic_angle));
    fd.set("Ascent", round1(m.ascent * s1000));
    fd.set("Descent", round1(m.descent * s1000));
    fd.set("CapHeight", round1(m.cap_height * s1000));
    fd.set("XHeight", round1(m.x_height * s1000));
    fd.set("StemV", round1(stem_v(font.weight)));
    fd.set(
        "FontBBox",
        vec![
            Object::Real(round1(m.bbox[0] * s1000)),
            Object::Real(round1(m.bbox[1] * s1000)),
            Object::Real(round1(m.bbox[2] * s1000)),
            Object::Real(round1(m.bbox[3] * s1000)),
        ],
    );
    fd.set(file_key.to_vec(), Object::Reference(font_file));
    let descriptor = doc.add_object(fd);

    // ---- 3) CIDFontType2 ----
    let mut cid = Dictionary::new();
    cid.set("Type", Object::Name(b"Font".to_vec()));
    cid.set("Subtype", Object::Name(b"CIDFontType2".to_vec()));
    cid.set("BaseFont", Object::Name(base_font.clone().into_bytes()));
    let mut csi = Dictionary::new();
    csi.set(
        "Registry",
        Object::String(b"Adobe".to_vec(), lopdf::StringFormat::Literal),
    );
    csi.set(
        "Ordering",
        Object::String(b"Identity".to_vec(), lopdf::StringFormat::Literal),
    );
    csi.set("Supplement", 0);
    cid.set("CIDSystemInfo", csi);
    cid.set("FontDescriptor", Object::Reference(descriptor));
    cid.set("DW", 1000);
    // /W：按子集内新 gid 写宽度。
    cid.set("W", build_w_array(&new_gids, &new_to_old, &widths));
    match cid_to_gid {
        None => {
            cid.set("CIDToGIDMap", Object::Name(b"Identity".to_vec()));
        }
        Some(map) => {
            let mut bytes = Vec::with_capacity(map.len() * 2);
            for g in map {
                bytes.extend_from_slice(&g.to_be_bytes());
            }
            let map_id = doc.add_object(Stream::new(Dictionary::new(), bytes));
            cid.set("CIDToGIDMap", Object::Reference(map_id));
        }
    }
    let cid_id = doc.add_object(cid);

    // ---- 4) 自定义编码 CMap ----
    let cmap_bytes = build_encoding_cmap(&new_gids);
    let mut cmap_dict = Dictionary::new();
    cmap_dict.set("Type", Object::Name(b"CMap".to_vec()));
    cmap_dict.set("CMapName", Object::Name(b"SPFEncoding".to_vec()));
    let mut cmap_si = Dictionary::new();
    cmap_si.set(
        "Registry",
        Object::String(b"Adobe".to_vec(), lopdf::StringFormat::Literal),
    );
    cmap_si.set(
        "Ordering",
        Object::String(b"Identity".to_vec(), lopdf::StringFormat::Literal),
    );
    cmap_si.set("Supplement", 0);
    cmap_dict.set("CIDSystemInfo", cmap_si);
    let cmap_id = doc.add_object(Stream::new(cmap_dict, cmap_bytes));

    // ---- 5) ToUnicode ----
    let to_uni_bytes = build_to_unicode(to_unicode);
    let tu_id = doc.add_object(Stream::new(Dictionary::new(), to_uni_bytes));

    // ---- 6) Type0 ----
    let mut t0 = Dictionary::new();
    t0.set("Type", Object::Name(b"Font".to_vec()));
    t0.set("Subtype", Object::Name(b"Type0".to_vec()));
    t0.set("BaseFont", Object::Name(base_font.into_bytes()));
    t0.set("Encoding", Object::Reference(cmap_id));
    t0.set("DescendantFonts", vec![Object::Reference(cid_id)]);
    t0.set("ToUnicode", Object::Reference(tu_id));
    let font_obj = doc.add_object(t0);

    Ok(EmbeddedFontObj {
        font_dict: font_obj,
        resource_name: resource_name.to_string(),
        gid_map: sub.gid_map,
    })
}

/// 一个间接字体 id 是否为 Type0（供 `writer` 校验）。
pub fn is_type0(doc: &Document, id: ObjectId) -> bool {
    match doc.get_object(id) {
        Ok(Object::Dictionary(d)) => d
            .get(b"Subtype")
            .ok()
            .and_then(|o| o.as_name().ok())
            .map(|n| n == b"Type0")
            .unwrap_or(false),
        _ => false,
    }
}

fn round1(v: f32) -> f32 {
    (v * 10.0).round() / 10.0
}

/// StemV 估计（与 syncpdf-font 的映射一致）。
fn stem_v(weight: u16) -> f32 {
    match weight {
        0..=149 => 24.0,
        150..=249 => 42.0,
        250..=349 => 56.0,
        350..=449 => 72.0,
        450..=549 => 88.0,
        550..=649 => 110.0,
        650..=749 => 136.0,
        750..=849 => 162.0,
        _ => 190.0,
    }
}

/// PostScript 名（name 表 ID 6 → 兜底 family）。
fn postscript_name(font: &LoadedFont) -> String {
    use skrifa::MetadataProvider;
    let name_id = skrifa::raw::types::NameId::POSTSCRIPT_NAME;
    skrifa::FontRef::from_index(&font.data, font.face_index)
        .ok()
        .and_then(|f| {
            f.localized_strings(name_id)
                .english_or_first()
                .map(|s| s.to_string())
        })
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| font.family.replace(' ', "-"))
}

/// 每个原 gid 的 1000 单位 advance（hmtx）。
fn glyph_widths_1000(font: &LoadedFont) -> BTreeMap<u16, f32> {
    use skrifa::MetadataProvider;
    let mut map = BTreeMap::new();
    let Ok(face) = skrifa::FontRef::from_index(&font.data, font.face_index) else {
        return map;
    };
    let gm = face.glyph_metrics(
        skrifa::instance::Size::new(1000.0),
        skrifa::instance::LocationRef::default(),
    );
    for g in 0..gm.glyph_count() {
        if let Some(w) = gm.advance_width(skrifa::GlyphId::from(g)) {
            map.insert(g as u16, w);
        }
    }
    map
}

/// `/W` 数组：连号且同宽的 gid 合并成 `first last w`，否则 `first [w...]`。
fn build_w_array(
    new_gids: &[u16],
    new_to_old: &BTreeMap<u16, u16>,
    widths: &BTreeMap<u16, f32>,
) -> Object {
    let entries: Vec<(u16, f32)> = new_gids
        .iter()
        .map(|&new| {
            let old = new_to_old.get(&new).copied().unwrap_or(0);
            (new, widths.get(&old).copied().unwrap_or(1000.0))
        })
        .collect();

    let mut out: Vec<Object> = Vec::new();
    let mut i = 0usize;
    while i < entries.len() {
        let start = i;
        // 先尝试连续同宽段（>=2 个才值得）。
        let mut j = i;
        while j + 1 < entries.len()
            && entries[j + 1].0 == entries[j].0 + 1
            && (entries[j + 1].1 - entries[j].1).abs() < 1e-3
        {
            j += 1;
        }
        if j > start {
            out.push(Object::Integer(i64::from(entries[start].0)));
            out.push(Object::Integer(i64::from(entries[j].0)));
            out.push(Object::Real(round1(entries[start].1)));
            i = j + 1;
            continue;
        }
        // 否则收集一段连续的（gid 递增）宽度列表。
        let mut k = i;
        let mut ws: Vec<Object> = Vec::new();
        while k < entries.len() {
            if k > i && entries[k].0 != entries[k - 1].0 + 1 {
                break;
            }
            ws.push(Object::Real(round1(entries[k].1)));
            k += 1;
        }
        if ws.len() >= 2 {
            out.push(Object::Integer(i64::from(entries[i].0)));
            out.push(Object::Array(ws));
            i = k;
            continue;
        }
        // 单点：仍用数组形式。
        out.push(Object::Integer(i64::from(entries[i].0)));
        out.push(Object::Array(vec![Object::Real(round1(entries[i].1))]));
        i += 1;
    }
    Object::Array(out)
}

/// 自定义编码 CMap 文本（`code = cid = 子集内新 gid`）。
fn build_encoding_cmap(gids: &[u16]) -> Vec<u8> {
    let mut s = String::new();
    s.push_str("%!PS-Adobe-3.0 Resource-CMap\n");
    s.push_str("%%DocumentNeededResources: procset CIDInit\n");
    s.push_str("%%IncludeResource: procset CIDInit\n");
    s.push_str("%%BeginResource: CMap SPFEncoding\n");
    s.push_str("%%Version: 1\n");
    s.push_str("%%EndComments\n");
    s.push_str("/CIDInit /ProcSet findresource begin\n");
    s.push_str("12 dict begin\n");
    s.push_str("begincmap\n");
    s.push_str("/CIDSystemInfo 3 dict dup begin\n");
    s.push_str("  /Registry (Adobe) def\n");
    s.push_str("  /Ordering (Identity) def\n");
    s.push_str("  /Supplement 0 def\n");
    s.push_str("end def\n");
    s.push_str("/CMapName /SPFEncoding def\n");
    s.push_str("/CMapVersion 1 def\n");
    s.push_str("/CMapType 1 def\n");
    s.push_str("/WMode 0 def\n");
    s.push_str("1 begincodespacerange\n");
    s.push_str("<0000> <FFFF>\n");
    s.push_str("endcodespacerange\n");
    for chunk in gids.chunks(100) {
        s.push_str(&format!("{} begincidchar\n", chunk.len()));
        for &g in chunk {
            s.push_str(&format!("<{g:04X}> {g}\n"));
        }
        s.push_str("endcidchar\n");
    }
    s.push_str("endcmap\n");
    s.push_str("CMapName currentdict /CMap defineresource pop\n");
    s.push_str("end\n");
    s.push_str("end\n");
    s.push_str("%%EndResource\n");
    s.push_str("%%EOF");
    s.into_bytes()
}

/// ToUnicode CMap 文本（bfchar，`cid → UTF-16BE`）。
fn build_to_unicode(mapping: &[(u16, String)]) -> Vec<u8> {
    let mut s = String::new();
    s.push_str("/CIDInit /ProcSet findresource begin\n");
    s.push_str("12 dict begin\n");
    s.push_str("begincmap\n");
    s.push_str("/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n");
    s.push_str("/CMapName /SPFToUnicode def\n");
    s.push_str("/CMapType 2 def\n");
    s.push_str("1 begincodespacerange\n");
    s.push_str("<0000> <FFFF>\n");
    s.push_str("endcodespacerange\n");
    for chunk in mapping.chunks(100) {
        s.push_str(&format!("{} beginbfchar\n", chunk.len()));
        for (cid, text) in chunk {
            s.push_str(&format!("<{cid:04X}> <{}>\n", utf16be_hex(text)));
        }
        s.push_str("endbfchar\n");
    }
    s.push_str("endcmap\n");
    s.push_str("CMapName currentdict /CMap defineresource pop\n");
    s.push_str("end\n");
    s.push_str("end\n");
    s.into_bytes()
}

/// 文本 → UTF-16BE 十六进制（BMP 外字符用代理对）。
fn utf16be_hex(text: &str) -> String {
    let mut out = String::new();
    for u in text.encode_utf16() {
        out.push_str(&format!("{u:04X}"));
    }
    if out.is_empty() {
        // 空文本写一个 0000，避免 `< >` 空串。
        out.push_str("0000");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_font::loader::Script;
    use syncpdf_font::{FontQuery, FontStore};

    fn store() -> Option<FontStore> {
        let dir = syncpdf_core::fixtures::fonts_dir()?;
        FontStore::load_builtin(&dir).ok()
    }

    fn simple_doc() -> (Document, ObjectId) {
        let mut doc = Document::with_version("1.7");
        let pages_id = doc.new_object_id();
        let mut page = Dictionary::new();
        page.set("Type", Object::Name(b"Page".to_vec()));
        page.set("Parent", Object::Reference(pages_id));
        page.set("MediaBox", vec![0.into(), 0.into(), 595.into(), 842.into()]);
        page.set("Contents", Object::Null);
        page.set("Resources", Dictionary::new());
        let page_id = doc.add_object(page);
        let mut pages = Dictionary::new();
        pages.set("Type", Object::Name(b"Pages".to_vec()));
        pages.set("Kids", vec![Object::Reference(page_id)]);
        pages.set("Count", 1);
        doc.objects.insert(pages_id, Object::Dictionary(pages));
        let cat = doc.add_object(Dictionary::new());
        doc.get_object_mut(cat)
            .unwrap()
            .as_dict_mut()
            .unwrap()
            .set("Pages", Object::Reference(pages_id));
        doc.trailer.set("Root", Object::Reference(cat));
        (doc, page_id)
    }

    #[test]
    fn utf16be_hex_basic() {
        assert_eq!(utf16be_hex("A"), "0041");
        assert_eq!(utf16be_hex("中"), "4E2D");
        assert_eq!(utf16be_hex(""), "0000");
        // BMP 外字符（𝄞 U+1D11E）→ 代理对 D834 DD1E。
        assert_eq!(utf16be_hex("\u{1D11E}"), "D834DD1E");
    }

    #[test]
    fn encoding_cmap_shape() {
        let cmap = build_encoding_cmap(&[1, 2, 3]);
        let s = String::from_utf8_lossy(&cmap);
        assert!(s.starts_with("%!PS-Adobe-3.0 Resource-CMap"));
        assert!(s.contains("3 begincidchar"));
        assert!(s.contains("<0001> 1"));
        assert!(s.ends_with("%%EOF"));
    }

    #[test]
    fn to_unicode_shape() {
        let tu = build_to_unicode(&[(1, "中".into()), (2, "A".into())]);
        let s = String::from_utf8_lossy(&tu);
        assert!(s.contains("2 beginbfchar"));
        assert!(s.contains("<0001> <4E2D>"));
        assert!(s.contains("<0002> <0041>"));
    }

    #[test]
    fn w_array_consecutive_same_width() {
        let mut w = BTreeMap::new();
        w.insert(10u16, 500.0);
        w.insert(11u16, 500.0);
        let mut n2o = BTreeMap::new();
        n2o.insert(1u16, 10u16);
        n2o.insert(2u16, 11u16);
        let obj = build_w_array(&[1, 2], &n2o, &w);
        match obj {
            Object::Array(a) => {
                // [1 2 500]
                assert_eq!(a.len(), 3);
                assert_eq!(a[0], Object::Integer(1));
                assert_eq!(a[1], Object::Integer(2));
            }
            _ => panic!("expect array"),
        }
    }

    #[test]
    fn w_array_singleton_uses_list_form() {
        let mut w = BTreeMap::new();
        w.insert(10u16, 500.0);
        let mut n2o = BTreeMap::new();
        n2o.insert(5u16, 10u16);
        let obj = build_w_array(&[5], &n2o, &w);
        match obj {
            Object::Array(a) => {
                assert_eq!(a.len(), 2);
                assert_eq!(a[0], Object::Integer(5));
                assert!(matches!(a[1], Object::Array(_)));
            }
            _ => panic!("expect array"),
        }
    }

    #[test]
    fn embed_saves_and_reopens() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s.find(&FontQuery {
            family: Some("Inter".into()),
            ..Default::default()
        });
        let id = match id.or_else(|| {
            s.find(&FontQuery {
                script: Script::HanSC,
                ..Default::default()
            })
        }) {
            Some(id) => id,
            None => {
                eprintln!("SKIP: no suitable font");
                return;
            }
        };
        let f = s.get(id).unwrap();
        let glyphs = syncpdf_font::shape(f, "ABC", 12.0, false, &[]);
        let gids: Vec<u16> = glyphs.iter().map(|g| g.gid).collect();

        let (mut doc, _page) = simple_doc();
        let em = embed_font_lopdf(&mut doc, f, &gids, &[(1, "A".into())], "SPF0", None).unwrap();
        assert_eq!(em.resource_name, "SPF0");
        assert!(em.gid_map.contains_key(&gids[0]));

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("out.pdf");
        doc.save(&path).unwrap();

        let back = Document::load(&path).unwrap();
        let d = back.get_object(em.font_dict).unwrap().as_dict().unwrap();
        assert_eq!(d.get(b"Subtype").unwrap().as_name().unwrap(), b"Type0");
        assert!(d.get(b"ToUnicode").is_ok());
        assert!(d.get(b"Encoding").is_ok());
        let desc = d.get(b"DescendantFonts").unwrap().as_array().unwrap();
        let cid_id = desc[0].as_reference().unwrap();
        let cid = back.get_object(cid_id).unwrap().as_dict().unwrap();
        assert_eq!(
            cid.get(b"Subtype").unwrap().as_name().unwrap(),
            b"CIDFontType2"
        );
        assert!(cid.get(b"W").is_ok());
        let fd_id = cid.get(b"FontDescriptor").unwrap().as_reference().unwrap();
        let fd = back.get_object(fd_id).unwrap().as_dict().unwrap();
        let ff_id = fd
            .get(b"FontFile2")
            .or_else(|_| fd.get(b"FontFile3"))
            .unwrap()
            .as_reference()
            .unwrap();
        let ff = back.get_object(ff_id).unwrap().as_stream().unwrap();
        assert!(!ff.content.is_empty(), "FontFile 长度应 > 0");
    }

    #[test]
    fn embed_with_cid_map_stream() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = match s.find(&FontQuery {
            family: Some("Inter".into()),
            ..Default::default()
        }) {
            Some(id) => id,
            None => {
                eprintln!("SKIP: no Inter");
                return;
            }
        };
        let f = s.get(id).unwrap();
        let glyphs = syncpdf_font::shape(f, "AB", 12.0, false, &[]);
        let gids: Vec<u16> = glyphs.iter().map(|g| g.gid).collect();
        let mut doc2 = Document::new();
        let (mut doc, _page) = simple_doc();
        let _ = &mut doc2;
        let em = embed_font_lopdf(
            &mut doc,
            f,
            &gids,
            &[(1, "A".into()), (2, "B".into())],
            "SPF1",
            Some(&[0, 1, 2]),
        )
        .unwrap();
        let d = doc.get_object(em.font_dict).unwrap().as_dict().unwrap();
        let cid_id = d.get(b"DescendantFonts").unwrap().as_array().unwrap()[0]
            .as_reference()
            .unwrap();
        let cid = doc.get_object(cid_id).unwrap().as_dict().unwrap();
        let map_id = cid.get(b"CIDToGIDMap").unwrap().as_reference().unwrap();
        let map = doc.get_object(map_id).unwrap().as_stream().unwrap();
        assert_eq!(map.content.len(), 6, "3 个 cid × 2 字节");
    }
}
