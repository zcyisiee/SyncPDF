//! op 级补丁：字形级删除与内容流重写（M1-10）。
//!
//! 设计基准：02-技术路径与架构.md §6.3、research/02-hjfy-engine-deep-dive.md §3。
//!
//! # 键与语义
//!
//! 补丁以 [`OpKey`]（流对象 + 操作序号）为键，值为该操作内被删字形的序号集合。
//!
//! - **同一 Op 内全部字形被删**：把该操作改写为等宽 kerning 的 `TJ` 数值
//!   （`-(advance 之和)*1000/size`），这样后续字形位置保持不变；advance 和为 0
//!   时直接删除该操作。
//! - **部分删除**：重建字符串操作数，被删的连续段替换为等宽 kerning 数值。
//!   `Tj` 升级为 `TJ`；`'` / `"` 先拆成 `T*`（或 `Tw Tc`）+ 处理。
//!
//! 孤立的前置 `Td`/`Tm`/`Tf` 一律保留（安全优先，不做无用指令消除）。
//!
//! # Form XObject
//!
//! 被修改的 Form 流不原地改：克隆为新对象，页 `/Resources/XObject` 加
//! `/SPfX<n>` 条目，并把对应那次 `Do` 改名为新键，原对象保持不动
//! （其他页可能共享同一 Form）。

use std::collections::{BTreeMap, BTreeSet};

use lopdf::{Dictionary, Document, Object, ObjectId};
use syncpdf_core::ir::{DisplayItem, Glyph, PageIR};
use syncpdf_core::{GlyphId, ObjRef, OpKey};

use crate::bind::FormDo;
use crate::content::{parse_content, write_content, Op, Operand};

/// 补丁统计。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PatchStats {
    /// 整体删除（或改写为纯 kerning）的操作数。
    pub ops_deleted: u32,
    /// 部分删除后重写字符串的操作数。
    pub ops_rewritten: u32,
    /// 改动的流数。
    pub streams_touched: u32,
    /// 克隆的 Form XObject 数。
    pub forms_cloned: u32,
}

/// 补丁错误。
#[derive(Debug, thiserror::Error)]
pub enum PatchError {
    /// lopdf 侧错误。
    #[error("lopdf: {0}")]
    Lopdf(#[from] lopdf::Error),
    /// 页号越界。
    #[error("page {page} out of range (1..={count})")]
    PageOutOfRange { page: u32, count: u32 },
    /// 内容流解析失败。
    #[error("parse stream {stream}: {msg}")]
    Parse { stream: u32, msg: String },
}

/// 结果别名。
pub type Result<T, E = PatchError> = std::result::Result<T, E>;

/// 一个操作内的字形快照（删除时算 advance 用）。
#[derive(Debug, Clone, Copy, PartialEq)]
struct GlyphSnap {
    advance: f32,
    size: f32,
}

/// 待应用的补丁集合。
#[derive(Debug, Clone, Default)]
pub struct PatchSet {
    /// `OpKey` → 该操作内被删字形的序号集合。
    deleted: BTreeMap<OpKey, BTreeSet<u16>>,
    /// `OpKey` → 该操作的字形快照（序号 → advance/size）。
    snaps: BTreeMap<OpKey, Vec<GlyphSnap>>,
    /// 当前页的 Form `Do` 记录（由 [`PatchSet::with_forms`] 注入）。
    forms: Vec<FormDo>,
}

impl PatchSet {
    /// 空补丁集。
    pub fn new() -> Self {
        Self::default()
    }

    /// 注入 `bind_page` 得到的 Form `Do` 记录（Form 克隆改名需要）。
    pub fn with_forms(mut self, bound: &crate::bind::BoundPage) -> Self {
        self.forms = bound.form_dos.clone();
        self
    }

    /// 标记删除一批字形。`ids` 中不属于 `ir` 的会被忽略。
    pub fn delete_glyphs(&mut self, ir: &PageIR, ids: &[GlyphId]) {
        // 先按 op 归集要删的序号。
        let mut want: BTreeMap<OpKey, BTreeSet<u16>> = BTreeMap::new();
        for id in ids {
            want.entry(id.op).or_default().insert(id.ordinal);
        }
        // 用 ir 里的字形快照补全 advance/size。
        for item in &ir.items {
            if let DisplayItem::Text { glyphs } = item {
                record_snaps(glyphs, &want, &mut self.snaps, &mut self.deleted);
            }
        }
    }

