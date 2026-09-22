//! 字形 ↔ 内容流操作绑定（M0-06）。
//!
//! 设计基准：02-技术路径与架构.md §6、research/02-hjfy-engine-deep-dive.md §3。
//!
//! # 思路
//!
//! 把 lopdf 内容流解析出的 text-show 操作（`Tj`/`TJ`/`'`/`"`）与 pdfium 的文本对象
//! 一一配对：pdfium 每遇到一个 text-show 操作就生成一个 [`crate::pdfium::TextObject`]，
//! 因此按顺序配对即可。配对成功后按当前 `Tf` 字体的编码把字符串操作数切成 code，
//! 每个 code 产出一个 [`Glyph`]（带字节级溯源 [`GlyphSource`]），供 `patch` 做字形级删除。
//!
//! Form XObject 内的操作按递归进入的次序编号，得到 `form_path`（页级为 `[]`），
//! 与 `pdfium::TextObject::form_path` 的分量一一对应。
//!
//! # 降级策略（绝不 panic）
//!
//! - code 数与 pdfium 字符数不等：仍按 code 数出字形，unicode 用能对上的前缀，
//!   其余置空，`BindStats::degraded` 计数并记 issue；
//! - pdfium 文本对象数与 text-show 操作数不等：多出的操作走「无 unicode、空 bbox」
//!   的降级路径，多出的对象忽略，记 issue；
//! - 任一流解压/解析失败：跳过该流并记 issue，不影响其余流。

use std::collections::BTreeMap;

use lopdf::{Dictionary, Document, Object, ObjectId};
use smallvec::SmallVec;
use syncpdf_core::ir::{DisplayItem, FontRef, Glyph, GlyphFlags, GlyphSource, PageIR};
use syncpdf_core::{Color, GlyphId, Matrix, ObjRef, OpKey, PageId, Point, Rect};

use crate::content::{parse_content, Op, Operand};
use crate::pdfium::{DocId, PdfiumWorker, TextObject};

/// Form XObject 递归深度上限，防止病态文件爆栈。
const MAX_FORM_DEPTH: usize = 8;

/// 绑定统计。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct BindStats {
    /// pdfium 报告的文本对象数。
    pub text_objects: u32,
    /// 内容流中的 text-show 操作数。
    pub text_ops: u32,
    /// 成功逐字形配对的 text-show 操作数（code 数与字符数相等）。
    pub matched: u32,
    /// 走降级路径的操作数（数量不等或自行解码）。
    pub degraded: u32,
}

/// 一次 `Do` 引用 Form XObject 的记录（供 `patch` 克隆改名用）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FormDo {
    /// 进入的 Form 序号路径（即这条 `Do` 所在的流层级）。
    pub form_path: Vec<u32>,
    /// `Do` 操作本身所在的流与操作序号。
    pub do_op: OpKey,
    /// `/Resources/XObject` 里的键名。
    pub name: String,
    /// 被 `Do` 的 Form 流对象 id。
    pub target: ObjRef,
}

/// 绑定结果。
#[derive(Debug, Clone)]
pub struct BoundPage {
    /// 页面 IR。
    pub ir: PageIR,
    pub stats: BindStats,
    /// 降级/异常说明（不构成失败）。
    pub issues: Vec<String>,
    /// 页级与 Form 内的 Form 类型 `Do` 记录，按出现次序。
    pub form_dos: Vec<FormDo>,
    /// 页对象 id。
    pub page_id: ObjectId,
}

