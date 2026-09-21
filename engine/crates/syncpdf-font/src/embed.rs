//! PDF 字体嵌入（pdf-writer 0.15）。
//!
//! hjfy/设计文档配方（02-技术路径与架构.md §7、research/02-hjfy §3.4）：
//!
//! ```text
//! /Type0 Font ─ /Encoding → 自定义 CMap（codespace <0000>-<FFFF>，
//!              │   cidchar code→cid，code = cid = 子集内新 gid）
//!              ├─ /DescendantFonts [ CIDFontType2 ]
//!              │     ├─ /CIDToGIDMap /Identity（cid = gid 恒成立）
//!              │     ├─ /DW 1000 + /W（advance × 1000 / upem）
//!              │     └─ /FontDescriptor（Flags/ItalicAngle/Ascent/
//!              │         Descent/CapHeight/StemV/FontBBox + 字体文件）
//!              └─ /ToUnicode → bfchar CMap
//! ```
//!
//! **gid 语义**（subsetter 0.2.6）：子集化会把 gid 重映射为连续整数
//! （见 [`crate::subset`] 模块注释）。本模块的约定：
//!
//! - 入参 `gids` / `to_unicode` 用 **原字体 gid**（即 `shape` 的输出）；
//! - 内容流 `Tj` 的 code、CMap cidchar、`/W` 索引、ToUnicode 键都用
//!   **子集内新 gid**（`SubsetResult::gid_map` 换算），这样
//!   `/CIDToGIDMap /Identity` + code = cid = 新 gid 恒成立。
//!
//! 有 `CFF ` 表时字体文件用 `/FontFile3 /Subtype /OpenType`，否则
//! `/FontFile2`。subsetter 会删除 cmap 表，PDF 侧靠自定义 CMap 寻址。

use std::collections::HashMap;

use pdf_writer::types::{CidFontType, FontFlags, SystemInfo, UnicodeCmap};
use pdf_writer::{Finish, Name, Pdf, Rect, Ref, Str};

use crate::loader::LoadedFont;
use crate::{FontError, Result};

/// 嵌入产物：字体对象组入口 + 资源名 + gid 重映射表。
#[derive(Debug, Clone)]
pub struct EmbeddedFont {
    /// Type0 字体字典的间接引用。
    pub font_dict: Ref,
    /// 页资源 /Font 字典键（调用方传入，如 "F1"/"SPF0"）。
    pub resource_name: String,
    /// 原字体 gid → 子集内新 gid（= PDF code/cid）。生成内容流时用它把
    /// `shape` 输出的原 gid 换算为 Tj 字节（配合 [`encode_gids`]）。
    pub gid_map: std::collections::BTreeMap<u16, u16>,
}

/// 把 2 字节大端 code（= gid）拼成 Tj 用的字节串。
///
/// 每个 gid 2 字节，大端；供 `<hex> Tj` 或字符串 Tj。
/// 注意：传入的应是**子集内新 gid**（见 [`embed_font`] 的 gid 约定）。
pub fn encode_gids(gids: &[u16]) -> Vec<u8> {
    let mut out = Vec::with_capacity(gids.len() * 2);
    for g in gids {
        out.extend_from_slice(&g.to_be_bytes());
    }
    out
}