    /// 是否有任何删除。
    pub fn is_empty(&self) -> bool {
        self.deleted.values().all(BTreeSet::is_empty)
    }

    /// 应用补丁到文档第 `page`（1 基）页。
    pub fn apply(&self, doc: &mut Document, page: u32) -> Result<PatchStats> {
        let pages = doc.get_pages();
        let count = pages.len() as u32;
        let Some(&page_id) = pages.get(&page) else {
            return Err(PatchError::PageOutOfRange { page, count });
        };

        let mut stats = PatchStats::default();

        // 按流分组待改操作。
        let mut by_stream: BTreeMap<ObjRef, BTreeMap<u32, BTreeSet<u16>>> = BTreeMap::new();
        for (key, ords) in &self.deleted {
            if ords.is_empty() {
                continue;
            }
            by_stream
                .entry(key.stream)
                .or_default()
                .insert(key.op_index, ords.clone());
        }

        // Form 流需要克隆：先决定哪些 stream 是 Form。
        let mut form_clones: BTreeMap<ObjRef, ObjectId> = BTreeMap::new();

        for (sref, ops_map) in &by_stream {
            let stream_id = (sref.obj, sref.gen);
            let is_form = is_form_stream(doc, stream_id);
            let target_id = if is_form {
                // 克隆：复制原始（未改）流为新对象。
                if let Some(&new_id) = form_clones.get(sref) {
                    new_id
                } else {
                    let src = doc
                        .get_object(stream_id)
                        .and_then(Object::as_stream)
                        .cloned()?;
                    let new_id = doc.add_object(src);
                    form_clones.insert(*sref, new_id);
                    stats.forms_cloned += 1;
                    new_id
                }
            } else {
                stream_id
            };

            let bytes = match doc.get_object(target_id) {
                Ok(Object::Stream(s)) => s
                    .decompressed_content()
                    .unwrap_or_else(|_| s.content.clone()),
                _ => continue,
            };
            let mut ops = parse_content(&bytes).map_err(|e| PatchError::Parse {
                stream: sref.obj,
                msg: e.to_string(),
            })?;

            let mut touched = false;
            for (op_index, ords) in ops_map {
                let Some(op) = ops.get_mut(*op_index as usize) else {
                    continue;
                };
                let snaps = self
                    .snaps
                    .get(&OpKey::new(*sref, *op_index))
                    .cloned()
                    .unwrap_or_default();
                if rewrite_show_op(op, ords, &snaps) {
                    touched = true;
                    if ords.len() >= snaps.len().max(1) && ords.len() * 2 >= snaps.len().max(1) {
                        // 全部删除的启发式：ordinal 覆盖了所有快照。
                        if snaps
                            .iter()
                            .enumerate()
                            .all(|(i, _)| ords.contains(&(i as u16)))
                        {
                            stats.ops_deleted += 1;
                        } else {
                            stats.ops_rewritten += 1;
                        }
                    } else {
                        stats.ops_rewritten += 1;
                    }
                }
            }

            if !touched {
                continue;
            }
            let new_bytes = write_content(&bytes, &ops);
            write_stream_content(doc, target_id, new_bytes)?;
            stats.streams_touched += 1;

            // Form 克隆：改名并改页级 Do。
            if is_form {
                let new_name = format!("SPfX{}", form_clones.len() - 1);
                let old_name = self
                    .forms
                    .iter()
                    .find(|f| f.target.obj == sref.obj)
                    .map(|f| f.name.clone());
                if let Some(old_name) = old_name {
                    rename_xobject(doc, page_id, &old_name, &new_name, target_id);
                }
            }
        }

        Ok(stats)
    }
}

/// 从 `glyphs` 记录被删字形的快照。
fn record_snaps(
    glyphs: &[Glyph],
    want: &BTreeMap<OpKey, BTreeSet<u16>>,
    snaps: &mut BTreeMap<OpKey, Vec<GlyphSnap>>,
    deleted: &mut BTreeMap<OpKey, BTreeSet<u16>>,
) {
    if glyphs.is_empty() {
        return;
    }
    let op = glyphs[0].id.op;
    let Some(want_ords) = want.get(&op) else {
        return;
    };
    let entry = snaps.entry(op).or_default();
    if entry.len() < glyphs.len() {
        entry.resize(
            glyphs.len(),
            GlyphSnap {
                advance: 0.0,
                size: 0.0,
            },
        );
    }
    for (i, g) in glyphs.iter().enumerate() {
        entry[i] = GlyphSnap {
            advance: g.advance,
            size: g.size,
        };
    }
    let del = deleted.entry(op).or_default();
    for ord in want_ords {
        if (*ord as usize) < glyphs.len() {
            del.insert(*ord);
        }
    }
}

/// 改写一个 text-show 操作；返回是否发生改动。
fn rewrite_show_op(op: &mut Op, ords: &BTreeSet<u16>, snaps: &[GlyphSnap]) -> bool {
    if ords.is_empty() {
        return false;
    }
    let all_deleted = !snaps.is_empty()
        && snaps
            .iter()
            .enumerate()
            .all(|(i, _)| ords.contains(&(i as u16)));
    if all_deleted {
        let total = total_advance(ords, snaps);
        if total.abs() < 1e-6 {
            // 无推进：退化为空操作（改用 TJ 空数组，保持操作符存在）。
            op.operator = "TJ".to_string();
            op.operands = vec![Operand::Array(Vec::new())];
        } else {
            op.operator = "TJ".to_string();
            op.operands = vec![Operand::Array(vec![Operand::Real(-total as f64)])];
        }
        op.mark_dirty();
        return true;
    }

    // 部分删除：重建字符串数组。
    let strings = op
        .text_strings()
        .into_iter()
        .map(|s| s.to_vec())
        .collect::<Vec<_>>();
    if strings.is_empty() {
        return false;
    }
    // 该操作的 code 切分宽度：由字形数 / 总字节数推断。
    let total_bytes: usize = strings.iter().map(Vec::len).sum();
    let n_glyphs = snaps.len().max(1);
    let width = if total_bytes % n_glyphs == 0 && total_bytes / n_glyphs >= 1 {
        (total_bytes / n_glyphs).clamp(1, 2)
    } else {
        1
    };

    // 逐字符串按 code 切分，被删 code 换成 kerning 数值。
    let mut arr: Vec<Operand> = Vec::new();
    let mut ordinal = 0u16;
    for s in &strings {
        let mut cur: Vec<u8> = Vec::new();
        let mut pending: Vec<u8> = Vec::new(); // 累积待输出的当前串
        let mut i = 0usize;
        while i < s.len() {
            let end = (i + width).min(s.len());
            let chunk = &s[i..end];
            if ords.contains(&ordinal) {
                // 冲刷当前串。
                if !pending.is_empty() {
                    cur.extend_from_slice(&pending);
                    arr.push(Operand::Str(std::mem::take(&mut cur)));
                    pending.clear();
                }
                let adv = snaps
                    .get(ordinal as usize)
                    .map(|g| g.advance)
                    .unwrap_or(0.0);
                let size = snaps.get(ordinal as usize).map(|g| g.size).unwrap_or(0.0);
                if size.abs() > 1e-6 && adv.abs() > 1e-6 {
                    let kern = -(adv as f64) * 1000.0 / (size as f64);
                    arr.push(Operand::Real(kern));
                }
            } else {
                pending.extend_from_slice(chunk);
            }
            ordinal += 1;
            i = end;
        }
        if !pending.is_empty() {
            cur.extend_from_slice(&pending);
            arr.push(Operand::Str(cur));
        }
    }
    if arr.is_empty() {
        arr.push(Operand::Str(Vec::new()));
    }
    op.operator = "TJ".to_string();
    op.operands = vec![Operand::Array(arr)];
    op.mark_dirty();
    true
}

/// 被删字形的 advance 之和（1000 单位）。
fn total_advance(ords: &BTreeSet<u16>, snaps: &[GlyphSnap]) -> f32 {
    let mut sum = 0.0f32;
    for &o in ords {
        if let Some(s) = snaps.get(o as usize) {
            if s.size.abs() > 1e-6 {
                sum += s.advance * 1000.0 / s.size;
            }
        }
    }
    sum
}

/// 写回解压后的流内容（重新用 FlateDecode 压缩，必要时）。
fn write_stream_content(doc: &mut Document, id: ObjectId, bytes: Vec<u8>) -> Result<()> {
    let obj = doc.get_object_mut(id)?;
    let stream = obj.as_stream_mut()?;
    stream.set_plain_content(bytes);
    stream.compress()?;
    Ok(())
}

/// 该对象是否为 `/Subtype /Form` 的流。
fn is_form_stream(doc: &Document, id: ObjectId) -> bool {
    match doc.get_object(id) {
        Ok(Object::Stream(s)) => s
            .dict
            .get(b"Subtype")
            .ok()
            .and_then(|o| o.as_name().ok())
            .map(|n| n == b"Form")
            .unwrap_or(false),
        _ => false,
    }
}

/// 在页（或 Form）`/Resources/XObject` 里加新键并改对应 `Do`。
fn rename_xobject(
    doc: &mut Document,
    page_id: ObjectId,
    old_name: &str,
    new_name: &str,
    new_obj: ObjectId,
) {
    // 1) 资源里加键。页级资源可能是指向字典的引用。
    if let Ok(res) = doc.get_or_create_resources(page_id) {
        if let Ok(rd) = res.as_dict_mut() {
            let xobj = match rd.get_mut(b"XObject") {
                Ok(o) => o,
                Err(_) => {
                    rd.set("XObject", Dictionary::new());
                    rd.get_mut(b"XObject").expect("just set")
                }
            };
            if let Ok(xd) = xobj.as_dict_mut() {
                xd.set(new_name.as_bytes().to_vec(), Object::Reference(new_obj));
            }
        }
    }
    // 2) 改内容流里对应那次 `Do`。Form 的 Do 在页内容流或父 Form 流里，
    //    这里按 /Contents 与页 XObject 里的 Form 流逐个尝试。
    let mut streams: Vec<ObjectId> = doc.get_page_contents(page_id);
    // 父 Form 流（Do 记录里 form_path 非空时目标在 Form 内）。
    for (_, id) in xobject_forms(doc, page_id) {
        streams.push(id);
    }
    for sid in streams {
        let Ok(Object::Stream(s)) = doc.get_object(sid) else {
            continue;
        };
        let bytes = s
            .decompressed_content()
            .unwrap_or_else(|_| s.content.clone());
        let Ok(mut ops) = parse_content(&bytes) else {
            continue;
        };
        let mut changed = false;
        for op in ops.iter_mut() {
            if op.operator == "Do"
                && op.operands.first().and_then(Operand::as_name) == Some(old_name)
            {
                op.operands[0] = Operand::Name(new_name.to_string());
                op.mark_dirty();
                changed = true;
            }
        }
        if changed {
            let new_bytes = write_content(&bytes, &ops);
            let _ = write_stream_content(doc, sid, new_bytes);
        }
    }
}

/// 页 `/Resources/XObject` 里全部 Form 流对象。
fn xobject_forms(doc: &Document, page_id: ObjectId) -> Vec<(String, ObjectId)> {
    let mut out = Vec::new();
    let Ok((_, _)) = doc.get_page_resources(page_id) else {
        return out;
    };
    let res = match doc.get_object(page_id).and_then(Object::as_dict) {
        Ok(d) => d.clone(),
        Err(_) => return out,
    };
    let rd = match res.get(b"Resources") {
        Ok(Object::Dictionary(d)) => d.clone(),
        Ok(Object::Reference(id)) => match doc.get_dictionary(*id) {
            Ok(d) => d.clone(),
            Err(_) => return out,
        },
        _ => return out,
    };
    let xd = match rd.get(b"XObject") {
        Ok(Object::Dictionary(d)) => d.clone(),
        Ok(Object::Reference(id)) => match doc.get_dictionary(*id) {
            Ok(d) => d.clone(),
            Err(_) => return out,
        },
        _ => return out,
    };
    for (k, v) in xd.iter() {
        if let Ok(id) = v.as_reference() {
            if is_form_stream(doc, id) {
                out.push((String::from_utf8_lossy(k).into_owned(), id));
            }
        }
    }
    out
}

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use lopdf::Stream;
    use smallvec::SmallVec;
    use syncpdf_core::ir::{DisplayItem, GlyphFlags, GlyphSource, PageIR};
    use syncpdf_core::{Color, GlyphId, Matrix, ObjRef, OpKey, PageId, Rect};