/// 绑定错误（只有真正无法继续的情况才返回 `Err`）。
#[derive(Debug, thiserror::Error)]
pub enum BindError {
    /// lopdf 侧错误。
    #[error("lopdf: {0}")]
    Lopdf(#[from] lopdf::Error),
    /// pdfium 侧错误。
    #[error("pdfium: {0}")]
    Pdfium(#[from] crate::pdfium::PdfiumError),
    /// 页号超出文档范围。
    #[error("page {page} out of range (1..={count})")]
    PageOutOfRange { page: u32, count: u32 },
}

/// 结果别名。
pub type Result<T, E = BindError> = std::result::Result<T, E>;

// ---------------------------------------------------------------------------
// 文本状态
// ---------------------------------------------------------------------------

/// 遍历时跟踪的图形/文本状态。
#[derive(Debug, Clone)]
struct Ctx {
    ctm: Matrix,
    text_matrix: Matrix,
    text_line_matrix: Matrix,
    leading: f32,
    char_spacing: f32,
    word_spacing: f32,
    h_scale: f32,
    font_name: String,
    font_size: f32,
    /// 当前流作用域的资源字典（页级或 Form 级）。
    resources: Dictionary,
}

impl Ctx {
    fn new(resources: Dictionary) -> Self {
        Self {
            ctm: Matrix::IDENTITY,
            text_matrix: Matrix::IDENTITY,
            text_line_matrix: Matrix::IDENTITY,
            leading: 0.0,
            char_spacing: 0.0,
            word_spacing: 0.0,
            h_scale: 1.0,
            font_name: String::new(),
            font_size: 0.0,
            resources,
        }
    }
}

/// 一条扁平化的 text-show 操作（未配对前）。
#[derive(Debug, Clone)]
struct FlatTextOp {
    form_path: Vec<u32>,
    key: OpKey,
    /// 显示操作携带的字符串（`TJ` 为数组里的全部字符串，按出现次序）。
    strings: Vec<Vec<u8>>,
    /// 每个字符串操作数在 `TJ` 数组里的下标（`Tj`/`'`/`"` 恒为 0）。
    element_indices: Vec<u32>,
    /// 该操作所在流的文本状态快照。
    font_name: String,
    font_size: f32,
    /// 当前 `/Resources/Font` 里 `font_name` 对应的对象 id。
    font_id: Option<ObjectId>,
}

// ---------------------------------------------------------------------------
// 编码切分（纯函数）
// ---------------------------------------------------------------------------

/// 编码类别。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncodingKind {
    /// 1 字节一 code。
    Single,
    /// 2 字节一 code。
    Double,
}

/// 按编码把字符串字节切成 code 列表。
pub fn split_codes(bytes: &[u8], kind: EncodingKind) -> Vec<u32> {
    match kind {
        EncodingKind::Single => bytes.iter().map(|b| u32::from(*b)).collect(),
        EncodingKind::Double => bytes
            .chunks(2)
            .map(|c| {
                if c.len() == 2 {
                    u32::from(u16::from_be_bytes([c[0], c[1]]))
                } else {
                    u32::from(c[0]) << 8
                }
            })
            .collect(),
    }
}

/// 每个 code 的字节宽度。
fn code_width(kind: EncodingKind) -> usize {
    match kind {
        EncodingKind::Single => 1,
        EncodingKind::Double => 2,
    }
}

// ---------------------------------------------------------------------------
// 字体宽度表（纯函数）
// ---------------------------------------------------------------------------

/// 字体的宽度描述（PDF 1000 单位）。
#[derive(Debug, Clone, Default)]
pub struct FontWidths {
    /// Type0 `/DW`，缺省 1000。
    pub dw: f32,
    /// Type0 `/W` 展开后的 `(cid, width1000)`。
    pub w: Vec<(u32, f32)>,
    /// 简单字体 `/FirstChar`。
    pub first_char: i64,
    /// 简单字体 `/Widths`。
    pub widths: Vec<f32>,
    /// 是否 Type0。
    pub is_type0: bool,
    /// 是否 Identity 编码（Type0 默认按 Identity 处理）。
    pub identity: bool,
}

impl FontWidths {
    /// 取某 code 的宽度（1000 单位）。
    pub fn width_1000(&self, code: u32) -> f32 {
        if self.is_type0 {
            for &(c, w) in &self.w {
                if c == code {
                    return w;
                }
            }
            self.dw
        } else {
            let idx = code as i64 - self.first_char;
            if idx >= 0 {
                if let Some(w) = self.widths.get(idx as usize) {
                    return *w;
                }
            }
            1000.0
        }
    }