/// 生成字体对象组（Type0 + CIDFontType2 + CMap + ToUnicode + 描述符 + 字体文件）。
///
/// - `alloc`：分配间接对象引用的回调（每调用一次返回一个未用 `Ref`），
///   共调用 6 次。
/// - `gids`：本 face 用到的全部 **原字体 gid**（`shape` 的输出即可，
///   内部排序去重并经 subsetter 重映射）。
/// - `to_unicode`：**原字体 gid** → Unicode 文本（多码点字符串也支持，
///   UTF-16BE 编码）。
/// - `resource_name`：返回值原样带回。
///
/// 返回的 [`EmbeddedFont::gid_map`] 供内容流编码用：
/// `encode_gids(&orig_gids.iter().map(|g| em.gid_map[g])…)`。
pub fn embed_font(
    pdf: &mut Pdf,
    alloc: &mut impl FnMut() -> Ref,
    font: &LoadedFont,
    gids: &[u16],
    to_unicode: &[(u16, String)],
    resource_name: &str,
) -> Result<EmbeddedFont> {
    let m = crate::metrics::metrics(font);
    if m.upem == 0 {
        return Err(FontError::Embed("font has no valid head table".into()));
    }
    let face = skrifa::FontRef::from_index(&font.data, font.face_index)
        .map_err(|e| FontError::Parse(format!("{:?}: {:?}", font.path, e)))?;

    // 排序去重的原 gid 集。
    let mut sorted_gids: Vec<u16> = gids.to_vec();
    sorted_gids.sort_unstable();
    sorted_gids.dedup();

    // ---- 子集（先做，拿到 gid_map 供 CMap/W/ToUnicode 用）----
    let subset = crate::subset::subset(font, &sorted_gids)?;
    // 新 gid 升序表（cidchar / W 按它生成）。
    let new_gids: Vec<u16> = subset.gid_map.values().copied().collect();

    let type0 = alloc();
    let cid_font = alloc();
    let cmap = alloc();
    let to_unicode_ref = alloc();
    let descriptor = alloc();
    let font_file = alloc();

    // ---- /BaseFont：PostScript 名（name 表 ID 6），失败用 family。----
    let base_font = postscript_name(&face, &font.family);

    // ---- Type0 ----
    let mut type0_ref = pdf.type0_font(type0);
    type0_ref.base_font(Name(base_font.as_bytes()));
    type0_ref.encoding_cmap(cmap);
    type0_ref.descendant_font(cid_font);
    type0_ref.to_unicode(to_unicode_ref);
    type0_ref.finish();

    // ---- CIDFontType2 ----
    let mut cid = pdf.cid_font(cid_font);
    cid.subtype(CidFontType::Type2);
    cid.base_font(Name(base_font.as_bytes()));
    cid.system_info(SystemInfo {
        registry: Str(b"Adobe"),
        ordering: Str(b"Identity"),
        supplement: 0,
    });
    cid.font_descriptor(descriptor);
    cid.default_width(1000.0);
    cid.cid_to_gid_map_predefined(Name(b"Identity"));

    // /W：逐新 gid 宽度（consecutive 形式：[gid [w1 w2 ...]]）。
    {
        let mut widths = cid.widths();
        let gm = glyph_metrics_1000(&face);
        let entries: Vec<(u16, f32)> = new_gids
            .iter()
            .map(|&new_gid| {
                // 新 gid ↔ 原 gid 反查。
                let old = subset
                    .gid_map
                    .iter()
                    .find_map(|(o, n)| (*n == new_gid).then_some(*o))
                    .unwrap_or(0);
                let w = gm.get(&old).copied().unwrap_or(1000.0);
                (new_gid, w)
            })
            .collect();
        write_widths(&mut widths, &entries);
    }
    cid.finish();

    // ---- 自定义编码 CMap（/SPFEncoding）----
    let cmap_bytes = build_encoding_cmap(&new_gids);
    let mut cmap_writer = pdf.cmap(cmap, &cmap_bytes);
    cmap_writer.name(Name(b"SPFEncoding"));
    cmap_writer.system_info(SystemInfo {
        registry: Str(b"Adobe"),
        ordering: Str(b"Identity"),
        supplement: 0,
    });
    cmap_writer.finish();

    // ---- ToUnicode（键 = 新 gid）----
    let old_to_uni: HashMap<u16, &String> = to_unicode.iter().map(|(g, t)| (*g, t)).collect();
    let mut uni_pairs: Vec<(u16, &String)> = Vec::new();
    for (&old, &new) in &subset.gid_map {
        if let Some(t) = old_to_uni.get(&old) {
            uni_pairs.push((new, t));
        }
    }
    let to_uni_bytes = build_to_unicode(&uni_pairs);
    pdf.stream(to_unicode_ref, &to_uni_bytes).finish();

    // ---- FontDescriptor ----
    // Metrics 字段是 upem 归一值（design/upem），PDF 1000 单位 = 归一值 × 1000。
    let scale1000 = 1000.0f32;
    let stem_v = stem_v_estimate(font.weight);
    let mut desc = pdf.font_descriptor(descriptor);
    desc.name(Name(base_font.as_bytes()));
    let mut flags = FontFlags::SYMBOLIC;
    if font.italic {
        flags |= FontFlags::ITALIC;
    }
    desc.flags(flags);
    desc.italic_angle(m.italic_angle);
    desc.ascent((m.ascent * scale1000).round());
    desc.descent((m.descent * scale1000).round());
    desc.cap_height((m.cap_height * scale1000).round());
    desc.x_height((m.x_height * scale1000).round());
    desc.stem_v(stem_v);
    desc.bbox(Rect::new(
        m.bbox[0] * scale1000,
        m.bbox[1] * scale1000,
        m.bbox[2] * scale1000,
        m.bbox[3] * scale1000,
    ));
    // CFF（Inter otf / Noto CJK）→ FontFile3 /OpenType；TrueType → FontFile2。
    if m.is_cff {
        desc.font_file3(font_file);
    } else {
        desc.font_file2(font_file);
    }
    desc.finish();

    // ---- 字体文件（子集后的 SFNT）----
    let mut stream = pdf.stream(font_file, &subset.data);
    if m.is_cff {
        stream.pair(Name(b"Subtype"), Name(b"OpenType"));
    }
    stream.finish();

    Ok(EmbeddedFont {
        font_dict: type0,
        resource_name: resource_name.to_string(),
        gid_map: subset.gid_map,
    })
}