    /// 构造一个内存文档：一页 + 一个内容流。
    fn doc_with_content(content: &[u8]) -> (Document, ObjectId, ObjectId) {
        let mut doc = Document::with_version("1.7");
        let pages_id = doc.new_object_id();
        let content_id = doc.add_object(Stream::new(Dictionary::new(), content.to_vec()));
        let mut page = Dictionary::new();
        page.set("Type", Object::Name(b"Page".to_vec()));
        page.set("Parent", Object::Reference(pages_id));
        page.set("MediaBox", vec![0.into(), 0.into(), 612.into(), 792.into()]);
        page.set("Contents", Object::Reference(content_id));
        page.set("Resources", Dictionary::new());
        let page_id = doc.add_object(page);
        let mut pages = Dictionary::new();
        pages.set("Type", Object::Name(b"Pages".to_vec()));
        pages.set("Kids", vec![Object::Reference(page_id)]);
        pages.set("Count", 1);
        doc.objects.insert(pages_id, Object::Dictionary(pages));
        let catalog = doc.add_object(Dictionary::new());
        if let Ok(Object::Dictionary(d)) = doc.get_object_mut(catalog) {
            d.set("Type", Object::Name(b"Catalog".to_vec()));
            d.set("Pages", Object::Reference(pages_id));
        }
        doc.trailer.set("Root", Object::Reference(catalog));
        (doc, page_id, content_id)
    }