    /// 取某 code 的宽度（pt）。
    pub fn advance_pt(&self, code: u32, size: f32) -> f32 {
        self.width_1000(code) * size / 1000.0
    }
}

/// 解析 `/W` 数组（`c [w...]` 与 `cfirst clast w` 两种形式）。
fn parse_w_array(arr: &[Object], out: &mut Vec<(u32, f32)>) {
    let mut i = 0usize;
    while i < arr.len() {
        match &arr[i] {
            Object::Array(ws) => {
                let start = if i == 0 {
                    0u32
                } else {
                    arr[i - 1].as_i64().unwrap_or(0).max(0) as u32
                };
                for (k, w) in ws.iter().enumerate() {
                    if let Ok(v) = w.as_float() {
                        out.push((start.saturating_add(k as u32), v));
                    }
                }
                i += 1;
            }
            Object::Integer(cfirst) => {
                if i + 2 < arr.len() {
                    if let (Ok(clast), Ok(w)) = (arr[i + 1].as_i64(), arr[i + 2].as_float()) {
                        let a = (*cfirst).max(0) as u32;
                        let b = clast.max(0) as u32;
                        // 展开上限，防止病态范围耗尽内存。
                        for c in a..=b.min(a.saturating_add(65535)) {
                            out.push((c, w));
                        }
                    }
                    i += 3;
                    continue;
                }
                i += 1;
            }
            _ => i += 1,
        }
    }
}

/// 从 PDF 字体字典解析宽度描述与编码。
pub fn font_widths(doc: &Document, font_id: ObjectId) -> FontWidths {
    let mut out = FontWidths::default();
    let Some(d) = deref_dict(doc, font_id) else {
        return out;
    };
    let subtype = d.get(b"Subtype").ok().and_then(name_of);
    out.is_type0 = subtype.as_deref() == Some("Type0");
    if out.is_type0 {
        out.dw = d
            .get(b"DW")
            .ok()
            .and_then(|o| o.as_float().ok())
            .unwrap_or(1000.0);
        // Identity-H/V 是编码名；内联 CMap 也按双字节处理。
        out.identity = true;
        if let Ok(arr) = d.get(b"W").and_then(|o| o.as_array()) {
            parse_w_array(arr, &mut out.w);
        }
    } else {
        out.first_char = d
            .get(b"FirstChar")
            .ok()
            .and_then(|o| o.as_i64().ok())
            .unwrap_or(0);
        if let Ok(arr) = d.get(b"Widths").and_then(|o| o.as_array()) {
            out.widths = arr.iter().filter_map(|o| o.as_float().ok()).collect();
        }
    }
    out
}

/// 去掉 `/BaseFont` 的子集前缀（`ABCDEF+Name` → `Name`）。
pub fn strip_subset_prefix(name: &str) -> String {
    match name.split_once('+') {
        Some((pre, rest))
            if pre.len() == 6 && !pre.is_empty() && pre.chars().all(|c| c.is_ascii_uppercase()) =>
        {
            rest.to_string()
        }
        _ => name.to_string(),
    }
}

/// 由 `/FontDescriptor` 与名字推断字体特征：`(serif, fixed_pitch, italic, bold)`。
fn font_traits(doc: &Document, d: &Dictionary, base_font: &str) -> (bool, bool, bool, bool) {
    let (mut serif, mut fixed, mut italic, mut bold) = (false, false, false, false);
    if let Some(fd) = dict_ref(d, b"FontDescriptor").and_then(|id| deref_dict(doc, id)) {
        if let Ok(flags) = fd.get(b"Flags").and_then(|o| o.as_i64()) {
            serif = flags & 2 != 0;
            fixed = flags & 1 != 0;
            italic = flags & 64 != 0;
            bold = flags & (1 << 18) != 0;
        }
        if let Ok(v) = fd.get(b"StemV").and_then(|o| o.as_float()) {
            if v > 120.0 {
                bold = true;
            }
        }
        if let Ok(v) = fd.get(b"ItalicAngle").and_then(|o| o.as_float()) {
            if v.abs() > 0.01 {
                italic = true;
            }
        }
        if let Ok(o) = fd.get(b"FontName") {
            if let Some(n) = name_of(o) {
                let flat = n.rsplit('+').next().unwrap_or(&n).to_ascii_lowercase();
                if flat.contains("bold") {
                    bold = true;
                }
                if flat.contains("italic") || flat.contains("oblique") {
                    italic = true;
                }
            }
        }
    }
    let lower = base_font.to_ascii_lowercase();
    if lower.contains("bold") {
        bold = true;
    }
    if lower.contains("italic") || lower.contains("oblique") {
        italic = true;
    }
    (serif, fixed, italic, bold)
}

// ---------------------------------------------------------------------------
// lopdf 辅助
// ---------------------------------------------------------------------------

/// 解引用到字典：普通字典对象与流对象（取其 dict）都接受。
fn deref_dict(doc: &Document, id: ObjectId) -> Option<&Dictionary> {
    match doc.get_object(id) {
        Ok(Object::Dictionary(d)) => Some(d),
        Ok(Object::Stream(s)) => Some(&s.dict),
        _ => None,
    }
}

fn name_of(obj: &Object) -> Option<String> {
    obj.as_name()
        .ok()
        .map(|b| String::from_utf8_lossy(b).into_owned())
}

fn dict_ref(d: &Dictionary, key: &[u8]) -> Option<ObjectId> {
    d.get(key).ok().and_then(|o| o.as_reference().ok())
}

/// 把 `/Font` 或 `/XObject` 这类「名字→引用」字典解引用成拥有的字典。
fn sub_dict(doc: &Document, d: &Dictionary, key: &[u8]) -> DictMap {
    let mut out = DictMap::new();
    let src = match d.get(key).ok() {
        Some(Object::Dictionary(x)) => Some(x.clone()),
        Some(Object::Reference(id)) => deref_dict(doc, *id).cloned(),
        _ => None,
    };
    if let Some(src) = src {
        for (k, v) in src.iter() {
            if let Ok(id) = v.as_reference() {
                out.insert(String::from_utf8_lossy(k).into_owned(), id);
            }
        }
    }
    out
}

type DictMap = BTreeMap<String, ObjectId>;

/// 页的资源字典（继承 `/Resources`，不做父链合并——pdfium 已按父链解析）。
fn page_resources(doc: &Document, page_id: ObjectId) -> Dictionary {
    let Some(d) = deref_dict(doc, page_id) else {
        return Dictionary::new();
    };
    match d.get(b"Resources").ok() {
        Some(Object::Dictionary(x)) => x.clone(),
        Some(Object::Reference(id)) => deref_dict(doc, *id).cloned().unwrap_or_default(),
        _ => Dictionary::new(),
    }
}

fn page_box(doc: &Document, page_id: ObjectId, key: &[u8]) -> Option<Rect> {
    let d = deref_dict(doc, page_id)?;
    let arr = d.get(key).ok()?.as_array().ok()?;
    let v: Vec<f32> = arr.iter().filter_map(|o| o.as_float().ok()).collect();
    if v.len() < 4 {
        return None;
    }
    Some(Rect::new(v[0], v[1], v[2], v[3]))
}

fn page_rotation(doc: &Document, page_id: ObjectId) -> i32 {
    deref_dict(doc, page_id)
        .and_then(|d| d.get(b"Rotate").ok())
        .and_then(|o| o.as_i64().ok())
        .unwrap_or(0) as i32
}

// ---------------------------------------------------------------------------
// 内容流遍历
// ---------------------------------------------------------------------------

/// 遍历结果。
struct WalkOut {
    flat_text: Vec<FlatTextOp>,
    items: Vec<DisplayItem>,
    form_dos: Vec<FormDo>,
    issues: Vec<String>,
}

#[allow(clippy::too_many_arguments)]
fn walk_stream(
    doc: &Document,
    stream_id: ObjectId,
    bytes: &[u8],
    ctx: &mut Ctx,
    form_path: &[u32],
    depth: usize,
    out: &mut WalkOut,
) {
    if depth > MAX_FORM_DEPTH {
        out.issues
            .push(format!("form depth limit at stream {}", stream_id.0));
        return;
    }
    let ops = match parse_content(bytes) {
        Ok(ops) => ops,
        Err(e) => {
            out.issues
                .push(format!("parse stream {}: {e}", stream_id.0));
            return;
        }
    };
    let sref = ObjRef::new(stream_id.0, stream_id.1);
    let mut ctm_stack: Vec<Matrix> = Vec::new();
    let mut path_pts: Vec<Point> = Vec::new();
    let mut form_seq = 0u32;

    for (op_index, op) in ops.iter().enumerate() {
        let key = OpKey::new(sref, op_index as u32);
        match op.operator.as_str() {
            "q" => ctm_stack.push(ctx.ctm),
            "Q" => {
                if let Some(m) = ctm_stack.pop() {
                    ctx.ctm = m;
                }
            }
            "cm" => {
                if let Some(m) = matrix_from_array(&op.operands) {
                    ctx.ctm = m.then(&ctx.ctm);
                }
            }
            "BT" => {
                ctx.text_matrix = Matrix::IDENTITY;
                ctx.text_line_matrix = Matrix::IDENTITY;
            }
            "Tf" => {
                if let Some(n) = op.operands.first().and_then(Operand::as_name) {
                    ctx.font_name = n.to_string();
                }
                if let Some(s) = op.operands.get(1).and_then(Operand::as_f64) {
                    ctx.font_size = s as f32;
                }
            }
            "TL" => set_f32(&op.operands, 0, &mut ctx.leading),
            "Tc" => set_f32(&op.operands, 0, &mut ctx.char_spacing),
            "Tw" => set_f32(&op.operands, 0, &mut ctx.word_spacing),
            "Tz" => {
                if let Some(v) = op.operands.first().and_then(Operand::as_f64) {
                    ctx.h_scale = v as f32 / 100.0;
                }
            }
            "Tm" => {
                if let Some(m) = matrix_from_array(&op.operands) {
                    ctx.text_matrix = m;
                    ctx.text_line_matrix = m;
                }
            }
            "Td" | "TD" => {
                let tx = op.operands.first().and_then(Operand::as_f64).unwrap_or(0.0) as f32;
                let ty = op.operands.get(1).and_then(Operand::as_f64).unwrap_or(0.0) as f32;
                ctx.text_line_matrix = Matrix::translate(tx, ty).then(&ctx.text_line_matrix);
                ctx.text_matrix = ctx.text_line_matrix;
                if op.operator == "TD" {
                    ctx.leading = -ty;
                }
            }
            "T*" => {
                ctx.text_line_matrix =
                    Matrix::translate(0.0, -ctx.leading).then(&ctx.text_line_matrix);
                ctx.text_matrix = ctx.text_line_matrix;
            }
            "Do" => {
                let name = op
                    .operands
                    .first()
                    .and_then(Operand::as_name)
                    .map(str::to_string)
                    .unwrap_or_default();
                let mut child = Ctx {
                    ctm: ctx.ctm,
                    font_name: ctx.font_name.clone(),
                    font_size: ctx.font_size,
                    resources: ctx.resources.clone(),
                    ..ctx.clone()
                };
                handle_do(
                    doc,
                    &mut child,
                    key,
                    &name,
                    form_path,
                    depth,
                    &mut form_seq,
                    out,
                );
            }
            "BI" => out.items.push(DisplayItem::InlineImage {
                bbox: unit_box(&ctx.ctm),
            }),
            "m" | "l" => {
                if let Some(p) = point_at(&op.operands, 0) {
                    path_pts.push(p);
                }
            }
            "c" => {
                for k in [0usize, 2, 4] {
                    if let Some(p) = point_at(&op.operands, k) {
                        path_pts.push(p);
                    }
                }
            }
            "v" | "y" => {
                for k in [0usize, 2] {
                    if let Some(p) = point_at(&op.operands, k) {
                        path_pts.push(p);
                    }
                }
            }
            "re" => {
                if let (Some(x), Some(y), Some(w), Some(h)) = (
                    op.operands.first().and_then(Operand::as_f64),
                    op.operands.get(1).and_then(Operand::as_f64),
                    op.operands.get(2).and_then(Operand::as_f64),
                    op.operands.get(3).and_then(Operand::as_f64),
                ) {
                    path_pts.push(Point::new(x as f32, y as f32));
                    path_pts.push(Point::new((x + w) as f32, (y + h) as f32));
                }
            }
            "f" | "F" | "f*" | "B" | "B*" | "b" | "b*" | "S" | "s" => {
                if !path_pts.is_empty() {
                    let bbox = points_bbox(&path_pts, &ctx.ctm);
                    let o = op.operator.as_str();
                    let is_fill = matches!(o, "f" | "F" | "f*" | "B" | "B*" | "b" | "b*");
                    let is_stroke = matches!(o, "B" | "B*" | "b" | "b*" | "S" | "s");
                    out.items.push(DisplayItem::Path {
                        bbox,
                        is_fill,
                        is_stroke,
                    });
                    path_pts.clear();
                }
            }
            _ => {
                if op.is_text_show() {
                    let strings: Vec<Vec<u8>> =
                        op.text_strings().into_iter().map(|s| s.to_vec()).collect();
                    out.flat_text.push(FlatTextOp {
                        form_path: form_path.to_vec(),
                        key,
                        strings,
                        element_indices: element_indices_of(op),
                        font_name: ctx.font_name.clone(),
                        font_size: ctx.font_size,
                        font_id: font_map(doc, &ctx.resources).get(&ctx.font_name).copied(),
                    });
                    out.items.push(DisplayItem::Text { glyphs: Vec::new() });
                }
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn handle_do(
    doc: &Document,
    ctx: &mut Ctx,
    do_op: OpKey,
    name: &str,
    form_path: &[u32],
    depth: usize,
    form_seq: &mut u32,
    out: &mut WalkOut,
) {
    let xobjects = sub_dict(doc, &ctx.resources, b"XObject");
    let Some(&target_id) = xobjects.get(name) else {
        out.issues.push(format!(
            "Do /{} not in /XObject (stream {}), xobjects={:?}",
            name,
            do_op.stream.obj,
            xobjects.keys().collect::<Vec<_>>()
        ));
        return;
    };
    let target = deref_dict(doc, target_id);
    let is_form = target
        .and_then(|d| d.get(b"Subtype").ok())
        .and_then(name_of)
        .map(|s| s == "Form")
        .unwrap_or(false);
    if !is_form {
        // Image 或其它：按单位正方形 × CTM 记 bbox。
        out.items.push(DisplayItem::Image {
            bbox: unit_box(&ctx.ctm),
        });
        return;
    }

    let seq = *form_seq;
    *form_seq += 1;
    out.form_dos.push(FormDo {
        form_path: form_path.to_vec(),
        do_op,
        name: name.to_string(),
        target: ObjRef::new(target_id.0, target_id.1),
    });

    out.items.push(DisplayItem::FormBegin {
        name: name.to_string(),
        ctm: ctx.ctm,
    });

    let Some(fdict) = target else {
        out.items.push(DisplayItem::FormEnd);
        return;
    };
    // Form 自己的资源与 /Matrix。
    let mut child = Ctx::new(page_resources_of_form(doc, fdict));
    let form_matrix = fdict
        .get(b"Matrix")
        .ok()
        .and_then(|o| o.as_array().ok())
        .and_then(|a| {
            let v: Vec<Operand> = a
                .iter()
                .map(|o| Operand::Real(o.as_float().unwrap_or(0.0) as f64))
                .collect();
            matrix_from_array(&v)
        })
        .unwrap_or(Matrix::IDENTITY);
    child.ctm = form_matrix.then(&ctx.ctm);
    child.font_name = ctx.font_name.clone();
    child.font_size = ctx.font_size;

    let mut child_path = form_path.to_vec();
    child_path.push(seq);

    if let Ok(Object::Stream(s)) = doc.get_object(target_id) {
        let content = s
            .decompressed_content()
            .unwrap_or_else(|_| s.content.clone());
        walk_stream(
            doc,
            target_id,
            &content,
            &mut child,
            &child_path,
            depth + 1,
            out,
        );
    }
    out.items.push(DisplayItem::FormEnd);
}

/// Form 的 `/Resources`（缺省回退到空字典）。
fn page_resources_of_form(doc: &Document, fdict: &Dictionary) -> Dictionary {
    match fdict.get(b"Resources").ok() {
        Some(Object::Dictionary(x)) => x.clone(),
        Some(Object::Reference(id)) => deref_dict(doc, *id).cloned().unwrap_or_default(),
        _ => Dictionary::new(),
    }
}

/// 从资源字典取 `/Font` 的「名字 → 对象 id」表。
///
/// `/Font` 可能是内联字典，也可能是指向字典的间接引用（Form XObject 常见）。
fn font_map(doc: &Document, resources: &Dictionary) -> DictMap {
    let mut out = DictMap::new();
    let font = match resources.get(b"Font") {
        Ok(Object::Dictionary(d)) => Some(d.clone()),
        Ok(Object::Reference(id)) => deref_dict(doc, *id).cloned(),
        _ => None,
    };
    if let Some(d) = font {
        for (k, v) in d.iter() {
            if let Ok(id) = v.as_reference() {
                out.insert(String::from_utf8_lossy(k).into_owned(), id);
            }
        }
    }
    out
}

fn set_f32(ops: &[Operand], idx: usize, slot: &mut f32) {
    if let Some(v) = ops.get(idx).and_then(Operand::as_f64) {
        *slot = v as f32;
    }
}

fn point_at(ops: &[Operand], base: usize) -> Option<Point> {
    let x = ops.get(base).and_then(Operand::as_f64)? as f32;
    let y = ops.get(base + 1).and_then(Operand::as_f64)? as f32;
    Some(Point::new(x, y))
}

fn matrix_from_array(ops: &[Operand]) -> Option<Matrix> {
    if ops.len() < 6 {
        return None;
    }
    let mut m = [0f32; 6];
    for (i, v) in ops.iter().take(6).enumerate() {
        m[i] = v.as_f64()? as f32;
    }
    Some(Matrix::from(m))
}

/// `TJ` 数组里各字符串对应的元素下标；其余操作符恒为 0。
fn element_indices_of(op: &Op) -> Vec<u32> {
    match op.operator.as_str() {
        "TJ" => {
            let mut out = Vec::new();
            if let Some(arr) = op.operands.iter().rev().find_map(Operand::as_array) {
                for (i, it) in arr.iter().enumerate() {
                    if it.as_bytes().is_some() {
                        out.push(i as u32);
                    }
                }
            }
            out
        }
        _ => vec![0],
    }
}

fn unit_box(ctm: &Matrix) -> Rect {
    points_bbox(
        &[
            Point::new(0.0, 0.0),
            Point::new(1.0, 0.0),
            Point::new(0.0, 1.0),
            Point::new(1.0, 1.0),
        ],
        ctm,
    )
}

/// 一组点的 CTM 变换外接框；空输入返回零矩形。
fn points_bbox(pts: &[Point], ctm: &Matrix) -> Rect {
    let mut it = pts.iter().map(|p| ctm.apply(*p));
    let Some(first) = it.next() else {
        return Rect::default();
    };
    let mut bbox = Rect::new(first.x, first.y, first.x, first.y);
    for p in it {
        bbox.x0 = bbox.x0.min(p.x);
        bbox.y0 = bbox.y0.min(p.y);
        bbox.x1 = bbox.x1.max(p.x);
        bbox.y1 = bbox.y1.max(p.y);
    }
    bbox
}

// ---------------------------------------------------------------------------
// 配对
// ---------------------------------------------------------------------------

/// 配对后的字形，按 text-show 操作归组。
type GlyphGroups = BTreeMap<(ObjRef, u32), Vec<Glyph>>;

/// 编码类别：Type0 Identity → 双字节。
fn encoding_of(doc: &Document, font_id: Option<ObjectId>) -> (EncodingKind, Option<FontWidths>) {
    match font_id {
        Some(id) => {
            let w = font_widths(doc, id);
            let kind = if w.is_type0 && w.identity {
                EncodingKind::Double
            } else {
                EncodingKind::Single
            };
            (kind, Some(w))
        }
        None => (EncodingKind::Single, None),
    }
}

fn bind_glyphs(
    doc: &Document,
    ops_by_group: &BTreeMap<Vec<u32>, Vec<&FlatTextOp>>,
    objs_by_group: &BTreeMap<Vec<u32>, Vec<&TextObject>>,
    font_index: &BTreeMap<String, u32>,
    stats: &mut BindStats,
    issues: &mut Vec<String>,
) -> GlyphGroups {
    let mut out = GlyphGroups::new();
    for (path, ops) in ops_by_group {
        let objs = objs_by_group.get(path);
        for (i, fop) in ops.iter().enumerate() {
            let obj = objs.and_then(|v| v.get(i)).copied();
            let (kind, widths) = encoding_of(doc, fop.font_id);
            let font_idx = font_index.get(&fop.font_name).copied().unwrap_or(0);

            // 把该操作的字符串拼成 (code, element_index, byte_range) 序列。
            let w = code_width(kind);
            let mut codes: Vec<(u32, u32, (u32, u32))> = Vec::new();
            let mut byte_off = 0u32;
            for (si, s) in fop.strings.iter().enumerate() {
                let ele = *fop.element_indices.get(si).unwrap_or(&0);
                for (ci, c) in split_codes(s, kind).into_iter().enumerate() {
                    let b0 = byte_off + (ci * w) as u32;
                    let avail = s.len().saturating_sub(ci * w).min(w);
                    let b1 = b0 + avail as u32;
                    codes.push((c, ele, (b0, b1)));
                }
                byte_off += s.len() as u32;
            }

            let glyphs = match obj {
                Some(obj) => {
                    // pdfium 会为 TJ 数值间隙插入 `is_generated` 的合成空格字符，
                    // 它们不对应字符串里的字节，配对时剔除。
                    let real: Vec<&crate::pdfium::TextChar> =
                        obj.chars.iter().filter(|c| !c.is_generated).collect();
                    let mut glyphs = Vec::with_capacity(codes.len());
                    for (gi, (code, ele, range)) in codes.iter().enumerate() {
                        let ch = real.get(gi).copied();
                        let unicode: SmallVec<[char; 2]> = ch
                            .and_then(|c| c.unicode.as_deref())
                            .map(|s| s.chars().collect())
                            .unwrap_or_default();
                        let bbox = ch.map(|c| c.bbox).unwrap_or_default();
                        let advance = match &widths {
                            Some(w) => w.advance_pt(*code, fop.font_size),
                            None => ch.map(|c| c.width * fop.font_size / 1000.0).unwrap_or(0.0),
                        };
                        let origin = ch.map(|c| c.origin).unwrap_or(Point::new(bbox.x0, bbox.y0));
                        let matrix = Matrix::translate(origin.x, origin.y);
                        let is_space = unicode.first().is_some_and(|c: &char| c.is_whitespace());
                        let (fill, render_mode) = (obj.fill, obj.render_mode);
                        // pdfium 的正式字号（Tf 原值）优先；快照字号兜底。
                        let size = if obj.unscaled_font_size > 0.0 {
                            obj.unscaled_font_size
                        } else {
                            fop.font_size
                        };
                        glyphs.push(Glyph {
                            id: GlyphId {
                                page: PageId(0),
                                op: fop.key,
                                ordinal: gi as u16,
                            },
                            unicode,
                            code: *code,
                            font: font_idx,
                            size,
                            matrix,
                            bbox,
                            advance,
                            fill,
                            render_mode,
                            source: GlyphSource {
                                element_index: *ele,
                                string_operand_range: *range,
                                decoded_code_range: (gi as u32, gi as u32 + 1),
                            },
                            flags: GlyphFlags {
                                is_space,
                                is_type3: false,
                                invisible: render_mode == 3 || render_mode == 7,
                                outside_clip: false,
                            },
                        });
                    }
                    if codes.len() == real.len() {
                        stats.matched += 1;
                    } else {
                        stats.degraded += 1;
                        issues.push(format!(
                            "count_mismatch op={} stream={} codes={} chars={}",
                            fop.key.op_index,
                            fop.key.stream.obj,
                            codes.len(),
                            real.len()
                        ));
                    }
                    glyphs
                }
                None => {
                    // 降级：自行解码计数，无 unicode / 空 bbox。
                    stats.degraded += 1;
                    issues.push(format!(
                        "no_pdfium_object op={} stream={}",
                        fop.key.op_index, fop.key.stream.obj
                    ));
                    let mut glyphs = Vec::with_capacity(codes.len());
                    for (code, ele, range) in codes.iter() {
                        let advance = widths
                            .as_ref()
                            .map(|w| w.advance_pt(*code, fop.font_size))
                            .unwrap_or(0.0);
                        let ordinal = glyphs.len() as u16;
                        glyphs.push(Glyph {
                            id: GlyphId {
                                page: PageId(0),
                                op: fop.key,
                                ordinal,
                            },
                            unicode: SmallVec::new(),
                            code: *code,
                            font: font_idx,
                            size: fop.font_size,
                            matrix: Matrix::IDENTITY,
                            bbox: Rect::default(),
                            advance,
                            fill: Color::BLACK,
                            render_mode: 0,
                            source: GlyphSource {
                                element_index: *ele,
                                string_operand_range: *range,
                                decoded_code_range: (u32::from(ordinal), u32::from(ordinal) + 1),
                            },
                            flags: GlyphFlags::default(),
                        });
                    }
                    glyphs
                }
            };
            out.insert((fop.key.stream, fop.key.op_index), glyphs);
        }
    }
    out
}

/// 把配对结果并回 `Text` item（按内容流出现次序覆盖空槽）。
fn assign_glyphs(items: &mut [DisplayItem], groups: GlyphGroups) {
    let mut iter = groups.into_values();
    for item in items.iter_mut() {
        if let DisplayItem::Text { glyphs } = item {
            if glyphs.is_empty() {
                if let Some(g) = iter.next() {
                    *glyphs = g;
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// 主入口
// ---------------------------------------------------------------------------

/// 解析一页：把内容流操作与 pdfium 文本对象绑定，产出 [`PageIR`]。
pub fn bind_page(worker: &PdfiumWorker, doc: DocId, lo: &Document, page: u32) -> Result<BoundPage> {
    let pages = lo.get_pages();
    let count = pages.len() as u32;
    let Some(&page_id) = pages.get(&page) else {
        return Err(BindError::PageOutOfRange { page, count });
    };

    let resources = page_resources(lo, page_id);
    let media_box = page_box(lo, page_id, b"MediaBox").unwrap_or(Rect::new(0.0, 0.0, 612.0, 792.0));
    let crop_box = page_box(lo, page_id, b"CropBox").unwrap_or(media_box);
    let rotation = page_rotation(lo, page_id);

    // ---- 字体表：页级（Form 内字体在遍历时并入，见下） ----
    let mut fonts: Vec<FontRef> = Vec::new();
    let mut font_index: BTreeMap<String, u32> = BTreeMap::new();
    collect_font_table(lo, &resources, &mut fonts, &mut font_index);

    // ---- 扁平化 ----
    let mut out = WalkOut {
        flat_text: Vec::new(),
        items: Vec::new(),
        form_dos: Vec::new(),
        issues: Vec::new(),
    };
    let mut ctx = Ctx::new(resources.clone());
    for stream_id in lo.get_page_contents(page_id) {
        let bytes = match lo.get_object(stream_id) {
            Ok(Object::Stream(s)) => s
                .decompressed_content()
                .unwrap_or_else(|_| s.content.clone()),
            _ => {
                out.issues
                    .push(format!("stream {} unreadable", stream_id.0));
                continue;
            }
        };
        walk_stream(lo, stream_id, &bytes, &mut ctx, &[], 0, &mut out);
    }
    // Form 内字体此时才可见：重新扫一遍 items 里的字体资源并补齐表。
    collect_form_fonts(lo, &out.items, page_id, &mut fonts, &mut font_index);

    // ---- pdfium（pages 索引 0 基；本函数入参 page 为 1 基） ----
    let objects = worker.page_text_objects(doc, page - 1)?;
    let mut stats = BindStats {
        text_objects: objects.len() as u32,
        text_ops: out.flat_text.len() as u32,
        ..Default::default()
    };

    // ---- 配对 ----
    let mut objs_by_group: BTreeMap<Vec<u32>, Vec<&TextObject>> = BTreeMap::new();
    for o in &objects {
        objs_by_group
            .entry(o.form_path.clone())
            .or_default()
            .push(o);
    }
    let mut ops_by_group: BTreeMap<Vec<u32>, Vec<&FlatTextOp>> = BTreeMap::new();
    for f in &out.flat_text {
        ops_by_group.entry(f.form_path.clone()).or_default().push(f);
    }

    let groups = bind_glyphs(
        lo,
        &ops_by_group,
        &objs_by_group,
        &font_index,
        &mut stats,
        &mut out.issues,
    );
    assign_glyphs(&mut out.items, groups);

    Ok(BoundPage {
        ir: PageIR {
            page: PageId(page - 1),
            media_box,
            crop_box,
            rotation,
            fonts,
            items: out.items,
        },
        stats,
        issues: out.issues,
        form_dos: out.form_dos,
        page_id,
    })
}

/// 把资源字典里的字体合进页字体表（按资源名去重）。
fn collect_font_table(
    doc: &Document,
    resources: &Dictionary,
    table: &mut Vec<FontRef>,
    index: &mut BTreeMap<String, u32>,
) {
    for (name, id) in font_map(doc, resources) {
        if index.contains_key(&name) {
            continue;
        }
        let d = deref_dict(doc, id);
        let base = d
            .and_then(|d| d.get(b"BaseFont").ok())
            .and_then(name_of)
            .map(|n| strip_subset_prefix(&n))
            .unwrap_or_else(|| name.clone());
        let (is_serif, is_fixed_pitch, is_italic, is_bold) = match d {
            Some(d) => font_traits(doc, d, &base),
            None => (false, false, false, false),
        };
        let idx = table.len() as u32;
        table.push(FontRef {
            resource_name: name.clone(),
            base_font: base,
            is_serif,
            is_fixed_pitch,
            is_italic,
            is_bold,
        });
        index.insert(name, idx);
    }
}

/// 遍历 `FormBegin` 项，把 Form 自己的 `/Resources/Font` 合进字体表。
fn collect_form_fonts(
    doc: &Document,
    items: &[DisplayItem],
    page_id: ObjectId,
    table: &mut Vec<FontRef>,
    index: &mut BTreeMap<String, u32>,
) {
    // 页级 Form 的 Do 记录里只有 Form 对象 id；这里按 items 顺序找 Form 对象较麻烦，
    // 简单起见遍历全部页资源 XObject 里的 Form（少量，代价可接受）。
    let resources = page_resources(doc, page_id);
    for (_, id) in sub_dict(doc, &resources, b"XObject") {
        if let Some(d) = deref_dict(doc, id) {
            let is_form = d
                .get(b"Subtype")
                .ok()
                .and_then(name_of)
                .map(|s| s == "Form")
                .unwrap_or(false);
            if is_form {
                let fres = page_resources_of_form(doc, d);
                collect_font_table(doc, &fres, table, index);
            }
        }
    }
    let _ = items;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_single_bytes() {
        assert_eq!(split_codes(b"abc", EncodingKind::Single), vec![97, 98, 99]);
        assert!(split_codes(b"", EncodingKind::Single).is_empty());
    }

    #[test]
    fn split_double_bytes() {
        assert_eq!(split_codes(&[0x00, 0x41], EncodingKind::Double), vec![0x41]);
        assert_eq!(
            split_codes(&[0x00, 0x41, 0x12, 0x34], EncodingKind::Double),
            vec![0x41, 0x1234]
        );
        // 奇数长度：末字节左移补高位，不 panic。
        assert_eq!(
            split_codes(&[0x00, 0x41, 0x7F], EncodingKind::Double),
            vec![0x41, 0x7F00]
        );
    }

    #[test]
    fn widths_simple_font() {
        let w = FontWidths {
            first_char: 65,
            widths: vec![500.0, 600.0],
            is_type0: false,
            ..Default::default()
        };
        assert_eq!(w.width_1000(65), 500.0);
        assert_eq!(w.width_1000(66), 600.0);
        assert_eq!(w.width_1000(200), 1000.0);
        assert_eq!(w.advance_pt(65, 10.0), 5.0);
    }

    #[test]
    fn widths_type0_dw_and_w() {
        let w = FontWidths {
            dw: 1000.0,
            w: vec![(3412, 800.0)],
            is_type0: true,
            identity: true,
            ..Default::default()
        };
        assert_eq!(w.width_1000(3412), 800.0);
        assert_eq!(w.width_1000(9999), 1000.0);
    }

    #[test]
    fn parse_w_forms() {
        // c [w w] 形式。
        let arr = vec![
            Object::Integer(10),
            Object::Array(vec![Object::Integer(500), Object::Real(600.0)]),
        ];
        let mut out = Vec::new();
        parse_w_array(&arr, &mut out);
        assert_eq!(out, vec![(10, 500.0), (11, 600.0)]);

        // cfirst clast w 形式。
        let arr = vec![Object::Integer(3), Object::Integer(5), Object::Integer(250)];
        let mut out = Vec::new();
        parse_w_array(&arr, &mut out);
        assert_eq!(out, vec![(3, 250.0), (4, 250.0), (5, 250.0)]);
    }

    #[test]
    fn strip_subset() {
        assert_eq!(strip_subset_prefix("ABCDEF+NotoSans"), "NotoSans");
        assert_eq!(strip_subset_prefix("Helvetica"), "Helvetica");
        assert_eq!(strip_subset_prefix("abc+NotBold"), "abc+NotBold");
        assert_eq!(strip_subset_prefix("BOLD+No"), "BOLD+No");
    }

    #[test]
    fn matrix_from_array_ok() {
        let ops = vec![
            Operand::Int(1),
            Operand::Int(0),
            Operand::Int(0),
            Operand::Int(1),
            Operand::Int(10),
            Operand::Int(20),
        ];
        let m = matrix_from_array(&ops).unwrap();
        assert_eq!(m, Matrix::translate(10.0, 20.0));
        assert!(matrix_from_array(&ops[..3]).is_none());
    }

    #[test]
    fn element_indices_tj() {
        let op = Op::new("Tj", vec![Operand::Str(b"ab".to_vec())]);
        assert_eq!(element_indices_of(&op), vec![0]);
        let op = Op::new(
            "TJ",
            vec![Operand::Array(vec![
                Operand::Str(b"a".to_vec()),
                Operand::Int(-200),
                Operand::Str(b"b".to_vec()),
            ])],
        );
        assert_eq!(element_indices_of(&op), vec![0, 2]);
    }

    #[test]
    fn unit_box_transform() {
        let b = unit_box(&Matrix::translate(5.0, 7.0));
        assert_eq!((b.x0, b.y0, b.x1, b.y1), (5.0, 7.0, 6.0, 8.0));
    }

    #[test]
    fn assign_glyphs_fills_empty_text_items() {
        use syncpdf_core::ids::OpKey;
        let mut items = vec![DisplayItem::Text { glyphs: vec![] }];
        let mut groups = GlyphGroups::new();
        groups.insert((ObjRef::new(1, 0), 0u32), vec![]);
        let _ = OpKey::new(ObjRef::new(1, 0), 0);
        assign_glyphs(&mut items, groups);
        match &items[0] {
            DisplayItem::Text { glyphs } => assert!(glyphs.is_empty()),
            _ => panic!("expect text"),
        }
    }
}