/// /W 数组写入：连续 gid 且同宽的段合并成 [first last w]。
fn write_widths(widths: &mut pdf_writer::writers::Widths<'_>, entries: &[(u16, f32)]) {
    let mut i = 0;
    while i < entries.len() {
        let start = i;
        while i + 1 < entries.len()
            && entries[i + 1].0 == entries[i].0 + 1
            && (entries[i + 1].1 - entries[i].1).abs() < f32::EPSILON
        {
            i += 1;
        }
        // 同宽连续段用 same 形式更紧凑。
        let first = entries[start].0;
        let last = entries[i].0;
        let w = entries[start].1;
        widths.same(first, last, w);
        i += 1;
    }
}

/// 每个原 gid 的 1000 单位宽度（hmtx）。
fn glyph_metrics_1000(face: &skrifa::FontRef<'_>) -> HashMap<u16, f32> {
    use skrifa::MetadataProvider;
    let mut map = HashMap::new();
    let gm = face.glyph_metrics(
        skrifa::instance::Size::new(1000.0),
        skrifa::instance::LocationRef::default(),
    );
    let count = gm.glyph_count();
    for g in 0..count {
        if let Some(w) = gm.advance_width(skrifa::GlyphId::from(g)) {
            map.insert(g as u16, w);
        }
    }
    map
}

/// PostScript 名（name 表 ID 6 → 兜底 family）。
fn postscript_name(face: &skrifa::FontRef<'_>, family: &str) -> String {
    use skrifa::MetadataProvider;
    face.localized_strings(read_fonts::types::NameId::POSTSCRIPT_NAME)
        .english_or_first()
        .map(|s| s.to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| family.replace(' ', "-"))
}