    /// 造一个带 `n` 个字形的 Text item。
    fn ir_with(glyphs: Vec<Glyph>) -> PageIR {
        PageIR {
            page: PageId(0),
            media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            crop_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            rotation: 0,
            fonts: Vec::new(),
            items: vec![DisplayItem::Text { glyphs }],
        }
    }

    fn glyph(op: OpKey, ordinal: u16, advance: f32, size: f32) -> Glyph {
        Glyph {
            id: GlyphId {
                page: PageId(0),
                op,
                ordinal,
            },
            unicode: SmallVec::new(),
            code: u32::from(ordinal),
            font: 0,
            size,
            matrix: Matrix::IDENTITY,
            bbox: Rect::default(),
            advance,
            fill: Color::BLACK,
            render_mode: 0,
            source: GlyphSource {
                element_index: 0,
                string_operand_range: (0, 1),
                decoded_code_range: (u32::from(ordinal), u32::from(ordinal) + 1),
            },
            flags: GlyphFlags::default(),
        }
    }

    fn content_bytes(doc: &Document, id: ObjectId) -> Vec<u8> {
        doc.get_object(id)
            .and_then(Object::as_stream)
            .and_then(|s| s.decompressed_content())
            .expect("stream content")
    }

    #[test]
    fn delete_all_glyphs_becomes_kerning_tj() {
        // Tj (ABC) —— 3 字形，全部删除。
        let (mut doc, _page, content) =
            doc_with_content(b"BT /F1 10 Tf 1 0 0 1 10 20 Tm (ABC) Tj ET");
        let op = OpKey::new(ObjRef::new(content.0, content.1), 3);
        let ir = ir_with(vec![
            glyph(op, 0, 5.0, 10.0),
            glyph(op, 1, 5.0, 10.0),
            glyph(op, 2, 5.0, 10.0),
        ]);
        let mut ps = PatchSet::new();
        let ids: Vec<GlyphId> = ir.glyphs().map(|g| g.id).collect();
        ps.delete_glyphs(&ir, &ids);
        let stats = ps.apply(&mut doc, 1).unwrap();
        assert_eq!(stats.ops_deleted, 1);
        assert_eq!(stats.streams_touched, 1);

        let out = content_bytes(&doc, content);
        let text = String::from_utf8_lossy(&out);
        assert!(text.contains("TJ"), "应为 TJ：{text}");
        // 3 × 5pt @10pt = 1500 → -1500
        assert!(text.contains("-1500"), "应含等宽 kerning -1500：{text}");
        assert!(!text.contains("ABC"), "原字符串应已删除：{text}");
    }

    #[test]
    fn delete_partial_keeps_remaining_bytes() {
        // Tj (ABCD) —— 删中间两个。
        let (mut doc, _page, content) = doc_with_content(b"BT /F1 10 Tf (ABCD) Tj ET");
        let op = OpKey::new(ObjRef::new(content.0, content.1), 2);
        let ir = ir_with(vec![
            glyph(op, 0, 5.0, 10.0),
            glyph(op, 1, 5.0, 10.0),
            glyph(op, 2, 5.0, 10.0),
            glyph(op, 3, 5.0, 10.0),
        ]);
        let mut ps = PatchSet::new();
        let ids = vec![
            ir.glyphs().nth(1).unwrap().id,
            ir.glyphs().nth(2).unwrap().id,
        ];
        ps.delete_glyphs(&ir, &ids);
        let stats = ps.apply(&mut doc, 1).unwrap();
        assert_eq!(stats.ops_rewritten, 1);

        let out = content_bytes(&doc, content);
        let text = String::from_utf8_lossy(&out);
        assert!(text.contains("A"), "首字节保留：{text}");
        assert!(text.contains("D"), "末字节保留：{text}");
        assert!(text.contains("-500"), "每字 kerning = -500：{text}");
        // B、C 不应单独出现。
        let bs = text.find("(A").unwrap();
        let de = text.find("D)").unwrap();
        assert!(bs < de);
    }