/// StemV 估计（hjfy 同款粗略法：weight 映射）。
fn stem_v_estimate(weight: u16) -> f32 {
    // OS/2 usWeightClass → StemV 常用近似。
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

/// 自定义编码 CMap：codespace <0000>-<FFFF> + cidchar（code=cid=新 gid）。
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
    // codespace：1 段 <0000> <FFFF>。
    s.push_str("1 begincodespacerange\n");
    s.push_str("<0000> <FFFF>\n");
    s.push_str("endcodespacerange\n");
    // cidchar：每 100 行一段（PDF 惯例上限）。
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

/// ToUnicode CMap：bfchar（新 gid → UTF-16BE 文本）。
fn build_to_unicode<S: AsRef<str>>(mapping: &[(u16, S)]) -> Vec<u8> {
    let mut cmap = UnicodeCmap::new(
        Name(b"SPFToUnicode"),
        SystemInfo {
            registry: Str(b"Adobe"),
            ordering: Str(b"UCS"),
            supplement: 0,
        },
    );
    for &(gid, ref text) in mapping {
        let chars: Vec<char> = text.as_ref().chars().collect();
        cmap.pair_with_multiple(gid, chars);
    }
    cmap.finish().into_vec()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::loader::{FontQuery, FontStore, Script};
    use pdf_writer::Content;

    fn store() -> Option<FontStore> {
        let dir = syncpdf_core::fixtures::fonts_dir()?;
        FontStore::load_builtin(&dir).ok()
    }

    /// 生成一页 PDF 并断言结构（qpdf + lopdf）。
    fn check_pdf(bytes: &[u8], resource_name: &str, expect_cff: bool) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("out.pdf");
        std::fs::write(&path, bytes).unwrap();

        // (a) qpdf --check（工具缺失则 skip 该断言）。
        let qpdf = std::path::Path::new("/opt/homebrew/bin/qpdf");
        if qpdf.is_file() {
            let out = std::process::Command::new(qpdf)
                .arg("--check")
                .arg(&path)
                .output()
                .expect("run qpdf");
            assert!(
                out.status.success(),
                "qpdf --check failed:\n{}{}",
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr)
            );
        } else {
            eprintln!("SKIP: qpdf not found at /opt/homebrew/bin/qpdf");
        }

        // (b) lopdf 重开，检查对象结构。
        let doc = lopdf::Document::load(&path).expect("lopdf reopen");
        let pages: Vec<_> = doc.page_iter().collect();
        assert_eq!(pages.len(), 1);
        let fonts = doc.get_page_fonts(pages[0]).expect("page fonts");
        let font_dict = fonts
            .get(resource_name.as_bytes())
            .unwrap_or_else(|| panic!("resource {resource_name} missing"));
        // get_page_fonts 已解引用 → font_dict 就是 Type0 字典。
        let t0d = font_dict;
        assert_eq!(
            t0d.get(b"Type").unwrap(),
            &lopdf::Object::Name(b"Font".to_vec())
        );
        assert_eq!(
            t0d.get(b"Subtype").unwrap(),
            &lopdf::Object::Name(b"Type0".to_vec())
        );

        // DescendantFonts → CIDFontType2。
        let desc = match t0d.get(b"DescendantFonts").unwrap() {
            lopdf::Object::Array(a) => a,
            other => panic!("DescendantFonts not array: {other:?}"),
        };
        assert_eq!(desc.len(), 1);
        let cid_id = match &desc[0] {
            lopdf::Object::Reference(id) => *id,
            other => panic!("not ref: {other:?}"),
        };
        let cid = doc.get_object(cid_id).unwrap();
        let cidd = match cid {
            lopdf::Object::Dictionary(d) => d,
            other => panic!("CIDFont not dict: {other:?}"),
        };
        assert_eq!(
            cidd.get(b"Subtype").unwrap(),
            &lopdf::Object::Name(b"CIDFontType2".to_vec())
        );
        assert_eq!(
            cidd.get(b"CIDToGIDMap").unwrap(),
            &lopdf::Object::Name(b"Identity".to_vec())
        );
        assert!(cidd.get(b"W").is_ok(), "/W 必须存在");
        assert!(cidd.get(b"DW").is_ok(), "/DW 必须存在");

        // CIDSystemInfo。
        let csi = cidd.get(b"CIDSystemInfo").unwrap();
        let _ = csi;

        // ToUnicode。
        assert!(t0d.get(b"ToUnicode").is_ok(), "/ToUnicode 必须存在");
        // Encoding → CMap stream。
        assert!(t0d.get(b"Encoding").is_ok(), "/Encoding 必须存在");

        // FontDescriptor + FontFile2/3。
        let fd_id = match cidd.get(b"FontDescriptor").unwrap() {
            lopdf::Object::Reference(r) => *r,
            other => panic!("FontDescriptor not ref: {other:?}"),
        };
        let fd = doc.get_object(fd_id).unwrap();
        let fdd = match fd {
            lopdf::Object::Dictionary(d) => d,
            other => panic!("FontDescriptor not dict: {other:?}"),
        };
        for key in [
            b"Flags".as_slice(),
            b"ItalicAngle",
            b"Ascent",
            b"Descent",
            b"CapHeight",
            b"StemV",
            b"FontBBox",
        ] {
            assert!(fdd.get(key).is_ok(), "FontDescriptor 缺 {key:?}");
        }
        if expect_cff {
            assert!(fdd.get(b"FontFile3").is_ok(), "CFF 字体应走 FontFile3");
        } else {
            assert!(fdd.get(b"FontFile2").is_ok(), "TrueType 字体应走 FontFile2");
        }
    }

    /// 通用：字体 + 文本 → 单页 PDF → 断言。
    fn embed_and_check(store: &FontStore, id: crate::loader::FontId, text: &str, cff: bool) {
        let f = store.get(id).unwrap();
        let glyphs = crate::shape::shape(f, text, 24.0, false, &[]);
        assert!(!glyphs.is_empty());
        let orig_gids: Vec<u16> = glyphs.iter().map(|g| g.gid).collect();

        // to_unicode：cluster 反查原文（键 = 原 gid）。
        let mut to_uni: Vec<(u16, String)> = Vec::new();
        for g in &glyphs {
            let byte = g.cluster as usize;
            if let Some(ch) = text[byte..].chars().next() {
                to_uni.push((g.gid, ch.to_string()));
            }
        }

        let mut pdf = Pdf::new();
        let catalog_id = Ref::new(1);
        let page_tree_id = Ref::new(2);
        let page_id = Ref::new(3);
        let content_id = Ref::new(4);
        pdf.catalog(catalog_id).pages(page_tree_id);
        pdf.pages(page_tree_id).kids([page_id]).count(1);

        let mut next: i32 = 5;
        let mut alloc = || {
            let r = Ref::new(next);
            next += 1;
            r
        };
        let embedded =
            embed_font(&mut pdf, &mut alloc, f, &orig_gids, &to_uni, "F1").expect("embed");

        // 内容流用子集新 gid（embed 返回的映射表换算）。
        let new_gids: Vec<u16> = orig_gids.iter().map(|g| embedded.gid_map[g]).collect();
        let show_bytes = encode_gids(&new_gids);

        let mut page = pdf.page(page_id);
        page.media_box(Rect::new(0.0, 0.0, 595.0, 842.0));
        page.parent(page_tree_id);
        page.contents(content_id);
        page.resources()
            .fonts()
            .pair(Name(b"F1"), embedded.font_dict);
        page.finish();

        let mut content = Content::new();
        content.begin_text();
        content.set_font(Name(b"F1"), 24.0);
        content.next_line(72.0, 700.0);
        content.show(Str(&show_bytes));
        content.end_text();
        let buf = content.finish();
        pdf.stream(content_id, buf.as_slice());

        let bytes = pdf.finish();
        check_pdf(&bytes, "F1", cff);
    }

    #[test]
    fn embed_chinese() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&FontQuery {
                script: Script::HanSC,
                ..Default::default()
            })
            .unwrap();
        embed_and_check(&s, id, "中文排版测试", true);
    }

    #[test]
    fn embed_english() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&FontQuery {
                family: Some("Inter".into()),
                ..Default::default()
            })
            .unwrap();
        embed_and_check(&s, id, "Hello, World", true);
    }

    #[test]
    fn embed_truetype_uses_font_file2() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&FontQuery {
                family: Some("PT Sans".into()),
                ..Default::default()
            })
            .or_else(|| s.find_by_family("sans", 400, false))
            .expect("PT 字体应存在");
        embed_and_check(&s, id, "PT TrueType hello", false);
    }

    #[test]
    fn encode_gids_be() {
        assert_eq!(encode_gids(&[0x1234, 0x0056]), vec![0x12, 0x34, 0x00, 0x56]);
        assert!(encode_gids(&[]).is_empty());
    }
}