    #[test]
    fn untouched_bytes_are_preserved() {
        let src: &[u8] = b"BT /F1 10 Tf (AB) Tj ET";
        let (mut doc, _page, content) = doc_with_content(src);
        let op = OpKey::new(ObjRef::new(content.0, content.1), 99); // 不存在的 op
        let ir = ir_with(vec![glyph(op, 0, 5.0, 10.0)]);
        let mut ps = PatchSet::new();
        let id = ir.glyphs().next().unwrap().id;
        ps.delete_glyphs(&ir, &[id]);
        let stats = ps.apply(&mut doc, 1).unwrap();
        assert_eq!(stats.streams_touched, 0);
        assert_eq!(content_bytes(&doc, content), src, "未命中时字节不变");
    }

    #[test]
    fn tj_array_rewrite_preserves_kernings_position() {
        // TJ [(AB) -100 (CD)] —— 删 B 与 C。
        let src: &[u8] = b"BT [(AB) -100 (CD)] TJ ET";
        let (mut doc, _page, content) = doc_with_content(src);
        let op = OpKey::new(ObjRef::new(content.0, content.1), 1);
        let ir = ir_with(vec![
            glyph(op, 0, 5.0, 10.0),
            glyph(op, 1, 5.0, 10.0),
            glyph(op, 2, 5.0, 10.0),
            glyph(op, 3, 5.0, 10.0),
        ]);
        let mut ps = PatchSet::new();
        let ids = vec![
            ir.glyphs().nth(1).unwrap().id,
            ir.glyphs().nth(2).unwrap().id,
        ];
        ps.delete_glyphs(&ir, &ids);
        ps.apply(&mut doc, 1).unwrap();
        let out = content_bytes(&doc, content);
        let text = String::from_utf8_lossy(&out);
        assert!(text.contains("TJ"));
        assert!(text.contains("(A)") && text.contains("(D)"), "{text}");
        assert!(!text.contains("(AB)") && !text.contains("(CD)"), "{text}");
        // 每个被删 code 5pt@10pt = 500。
        assert!(text.contains("-500"), "应有 -500 kerning：{text}");
    }

    #[test]
    fn empty_patchset_is_empty_and_noop() {
        let (mut doc, _page, content) = doc_with_content(b"BT (AB) Tj ET");
        let ps = PatchSet::new();
        assert!(ps.is_empty());
        let stats = ps.apply(&mut doc, 1).unwrap();
        assert_eq!(stats, PatchStats::default());
        assert_eq!(content_bytes(&doc, content), b"BT (AB) Tj ET");
    }

    #[test]
    fn page_out_of_range_errors() {
        let (mut doc, _page, _content) = doc_with_content(b"BT (AB) Tj ET");
        let ps = PatchSet::new();
        assert!(matches!(
            ps.apply(&mut doc, 5),
            Err(PatchError::PageOutOfRange { .. })
        ));
    }
}
