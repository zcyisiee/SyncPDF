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
//! 与 `pdfium::TextObject::form_path` 的分量一一对应（两侧都只数 Form 的进入序号）。
//!
//! # code ↔ pdfium 字符的对齐语义（实测 pdfium 156/8066）
//!
//! pdfium 的文本页每个字符携带**一个** Unicode 码点，与内容流 code 不是一对一：
//!
//! - **合成字符**（`is_generated`，TJ 位移/换行补的空格）没有源字节，不参与配对，
//!   只计入 `BindStats::generated_chars`。它们是“文本提取需要的空格”，不是可删字形。
//! - **连字/一对多映射**（如 Type1 字形 `fi`、Type0 ToUnicode 多码点映射）：一个
//!   code 产生 N 个字符，pdfium 把同一字形的 origin/bbox 复制给每个字符。按
//!   **完全相同的 origin 分簇**后，一簇就是一个 code 的证据。
//! - **空格折叠**：内容流里连续的空格 code 在 pdfium 文本页里合并成一个字符
//!   （实测 `A␣␣B` 只出一个空格字符）。这些 code 有真实字节、可删除，只是没有
//!   独立字符证据；绑定保留它们（unicode 为空格、几何取折叠簇的共享证据）。
//! - 字体有 `/ToUnicode` 时优先用它验证（预期 Unicode、空映射、非常规空格 CID）；
//!   没有时（简单字体常见）用 origin 簇 + 空格 code 启发式（单字节 0x20 等）。
//!
//! `BoundPage::check_replacement` 拒绝已知不可信绑定；通过检查不代表所有 matched
//! 都已获安全认证。source 入口与公共删除 API 均必须调用此门禁。
//!
//! # 降级策略（绝不 panic）
//!
//! - pdfium 文本对象数与 text-show 操作数不等：多出的操作走「无 unicode、空 bbox」
//!   的降级路径，多出的对象忽略，记 issue；
//! - 对齐后存在不可解释事件（无证据 code、剩余簇、与 ToUnicode 不一致）：
//!   `BindStats::degraded` 计数并记 issue；空格折叠本身是已解释行为，只计
//!   `space_collapsed`，不算降级；
//! - 任一流解压/解析失败：跳过该流并记 issue，不影响其余流。

use std::collections::{BTreeMap, BTreeSet};

use lopdf::{Dictionary, Document, Object, ObjectId};
use smallvec::{smallvec, SmallVec};
use syncpdf_core::ir::{DisplayItem, FontRef, Glyph, GlyphFlags, GlyphSource, PageIR};
use syncpdf_core::{Color, GlyphId, Matrix, ObjRef, OpKey, PageId, Point, Rect};

use crate::content::{parse_content, Op, Operand};
use crate::pdfium::{DocId, ObjectBounds, PdfiumWorker, TextChar, TextObject};

/// Form XObject 递归深度上限，防止病态文件爆栈。
const MAX_FORM_DEPTH: usize = 8;

/// 绑定统计。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct BindStats {
    /// pdfium 报告的文本对象数。
    pub text_objects: u32,
    /// 内容流中的 text-show 操作数。
    pub text_ops: u32,
    /// 成功逐字形配对的 text-show 操作数（每个 code 恰好消费一簇 pdfium 字符；
    /// 连字等一对多映射也算匹配——一簇即一个 code 的证据）。
    pub matched: u32,
    /// Single-code Tj operations bound using their own rotated object bounds.
    pub object_geometry_bound_ops: u32,
    /// 走降级路径的操作数（无 pdfium 对象，或对齐后存在不可解释事件）。
    pub degraded: u32,
    /// 依赖「空格折叠」解释的操作数：连续空格 code 只对应一个 pdfium 字符，
    /// 字节真实存在、可删除，只是共享几何证据。不算降级。
    pub space_collapsed: u32,
    /// 一对多 Unicode（连字等）的字形数。
    pub multi_char_glyphs: u32,
    /// 无 pdfium 字符证据且无法解释的字形数（保留可删身份，unicode/几何为空）。
    pub unbound_glyphs: u32,
    /// 过滤掉的 pdfium 合成字符数（`is_generated`，无源字节，不可作为字形删除）。
    pub generated_chars: u32,
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

/// Independent Unicode evidence for an object without text-page characters.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ObjectUnicodeSource {
    ToUnicode,
    /// Explicit simple-font encoding name.
    Encoding(String),
    /// Source Differences name decoded with lopdf's built-in glyph-name table.
    Differences {
        glyph_name: String,
    },
}

/// Read-only provenance for the single-source-code object-bounds path.
#[derive(Debug, Clone, PartialEq)]
pub struct ObjectGeometryEvidence {
    pub glyph_id: GlyphId,
    pub object_index: u32,
    pub form_path: Vec<u32>,
    pub code: u32,
    /// Font resolved in the source operation's resource scope, including generation.
    pub font_id: ObjRef,
    pub byte_range: (u32, u32),
    /// Derived from this object's rotated bounds transformed through ancestor Forms.
    pub object_bounds: ObjectBounds,
    pub unicode: Vec<char>,
    pub unicode_source: ObjectUnicodeSource,
}

/// 绑定结果。
#[derive(Debug, Clone)]
pub struct BoundPage {
    /// 页面 IR。
    pub ir: PageIR,
    pub stats: BindStats,
    /// 降级/异常说明（绑定可返回，但替换门禁拒绝）。
    pub issues: Vec<String>,
    /// 页级与 Form 内的 Form 类型 `Do` 记录，按出现次序。
    pub form_dos: Vec<FormDo>,
    /// 页对象 id。
    pub page_id: ObjectId,
    // Expected code/byte coverage from parsed source operations, independent of IR assembly.
    source_spans: Vec<(OpKey, u32, u32)>,
    object_geometry_evidence: Vec<ObjectGeometryEvidence>,
    source_snapshot: SourceSnapshot,
}

/// Only the page's ordered Contents and the decoded streams visited by binding.
/// Resource and font semantics are outside this snapshot's scope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SourceSnapshot {
    contents: Vec<ObjectId>,
    streams: BTreeMap<ObjectId, Vec<u8>>,
}

impl SourceSnapshot {
    pub(crate) fn matches(&self, doc: &Document, page_id: ObjectId) -> bool {
        doc.get_page_contents(page_id) == self.contents
            && self.streams.iter().all(|(id, expected)| {
                doc.get_object(*id)
                    .ok()
                    .and_then(|object| object.as_stream().ok())
                    .is_some_and(|stream| {
                        stream
                            .decompressed_content()
                            .unwrap_or_else(|_| stream.content.clone())
                            == *expected
                    })
            })
    }
}

/// 已知不可信绑定的机器可读拒绝原因（不是完整绑定认证）。
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ReplacementError {
    #[error("unsafe binding statistics: {0:?}")]
    Statistics(BindStats),
    #[error("missing or inconsistent source coverage for {0:?}")]
    SourceCoverage(OpKey),
    #[error("duplicate source operation: {0:?}")]
    DuplicateSourceOperation(OpKey),
    #[error("binding issues: {0:?}")]
    Issues(Vec<String>),
    #[error("invalid or missing source identity: {0}")]
    SourceIdentity(GlyphId),
    #[error("duplicate source identity: {0}")]
    DuplicateIdentity(GlyphId),
}

impl BoundPage {
    pub(crate) fn source_snapshot(&self) -> &SourceSnapshot {
        &self.source_snapshot
    }

    pub fn object_geometry_evidence(&self) -> &[ObjectGeometryEvidence] {
        &self.object_geometry_evidence
    }

    /// Fail closed on reported anomalies and ambiguous/missing source identities.
    /// This only rejects known unsafe bindings; it does not certify the aligner.
    pub fn check_replacement(&self) -> Result<(), ReplacementError> {
        let mut source_ops = BTreeSet::new();
        for &(op, _, _) in &self.source_spans {
            if !source_ops.insert(op) {
                return Err(ReplacementError::DuplicateSourceOperation(op));
            }
        }
        let s = self.stats;
        if s.degraded != 0
            || s.unbound_glyphs != 0
            || s.text_objects != s.text_ops
            || u64::from(s.matched)
                + u64::from(s.space_collapsed)
                + u64::from(s.object_geometry_bound_ops)
                != u64::from(s.text_ops)
            || s.object_geometry_bound_ops as usize != self.object_geometry_evidence.len()
        {
            return Err(ReplacementError::Statistics(s));
        }
        if !self.issues.is_empty() {
            return Err(ReplacementError::Issues(self.issues.clone()));
        }
        let mut evidence_ops = BTreeSet::new();
        for e in &self.object_geometry_evidence {
            let g = self.ir.glyphs().find(|g| g.id == e.glyph_id);
            if !evidence_ops.insert(e.glyph_id.op)
                || !self
                    .source_spans
                    .contains(&(e.glyph_id.op, 1, e.byte_range.1))
                || e.byte_range.0 != 0
                || g.is_none_or(|g| {
                    g.code != e.code
                        || g.source.element_index != 0
                        || g.source.string_operand_range != e.byte_range
                        || g.bbox != e.object_bounds.bbox
                        || g.matrix
                            != Matrix::translate(e.object_bounds.origin.x, e.object_bounds.origin.y)
                        || g.unicode.as_slice() != e.unicode.as_slice()
                })
            {
                return Err(ReplacementError::SourceIdentity(e.glyph_id));
            }
        }
        let mut ids = BTreeSet::new();
        let mut ends: BTreeMap<OpKey, (u32, u32)> = BTreeMap::new();
        for g in self.ir.glyphs() {
            if !ids.insert(g.id) {
                return Err(ReplacementError::DuplicateIdentity(g.id));
            }
            let (ordinal, byte) = ends.entry(g.id.op).or_default();
            let src = &g.source;
            if g.id.page != self.ir.page
                || u32::from(g.id.ordinal) != *ordinal
                || src.decoded_code_range != (*ordinal, *ordinal + 1)
                || src.string_operand_range.0 != *byte
                || src.string_operand_range.1 <= *byte
            {
                return Err(ReplacementError::SourceIdentity(g.id));
            }
            *ordinal += 1;
            *byte = src.string_operand_range.1;
        }
        for &(op, codes, bytes) in &self.source_spans {
            if ends.get(&op).copied().unwrap_or_default() != (codes, bytes) {
                return Err(ReplacementError::SourceCoverage(op));
            }
        }
        if let Some(op) = ends
            .keys()
            .find(|op| !self.source_spans.iter().any(|s| s.0 == **op))
        {
            return Err(ReplacementError::SourceCoverage(*op));
        }
        Ok(())
    }
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
    is_tj: bool,
    form_path: Vec<u32>,
    key: OpKey,
    /// 显示操作携带的字符串（`TJ` 为数组里的全部字符串，按出现次序）。
    strings: Vec<Vec<u8>>,
    /// 每个字符串操作数在 `TJ` 数组里的下标（`Tj`/`'`/`"` 恒为 0）。
    element_indices: Vec<u32>,
    /// 该操作所在流的文本状态快照。
    font_name: String,
    font_size: f32,
    char_spacing: f32,
    word_spacing: f32,
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
    /// Explicit FontDescriptor /MissingWidth; absent metrics are not guessed.
    pub missing_width: Option<f32>,
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
            self.missing_width.unwrap_or(f32::NAN)
        }
    }

    /// 取某 code 的宽度（pt）。
    pub fn advance_pt(&self, code: u32, size: f32) -> f32 {
        self.width_1000(code) * size / 1000.0
    }
}

/// 解析 `/W` 数组（`c [w...]` 与 `cfirst clast w` 两种形式）。
fn parse_w_array(arr: &[Object], out: &mut Vec<(u32, f32)>) {
    let mut i = 0;
    while i < arr.len() {
        let Ok(start) = arr[i].as_i64() else {
            break;
        };
        let Ok(start) = u32::try_from(start) else {
            break;
        };
        match arr.get(i + 1) {
            Some(Object::Array(widths)) => {
                for (offset, width) in widths.iter().enumerate() {
                    if let Some(code) = start.checked_add(offset as u32) {
                        out.push((code, width.as_float().unwrap_or(f32::NAN)));
                    }
                }
                i += 2;
            }
            Some(Object::Integer(end)) => {
                let Some(width) = arr.get(i + 2) else {
                    break;
                };
                let Ok(end) = u32::try_from(*end) else {
                    break;
                };
                // Valid CIDs fit in two bytes; cap malformed ranges defensively.
                for code in start..=end.min(start.saturating_add(65535)) {
                    out.push((code, width.as_float().unwrap_or(f32::NAN)));
                }
                i += 3;
            }
            _ => break,
        }
    }
}

/// 从 PDF 字体字典解析宽度描述与编码。
pub fn font_widths(doc: &Document, font_id: ObjectId) -> FontWidths {
    let mut out = FontWidths::default();
    let Some(d) = deref_dict(doc, font_id) else {
        return out;
    };
    out.is_type0 = d.get(b"Subtype").ok().and_then(name_of).as_deref() == Some("Type0");
    let resolve = |o: &Object| doc.dereference(o).ok().map(|(_, value)| value.clone());
    let number = |d: &Dictionary, key: &[u8]| {
        d.get(key)
            .ok()
            .and_then(resolve)
            .and_then(|o| o.as_float().ok())
    };
    if out.is_type0 {
        out.identity = true;
        let descendant = d
            .get(b"DescendantFonts")
            .ok()
            .and_then(resolve)
            .and_then(|o| o.as_array().ok().and_then(|a| a.first().cloned()))
            .and_then(|o| resolve(&o))
            .and_then(|o| o.as_dict().ok().cloned());
        if let Some(cid) = descendant.as_ref() {
            out.dw = number(cid, b"DW").unwrap_or(1000.0);
            if let Some(Object::Array(arr)) = cid.get(b"W").ok().and_then(resolve) {
                let arr: Vec<_> = arr
                    .iter()
                    .map(|o| match resolve(o) {
                        Some(Object::Array(widths)) => Object::Array(
                            widths
                                .iter()
                                .map(|w| resolve(w).unwrap_or(Object::Null))
                                .collect(),
                        ),
                        Some(value) => value,
                        None => Object::Null,
                    })
                    .collect();
                parse_w_array(&arr, &mut out.w);
            }
        } else {
            out.dw = f32::NAN;
        }
    } else {
        out.first_char = number(d, b"FirstChar").unwrap_or(0.0) as i64;
        if let Some(Object::Array(arr)) = d.get(b"Widths").ok().and_then(resolve) {
            // Keep indices even for invalid values; never shift later code widths.
            out.widths = arr
                .iter()
                .map(|o| {
                    resolve(o)
                        .and_then(|v| v.as_float().ok())
                        .unwrap_or(f32::NAN)
                })
                .collect();
        }
        if d.get(b"Widths")
            .ok()
            .and_then(resolve)
            .is_some_and(|o| matches!(o, Object::Array(_)))
        {
            out.missing_width = Some(
                d.get(b"FontDescriptor")
                    .ok()
                    .and_then(resolve)
                    .and_then(|o| o.as_dict().ok().and_then(|d| number(d, b"MissingWidth")))
                    .unwrap_or(0.0),
            );
        }
        if d.get(b"Subtype").ok().and_then(name_of).as_deref() == Some("Type3") {
            // Type3 widths live in glyph space, transformed by FontMatrix.
            // Horizontal replacement only supports a finite positive x scale.
            let scale = d
                .get(b"FontMatrix")
                .ok()
                .and_then(resolve)
                .and_then(|o| o.as_array().ok().cloned())
                .filter(|a| a.len() == 6)
                .and_then(|a| {
                    let m: Option<Vec<_>> = a.iter().map(|o| o.as_float().ok()).collect();
                    let m = m?;
                    (m.iter().all(|v| v.is_finite())
                        && m[0] > 0.0
                        && m[3] != 0.0
                        && m[1] == 0.0
                        && m[2] == 0.0)
                        .then_some(m[0] * 1000.0)
                })
                .unwrap_or(f32::NAN);
            for width in &mut out.widths {
                *width = (*width * scale).round();
            }
            out.missing_width = out.missing_width.map(|w| (w * scale).round());
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

// ---------------------------------------------------------------------------
// ToUnicode CMap（纯函数）
// ---------------------------------------------------------------------------

/// 字体 `/ToUnicode` 查表：code → Unicode 串（空串 = 明确无映射）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ToUnicodeMap {
    entries: BTreeMap<u32, Vec<char>>,
    conflicts: BTreeSet<u32>,
}

impl ToUnicodeMap {
    /// 查 code 的 Unicode；`None` = 表里没有该 code（区别于空映射）。
    pub fn get(&self, code: u32) -> Option<&[char]> {
        self.entries.get(&code).map(Vec::as_slice)
    }

    fn insert(&mut self, code: u32, chars: Vec<char>) {
        if self.entries.get(&code).is_some_and(|old| old != &chars) {
            self.conflicts.insert(code);
        }
        self.entries.insert(code, chars);
    }
}

/// 解析 ToUnicode CMap 字节流（bfchar / bfrange 两种形式，含数组形式）。
///
/// 只需要 code→Unicode，codespace 与其它段落忽略；畸形片段跳过不致命。
pub fn parse_to_unicode_cmap(data: &[u8]) -> ToUnicodeMap {
    #[derive(Debug, Clone)]
    enum Tok {
        Hex(Vec<u8>),
        Word(String),
        ArrayOpen,
        ArrayClose,
    }
    fn tokenize(s: &[u8]) -> Vec<Tok> {
        let mut out = Vec::new();
        let mut i = 0usize;
        while i < s.len() {
            match s[i] {
                b'%' => {
                    while i < s.len() && s[i] != b'\n' && s[i] != b'\r' {
                        i += 1;
                    }
                }
                b'<' => {
                    if s.get(i + 1) == Some(&b'<') {
                        out.push(Tok::Word("<<".into()));
                        i += 2;
                    } else {
                        let mut hex = Vec::new();
                        let mut hi: Option<u8> = None;
                        i += 1;
                        while i < s.len() && s[i] != b'>' {
                            let c = s[i];
                            if let Some(v) = (c as char).to_digit(16) {
                                match hi.take() {
                                    Some(h) => hex.push((h << 4) | v as u8),
                                    None => hi = Some(v as u8),
                                }
                            }
                            i += 1;
                        }
                        i += 1; // '>'（可能越界，循环条件已护住）
                        if let Some(h) = hi {
                            hex.push(h << 4);
                        }
                        out.push(Tok::Hex(hex));
                    }
                }
                b'[' => {
                    out.push(Tok::ArrayOpen);
                    i += 1;
                }
                b']' => {
                    out.push(Tok::ArrayClose);
                    i += 1;
                }
                b'(' => {
                    // 字符串字面量（ToUnicode 一般不用，跳过嵌套转义）。
                    let mut depth = 1;
                    i += 1;
                    while i < s.len() && depth > 0 {
                        match s[i] {
                            b'\\' => i += 1,
                            b'(' => depth += 1,
                            b')' => depth -= 1,
                            _ => {}
                        }
                        i += 1;
                    }
                }
                c if c.is_ascii_whitespace() => i += 1,
                c if c.is_ascii_alphanumeric() || c == b'/' || c == b'#' || c == b'_' => {
                    let start = i;
                    while i < s.len()
                        && (s[i].is_ascii_alphanumeric()
                            || matches!(s[i], b'/' | b'#' | b'_' | b'.' | b'+' | b'-'))
                    {
                        i += 1;
                    }
                    out.push(Tok::Word(
                        String::from_utf8_lossy(&s[start..i]).into_owned(),
                    ));
                }
                _ => i += 1,
            }
        }
        out
    }

    fn hex_to_code(h: &[u8]) -> Option<u32> {
        if h.is_empty() || h.len() > 4 {
            return None;
        }
        Some(h.iter().fold(0u32, |acc, b| (acc << 8) | u32::from(*b)))
    }

    fn hex_to_chars(h: &[u8]) -> Vec<char> {
        if h.is_empty() || h.len() % 2 != 0 {
            return Vec::new();
        }
        let units: Vec<u16> = h
            .chunks(2)
            .map(|c| u16::from_be_bytes([c[0], c[1]]))
            .collect();
        String::from_utf16_lossy(&units).chars().collect()
    }

    let toks = tokenize(data);
    let mut map = ToUnicodeMap::default();
    let mut i = 0usize;
    while i < toks.len() {
        match &toks[i] {
            Tok::Word(w) if w == "beginbfchar" => {
                i += 1;
                while i + 1 < toks.len() && !matches!(toks[i], Tok::Word(ref w) if w == "endbfchar")
                {
                    if let (Tok::Hex(src), Tok::Hex(dst)) = (&toks[i], &toks[i + 1]) {
                        if let Some(code) = hex_to_code(src) {
                            map.insert(code, hex_to_chars(dst));
                        }
                        i += 2;
                    } else {
                        i += 1;
                    }
                }
            }
            Tok::Word(w) if w == "beginbfrange" => {
                i += 1;
                while i < toks.len() && !matches!(toks[i], Tok::Word(ref w) if w == "endbfrange") {
                    // <lo> <hi> <dst> 或 <lo> <hi> [<d>...]
                    if i + 2 < toks.len() {
                        if let (Tok::Hex(lo), Tok::Hex(hi)) = (&toks[i], &toks[i + 1]) {
                            let (Some(lo), Some(hi)) = (hex_to_code(lo), hex_to_code(hi)) else {
                                i += 1;
                                continue;
                            };
                            if hi < lo || hi - lo > 65535 {
                                i += 3;
                                continue;
                            }
                            match &toks[i + 2] {
                                Tok::Hex(dst) => {
                                    // PDF ranges increment only the final byte, without carry.
                                    // Decode the whole UTF-16 string so surrogate pairs stay intact.
                                    if dst.len() >= 2 && dst.len() % 2 == 0 {
                                        let mut target = dst.clone();
                                        let last_index = target.len() - 1;
                                        let last = target[last_index];
                                        let max_offset = (hi - lo).min(u32::from(u8::MAX - last));
                                        for k in 0..=max_offset {
                                            target[last_index] = last + k as u8;
                                            map.insert(lo + k, hex_to_chars(&target));
                                        }
                                    }
                                    i += 3;
                                }
                                Tok::ArrayOpen => {
                                    // 数组形式：逐个目的地串对应区间内的 code。
                                    let mut j = i + 3;
                                    let mut code = lo;
                                    while j < toks.len() && code <= hi {
                                        match &toks[j] {
                                            Tok::Hex(dst) => {
                                                map.insert(code, hex_to_chars(dst));
                                                j += 1;
                                                if code == hi {
                                                    break;
                                                }
                                                code += 1;
                                            }
                                            Tok::ArrayClose => break,
                                            _ => {
                                                j += 1;
                                            }
                                        }
                                    }
                                    i = j;
                                }
                                _ => i += 1,
                            }
                        } else {
                            i += 1;
                        }
                    } else {
                        i += 1;
                    }
                }
            }
            _ => i += 1,
        }
    }
    map
}

/// 从字体字典取 `/ToUnicode` 并解析；缺失或不可读返回空表。
fn to_unicode_of(doc: &Document, font_id: ObjectId) -> ToUnicodeMap {
    let Some(d) = deref_dict(doc, font_id) else {
        return ToUnicodeMap::default();
    };
    let Some(id) = dict_ref(d, b"ToUnicode") else {
        return ToUnicodeMap::default();
    };
    match doc.get_object(id) {
        Ok(Object::Stream(s)) => {
            let bytes = s
                .decompressed_content()
                .unwrap_or_else(|_| s.content.clone());
            parse_to_unicode_cmap(&bytes)
        }
        _ => ToUnicodeMap::default(),
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
    stream_bytes: BTreeMap<ObjectId, Vec<u8>>,
}

#[allow(clippy::too_many_arguments)]
fn walk_stream(
    doc: &Document,
    stream_id: ObjectId,
    bytes: &[u8],
    ctx: &mut Ctx,
    form_path: &[u32],
    depth: usize,
    form_seq: &mut u32,
    out: &mut WalkOut,
) {
    out.stream_bytes.insert(stream_id, bytes.to_vec());
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
                handle_do(doc, &mut child, key, &name, form_path, depth, form_seq, out);
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
                    if op.operator == "\"" {
                        set_f32(&op.operands, 0, &mut ctx.word_spacing);
                        set_f32(&op.operands, 1, &mut ctx.char_spacing);
                    }
                    let strings: Vec<Vec<u8>> =
                        op.text_strings().into_iter().map(|s| s.to_vec()).collect();
                    out.flat_text.push(FlatTextOp {
                        is_tj: op.operator == "Tj",
                        form_path: form_path.to_vec(),
                        key,
                        strings,
                        element_indices: element_indices_of(op),
                        font_name: ctx.font_name.clone(),
                        font_size: ctx.font_size,
                        char_spacing: ctx.char_spacing,
                        word_spacing: ctx.word_spacing,
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
    child.char_spacing = ctx.char_spacing;
    child.word_spacing = ctx.word_spacing;
    child.h_scale = ctx.h_scale;

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
            &mut 0, // Each child Form starts a new object-list numbering scope.
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

/// 一个字体的编码 + 宽度 + ToUnicode 查表。
#[derive(Debug, Clone)]
struct FontEncoding {
    kind: EncodingKind,
    widths: Option<FontWidths>,
    to_uni: ToUnicodeMap,
}

/// 编码类别：Type0 Identity → 双字节。
fn encoding_of(doc: &Document, font_id: Option<ObjectId>) -> FontEncoding {
    match font_id {
        Some(id) => {
            let w = font_widths(doc, id);
            let kind = if w.is_type0 && w.identity {
                EncodingKind::Double
            } else {
                EncodingKind::Single
            };
            FontEncoding {
                kind,
                widths: Some(w),
                to_uni: to_unicode_of(doc, id),
            }
        }
        None => FontEncoding {
            kind: EncodingKind::Single,
            widths: None,
            to_uni: ToUnicodeMap::default(),
        },
    }
}

/// 该 code 是否是空格类（空格折叠判定用）。
///
/// 无 ToUnicode 时的启发式：单字节 0x20；双字节取常见空格 CID（Identity-H 的 1、
/// WinAnsi 派生 CMap 的 0x20、CJK 全角空格 0x2000/0x3000）。有 ToUnicode 时
/// 由映射直接判定，不走本函数。
fn is_space_code(code: u32, kind: EncodingKind) -> bool {
    match kind {
        EncodingKind::Single => code == 0x20,
        EncodingKind::Double => matches!(code, 0x0020 | 0x0001 | 0x2000 | 0x3000),
    }
}

/// 按完全相同的 origin 把非生成字符分簇：pdfium 对一对多映射（连字等）会把
/// 同一字形的 origin/bbox 复制给每个 Unicode 字符，一簇即一个 code 的证据。
fn cluster_by_origin<'c>(real: &'c [&'c TextChar]) -> Vec<&'c [&'c TextChar]> {
    let mut out: Vec<&[&TextChar]> = Vec::new();
    let mut start = 0usize;
    for i in 1..=real.len() {
        if i == real.len() || real[i].origin != real[i - 1].origin {
            out.push(&real[start..i]);
            start = i;
        }
    }
    out
}

/// 簇的 Unicode 拼接（无映射字符贡献空串）。
fn cluster_chars(cluster: &[&TextChar]) -> SmallVec<[char; 2]> {
    cluster
        .iter()
        .flat_map(|c| c.unicode.as_deref().unwrap_or("").chars())
        .collect()
}

/// 簇是否是纯空白（至少一个字符且全部空白）。
fn cluster_is_whitespace(cluster: &[&TextChar]) -> bool {
    !cluster.is_empty()
        && cluster.iter().all(|c| {
            c.unicode
                .as_deref()
                .is_some_and(|u| !u.is_empty() && u.chars().all(char::is_whitespace))
        })
}

/// 簇是否无可用的 Unicode：pdfium 拿不到映射（None，如星面字符的 UTF-16
/// 代理对两半），或回退到原码点的控制字符（实测 CharisSIL 行尾连字给
/// U+0002）。这样的簇只有几何证据，身份应回退到 ToUnicode。
fn cluster_unmapped(cluster: &[&TextChar]) -> bool {
    cluster.iter().all(|c| match c.unicode.as_deref() {
        None => true,
        Some(u) => u.chars().all(|ch| (ch as u32) < 0x20),
    })
}

/// 一次对齐的单次决策结果。
#[derive(Debug, Clone, Copy)]
struct AlignStep {
    /// 消费的簇下标；`None` = 无证据（折叠或未绑定）。
    cluster: Option<usize>,
    /// 该 code 被判定为空格折叠（真实字节，无独立字符）。
    collapsed: bool,
    /// pdfium 证据与 ToUnicode 预期不一致（不消费簇，防错位）。
    mismatch: bool,
}

/// Empty mappings may emit a raw-code control character or no character at all.
/// Accept only a unique, complete ordered alignment, with a bounded state table.
/// Unknown Unicode and arbitrary controls are not identity evidence in this path.
fn align_with_empty_mappings(
    codes: &[(u32, u32, (u32, u32))],
    to_uni: &ToUnicodeMap,
    clusters: &[&[&TextChar]],
) -> Vec<AlignStep> {
    let rejected = vec![
        AlignStep {
            cluster: None,
            collapsed: false,
            mismatch: true
        };
        codes.len()
    ];
    let rows = codes.len() + 1;
    let cols = clusters.len() + 1;
    if rows.checked_mul(cols).is_none_or(|n| n > 65_536) {
        return rejected;
    }
    let chars: Vec<_> = clusters.iter().map(|c| cluster_chars(c)).collect();
    let accepts = |i: usize, j: usize| {
        let code = codes[i].0;
        let raw_control = code < 0x20
            && clusters[j].len() == 1
            && chars[j].as_slice() == [char::from_u32(code).unwrap()];
        match to_uni.get(code) {
            Some(exp) if !exp.is_empty() => chars[j].as_slice() == exp || raw_control,
            Some(_) => raw_control,
            None => false,
        }
    };
    // Saturated path counts distinguish impossible, unique, and ambiguous suffixes.
    let mut paths = vec![0u8; rows * cols];
    paths[codes.len() * cols + clusters.len()] = 1;
    for i in (0..codes.len()).rev() {
        for j in (0..=clusters.len()).rev() {
            let mut count = 0;
            if to_uni.get(codes[i].0).is_some_and(|e| e.is_empty()) {
                count = paths[(i + 1) * cols + j];
            }
            if j < clusters.len() && accepts(i, j) {
                count = (count + paths[(i + 1) * cols + j + 1]).min(2);
            }
            paths[i * cols + j] = count;
        }
    }
    if paths[0] != 1 {
        return rejected;
    }
    let mut j = 0;
    codes
        .iter()
        .enumerate()
        .map(|(i, (code, _, _))| {
            let empty = to_uni.get(*code).is_some_and(|e| e.is_empty());
            let consume = j < clusters.len() && accepts(i, j) && paths[(i + 1) * cols + j + 1] == 1;
            let cluster = (consume && !empty).then_some(j);
            if consume {
                j += 1;
            }
            // Empty mappings remain unbound even when their placeholder was consumed.
            AlignStep {
                cluster,
                collapsed: false,
                mismatch: false,
            }
        })
        .collect()
}

/// 一个操作的逐 code 对齐：每个 code 独立决策，不会因一处一对多/折叠让
/// 后续整段移位。
fn align_codes_to_clusters(
    codes: &[(u32, u32, (u32, u32))],
    kind: EncodingKind,
    to_uni: &ToUnicodeMap,
    clusters: &[&[&TextChar]],
) -> Vec<AlignStep> {
    if codes
        .iter()
        .any(|(code, _, _)| to_uni.get(*code).is_some_and(|e| e.is_empty()))
    {
        return align_with_empty_mappings(codes, to_uni, clusters);
    }
    let mut out = Vec::with_capacity(codes.len());
    let mut ki = 0usize;
    for (code, _, _) in codes.iter() {
        let expected = to_uni.get(*code);
        let mut step = AlignStep {
            cluster: None,
            collapsed: false,
            mismatch: false,
        };
        if let Some(exp) = expected {
            // 有 ToUnicode 预期：按预期验证。
            if exp.is_empty() {
                // 明确无映射：不消费簇（pdfium 通常也不出字符）。
            } else if ki < clusters.len() {
                let cu = cluster_chars(clusters[ki]);
                let exp_ws = exp.iter().copied().all(char::is_whitespace);
                let cu_ws = cluster_is_whitespace(clusters[ki]);
                // 全 None/控制字符簇：pdfium 渲染出了字符但拿不到可用
                // Unicode（如星面字符的 UTF-16 代理对两半、无映射回退到
                // 原码点）。证据仍属于本 code，身份用 ToUnicode，几何用簇。
                let cu_unmapped = cluster_unmapped(clusters[ki]);
                if cu.as_slice() == exp || cu_unmapped {
                    step.cluster = Some(ki);
                    ki += 1;
                } else if exp_ws {
                    if cu_ws {
                        step.cluster = Some(ki);
                        ki += 1;
                    } else {
                        // 空格折叠：预期是空白但下一簇不是 → 本 code 无独立字符。
                        step.collapsed = true;
                    }
                } else {
                    // 证据与预期不一致：不消费簇（留给后续 code 恢复对齐），
                    // unicode 回退用 ToUnicode（源侧真相），几何留空。
                    step.mismatch = true;
                }
            } else if exp.iter().copied().all(char::is_whitespace) {
                step.collapsed = true;
            } else {
                step.mismatch = true;
            }
        } else if ki < clusters.len() {
            // 无 ToUnicode（或该 code 不在表内）：origin 簇 + 空格启发式。
            let cu_ws = cluster_is_whitespace(clusters[ki]);
            if is_space_code(*code, kind) && !cu_ws {
                step.collapsed = true;
            } else {
                step.cluster = Some(ki);
                ki += 1;
            }
        } else if is_space_code(*code, kind) {
            step.collapsed = true;
        }
        out.push(step);
    }
    out
}

fn differences_glyph<'a>(
    doc: &'a Document,
    font: &Dictionary,
    d: &'a Dictionary,
    code: u32,
) -> Option<&'a Vec<u8>> {
    if let Ok(kind) = d.get(b"Type") {
        if kind.as_name().ok()? != b"Encoding" {
            return None;
        }
    }
    if let Ok(base) = d.get(b"BaseEncoding") {
        if !matches!(
            base.as_name().ok()?,
            b"StandardEncoding" | b"WinAnsiEncoding" | b"MacRomanEncoding" | b"MacExpertEncoding"
        ) {
            return None;
        }
    }
    let differences = d.get(b"Differences").ok()?.as_array().ok()?;
    let mut next = None;
    let mut names = BTreeMap::new();
    for entry in differences {
        match entry {
            Object::Integer(n) if (0..=255).contains(n) => next = Some(*n as u32),
            Object::Name(name) => {
                let n = next.filter(|n| *n <= 255)?;
                if names.insert(n, name).is_some() {
                    return None;
                }
                next = Some(n + 1);
            }
            _ => return None,
        }
    }
    let name = *names.get(&code)?;
    if font.get(b"Subtype").ok()?.as_name().ok()? == b"Type3" {
        let procs = font.get_deref(b"CharProcs", doc).ok()?.as_dict().ok()?;
        procs.get_deref(name, doc).ok()?.as_stream().ok()?;
    }
    Some(name)
}

// Deliberately narrower than the normal character-backed path. No default encoding guesses.
fn object_unicode(
    doc: &Document,
    font_id: Option<ObjectId>,
    code: u32,
    bytes: &[u8],
) -> Option<(Vec<char>, ObjectUnicodeSource)> {
    let font = deref_dict(doc, font_id?)?;
    let subtype = font.get(b"Subtype").ok()?.as_name().ok()?;
    if !matches!(
        subtype,
        b"Type0" | b"Type1" | b"MMType1" | b"TrueType" | b"Type3"
    ) {
        return None;
    }
    // Existing character-backed decoding is permissive; this path requires proven code width.
    if subtype == b"Type0" {
        if bytes.len() != 2
            || font.get_deref(b"Encoding", doc).ok()?.as_name().ok()? != b"Identity-H"
        {
            return None;
        }
    } else if bytes.len() != 1 {
        return None;
    }
    if subtype == b"Type3" {
        let encoding = font.get_deref(b"Encoding", doc).ok()?.as_dict().ok()?;
        differences_glyph(doc, font, encoding, code)?;
    }
    if font.has(b"ToUnicode") {
        let mut only_unicode = font.clone();
        only_unicode.remove(b"Encoding");
        let decoded = only_unicode.get_font_encoding(doc).ok()?;
        if !matches!(decoded, lopdf::Encoding::UnicodeMapEncoding(_)) {
            return None;
        }
        let text = decoded.bytes_to_string(bytes).ok()?;
        let map = to_unicode_of(doc, font_id?);
        let chars: Vec<_> = text.chars().collect();
        if chars.is_empty()
            || chars.iter().any(|c| c.is_control() || *c == '\u{fffd}')
            || map.conflicts.contains(&code)
            || map.get(code)? != chars.as_slice()
        {
            return None;
        }
        return Some((chars, ObjectUnicodeSource::ToUnicode));
    }
    if bytes.len() != 1 || font.get(b"Subtype").ok()?.as_name().ok()? == b"Type0" {
        return None;
    }
    let encoding = font.get_deref(b"Encoding", doc).ok()?;
    let (text, source) = match encoding {
        Object::Name(name)
            if matches!(
                name.as_slice(),
                b"StandardEncoding"
                    | b"WinAnsiEncoding"
                    | b"MacRomanEncoding"
                    | b"MacExpertEncoding"
            ) =>
        {
            // Type3 must identify and verify the actual CharProc through Differences.
            if font.get(b"Subtype").ok()?.as_name().ok()? == b"Type3" {
                return None;
            }
            (
                font.get_font_encoding(doc)
                    .ok()?
                    .bytes_to_string(bytes)
                    .ok()?,
                ObjectUnicodeSource::Encoding(String::from_utf8(name.clone()).ok()?),
            )
        }
        Object::Dictionary(d) => {
            let name = differences_glyph(doc, font, d, code)?;
            // Query only the named glyph in a synthetic dictionary; never use its default encoding.
            let mut lookup_encoding = Dictionary::new();
            lookup_encoding.set("Type", Object::Name(b"Encoding".to_vec()));
            lookup_encoding.set(
                "Differences",
                vec![Object::Integer(0), Object::Name(name.clone())],
            );
            let mut lookup_font = Dictionary::new();
            lookup_font.set("Type", Object::Name(b"Font".to_vec()));
            lookup_font.set("Encoding", lookup_encoding);
            let decoded = lookup_font.get_font_encoding(doc).ok()?;
            if !matches!(decoded, lopdf::Encoding::Differences(_)) {
                return None;
            }
            (
                decoded.bytes_to_string(&[0]).ok()?,
                ObjectUnicodeSource::Differences {
                    glyph_name: String::from_utf8(name.clone()).ok()?,
                },
            )
        }
        _ => return None,
    };
    let chars: Vec<_> = text.chars().collect();
    (!chars.is_empty() && chars.iter().all(|c| !c.is_control() && *c != '\u{fffd}'))
        .then_some((chars, source))
}

#[allow(clippy::too_many_arguments)]
fn bind_glyphs(
    doc: &Document,
    ops_by_group: &BTreeMap<Vec<u32>, Vec<&FlatTextOp>>,
    objs_by_group: &BTreeMap<Vec<u32>, Vec<&TextObject>>,
    font_index: &BTreeMap<String, u32>,
    stats: &mut BindStats,
    issues: &mut Vec<String>,
    evidence: &mut Vec<ObjectGeometryEvidence>,
) -> GlyphGroups {
    let mut out = GlyphGroups::new();
    for (path, ops) in ops_by_group {
        let objs = objs_by_group.get(path);
        for (i, fop) in ops.iter().enumerate() {
            let obj = objs.and_then(|v| v.get(i)).copied();
            let enc = encoding_of(doc, fop.font_id);
            let font_idx = font_index.get(&fop.font_name).copied().unwrap_or(0);

            // 把该操作的字符串拼成 (code, element_index, byte_range) 序列。
            let w = code_width(enc.kind);
            let mut codes: Vec<(u32, u32, (u32, u32))> = Vec::new();
            let mut byte_off = 0u32;
            for (si, s) in fop.strings.iter().enumerate() {
                let ele = *fop.element_indices.get(si).unwrap_or(&0);
                for (ci, c) in split_codes(s, enc.kind).into_iter().enumerate() {
                    let b0 = byte_off + (ci * w) as u32;
                    let avail = s.len().saturating_sub(ci * w).min(w);
                    let b1 = b0 + avail as u32;
                    codes.push((c, ele, (b0, b1)));
                }
                byte_off += s.len() as u32;
            }

            let glyphs = match obj {
                Some(obj) => {
                    // pdfium 会为 TJ 位移/换行插入 `is_generated` 的合成字符，
                    // 它们没有源字节，不参与配对。
                    let real: Vec<&TextChar> =
                        obj.chars.iter().filter(|c| !c.is_generated).collect();
                    stats.generated_chars += (obj.chars.len() - real.len()) as u32;
                    let recovery = if real.is_empty()
                        && fop.is_tj
                        && codes.len() == 1
                        && fop.strings.len() == 1
                        && fop.strings[0].len() == w
                        && objs.is_some_and(|o| o.len() == ops.len())
                    {
                        fop.font_id.and_then(|font_id| {
                            obj.object_bounds.and_then(|geometry| {
                                object_unicode(doc, Some(font_id), codes[0].0, &fop.strings[0]).map(
                                    |(unicode, source)| {
                                        (
                                            geometry,
                                            unicode,
                                            source,
                                            ObjRef::new(font_id.0, font_id.1),
                                        )
                                    },
                                )
                            })
                        })
                    } else {
                        None
                    };
                    let clusters = cluster_by_origin(&real);
                    let steps = align_codes_to_clusters(&codes, enc.kind, &enc.to_uni, &clusters);

                    let mut glyphs = Vec::with_capacity(codes.len());
                    let mut n_collapsed = 0u32;
                    let mut n_unbound = 0u32;
                    let mut n_mismatch = 0u32;
                    let mut n_multi = 0u32;
                    // 折叠空格可共享最近一次消费的空白簇作为几何证据。
                    let mut last_ws_cluster: Option<usize> = None;
                    for (gi, ((code, ele, range), step)) in
                        codes.iter().zip(steps.iter()).enumerate()
                    {
                        let exp = enc.to_uni.get(*code);
                        // unicode：簇证据 → ToUnicode 预期 → 折叠空格 → 空。
                        let unicode: SmallVec<[char; 2]> =
                            if let Some((_, unicode, _, _)) = &recovery {
                                SmallVec::from(unicode.as_slice())
                            } else if let Some(ki) = step.cluster {
                                let cu = cluster_chars(clusters[ki]);
                                if cluster_is_whitespace(clusters[ki]) {
                                    last_ws_cluster = Some(ki);
                                }
                                if cu.is_empty() || cluster_unmapped(clusters[ki]) {
                                    // None/控制字符簇：pdfium 几何证据在，
                                    // 身份用 ToUnicode 预期。
                                    exp.filter(|e| !e.is_empty())
                                        .map(SmallVec::from)
                                        .unwrap_or(cu)
                                } else {
                                    cu
                                }
                            } else if let Some(exp) = exp.filter(|e| !e.is_empty()) {
                                SmallVec::from(exp)
                            } else if step.collapsed {
                                smallvec![' ']
                            } else {
                                SmallVec::new()
                            };
                        if unicode.len() > 1 {
                            n_multi += 1;
                        }

                        // 几何：本 code 消费的簇；折叠空格共享最近空白簇。
                        let geom = step
                            .cluster
                            .or(if step.collapsed {
                                last_ws_cluster
                            } else {
                                None
                            })
                            .map(|ki| clusters[ki][0]);
                        let bbox = recovery
                            .as_ref()
                            .map(|r| r.0.bbox)
                            .or_else(|| geom.map(|c| c.bbox))
                            .unwrap_or_default();
                        let origin = recovery
                            .as_ref()
                            .map(|r| r.0.origin)
                            .or_else(|| geom.map(|c| c.origin))
                            .unwrap_or(Point::new(bbox.x0, bbox.y0));
                        let advance = match &enc.widths {
                            Some(w) => w.advance_pt(*code, fop.font_size),
                            None => f32::NAN,
                        };
                        let advance = advance
                            + fop.char_spacing
                            + if matches!(enc.kind, EncodingKind::Single) && *code == 32 {
                                fop.word_spacing
                            } else {
                                0.0
                            };
                        let matrix = Matrix::translate(origin.x, origin.y);
                        let is_space = unicode.first().is_some_and(|c: &char| c.is_whitespace());
                        let (fill, render_mode) = (obj.fill, obj.render_mode);
                        // pdfium 的正式字号（Tf 原值）优先；快照字号兜底。
                        let size = if obj.unscaled_font_size > 0.0 {
                            obj.unscaled_font_size
                        } else {
                            fop.font_size
                        };
                        if step.cluster.is_none() && recovery.is_none() {
                            if step.collapsed {
                                n_collapsed += 1;
                            } else {
                                n_unbound += 1;
                            }
                        }
                        if step.mismatch && recovery.is_none() {
                            n_mismatch += 1;
                        }
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
                                is_type3: fop
                                    .font_id
                                    .and_then(|id| deref_dict(doc, id))
                                    .and_then(|d| d.get(b"Subtype").ok())
                                    .and_then(|s| s.as_name().ok())
                                    == Some(b"Type3"),
                                invisible: render_mode == 3 || render_mode == 7,
                                outside_clip: false,
                            },
                        });
                    }
                    let leftovers = clusters.len() as u32
                        - steps.iter().filter(|s| s.cluster.is_some()).count() as u32;
                    stats.multi_char_glyphs += n_multi;
                    if let Some((object_bounds, unicode, unicode_source, font_id)) = recovery {
                        stats.object_geometry_bound_ops += 1;
                        evidence.push(ObjectGeometryEvidence {
                            glyph_id: glyphs[0].id,
                            object_index: obj.index,
                            form_path: obj.form_path.clone(),
                            code: codes[0].0,
                            font_id,
                            byte_range: codes[0].2,
                            object_bounds,
                            unicode,
                            unicode_source,
                        });
                    } else if n_unbound == 0 && leftovers == 0 && n_mismatch == 0 {
                        if n_collapsed > 0 {
                            stats.space_collapsed += 1;
                        } else {
                            stats.matched += 1;
                        }
                    } else {
                        stats.degraded += 1;
                        stats.unbound_glyphs += n_unbound;
                        issues.push(format!(
                            "align op={} stream={} codes={} clusters={} collapsed={} unbound={} leftover={} to_unicode_mismatch={}",
                            fop.key.op_index,
                            fop.key.stream.obj,
                            codes.len(),
                            clusters.len(),
                            n_collapsed,
                            n_unbound,
                            leftovers,
                            n_mismatch
                        ));
                    }
                    glyphs
                }
                None => {
                    // 降级：无 pdfium 对象，自行解码计数，无 unicode / 空 bbox。
                    stats.degraded += 1;
                    stats.unbound_glyphs += codes.len() as u32;
                    issues.push(format!(
                        "no_pdfium_object op={} stream={}",
                        fop.key.op_index, fop.key.stream.obj
                    ));
                    let mut glyphs = Vec::with_capacity(codes.len());
                    for (code, ele, range) in codes.iter() {
                        let advance = enc
                            .widths
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
            let key = (fop.key.stream, fop.key.op_index);
            if let std::collections::btree_map::Entry::Vacant(entry) = out.entry(key) {
                entry.insert(glyphs);
            } else {
                issues.push(format!("duplicate source operation: {:?}", fop.key));
            }
        }
    }
    out
}

/// 把配对结果并回 `Text` item（按内容流出现次序覆盖空槽）。
fn assign_glyphs(
    items: &mut [DisplayItem],
    keys: &[OpKey],
    mut groups: GlyphGroups,
    issues: &mut Vec<String>,
) {
    let mut keys = keys.iter();
    for item in items {
        if let DisplayItem::Text { glyphs } = item {
            let Some(key) = keys.next() else {
                issues.push("text slot missing source operation".into());
                continue;
            };
            match groups.remove(&(key.stream, key.op_index)) {
                Some(group) => *glyphs = group,
                None => issues.push(format!("text slot missing glyph group: {key:?}")),
            }
        }
    }
    if keys.next().is_some() || !groups.is_empty() {
        issues.push("source operations missing text slots".into());
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
        stream_bytes: BTreeMap::new(),
    };
    let mut ctx = Ctx::new(resources.clone());
    let mut form_seq = 0;
    let contents = lo.get_page_contents(page_id);
    for &stream_id in &contents {
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
        walk_stream(
            lo,
            stream_id,
            &bytes,
            &mut ctx,
            &[],
            0,
            &mut form_seq,
            &mut out,
        );
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

    let mut object_geometry_evidence = Vec::new();
    let groups = bind_glyphs(
        lo,
        &ops_by_group,
        &objs_by_group,
        &font_index,
        &mut stats,
        &mut out.issues,
        &mut object_geometry_evidence,
    );
    let keys: Vec<_> = out.flat_text.iter().map(|op| op.key).collect();
    assign_glyphs(&mut out.items, &keys, groups, &mut out.issues);
    for item in &mut out.items {
        if let DisplayItem::Text { glyphs } = item {
            for glyph in glyphs {
                glyph.id.page = PageId(page - 1);
            }
        }
    }

    for e in &mut object_geometry_evidence {
        e.glyph_id.page = PageId(page - 1);
    }
    let source_spans = out
        .flat_text
        .iter()
        .map(|op| {
            let width = code_width(encoding_of(lo, op.font_id).kind);
            let codes = op
                .strings
                .iter()
                .map(|s| s.len().div_ceil(width) as u32)
                .sum();
            let bytes = op.strings.iter().map(|s| s.len() as u32).sum();
            (op.key, codes, bytes)
        })
        .collect();
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
        source_spans,
        object_geometry_evidence,
        source_snapshot: SourceSnapshot {
            contents,
            streams: out.stream_bytes,
        },
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

    fn empty_mapping_steps(source: &[u32], observed: &[(Option<&str>, f32)]) -> Vec<AlignStep> {
        let mut map = ToUnicodeMap::default();
        map.insert(1, vec![]);
        map.insert(2, vec!['y']);
        let chars: Vec<_> = observed
            .iter()
            .map(|(unicode, x)| TextChar {
                unicode: unicode.map(str::to_string),
                bbox: Rect::default(),
                origin: Point::new(*x, 700.0),
                width: 12.0,
                angle: 0.0,
                is_generated: false,
            })
            .collect();
        let real: Vec<_> = chars.iter().collect();
        let clusters = cluster_by_origin(&real);
        let codes: Vec<_> = source.iter().map(|c| (*c, 0, (0, 2))).collect();
        align_codes_to_clusters(&codes, EncodingKind::Double, &map, &clusters)
    }

    #[test]
    fn empty_mapping_without_pdfium_char_does_not_consume_next_cluster() {
        for unicode in ["y", "\u{2}"] {
            let steps = empty_mapping_steps(&[1, 2], &[(Some(unicode), 84.0)]);
            assert!(steps[0].cluster.is_none());
            assert_eq!(steps[1].cluster, Some(0));
            assert!(!steps[1].mismatch);
        }
    }

    #[test]
    fn empty_mapping_placeholder_consumed_only_with_identity_evidence() {
        let steps = empty_mapping_steps(&[1, 2], &[(Some("\u{1}"), 72.0), (Some("\u{2}"), 84.0)]);
        assert!(steps[0].cluster.is_none());
        assert_eq!(steps[1].cluster, Some(1));
        for observed in [
            vec![(Some("\u{3}"), 72.0), (Some("\u{2}"), 84.0)],
            vec![(None, 72.0), (Some("y"), 84.0)],
            vec![(Some("\u{1}"), 72.0), (Some("\u{2}"), 72.0)],
            vec![(Some("y"), 72.0), (Some("y"), 84.0)],
        ] {
            let steps = empty_mapping_steps(&[1, 2], &observed);
            assert!(steps.iter().all(|s| s.cluster.is_none() && s.mismatch));
        }
    }

    #[test]
    fn empty_mapping_ambiguous_repeated_placeholder_rejects_alignment() {
        let steps = empty_mapping_steps(&[1, 1, 2], &[(Some("\u{1}"), 72.0), (Some("y"), 84.0)]);
        assert!(steps.iter().all(|s| s.cluster.is_none() && s.mismatch));
    }

    #[test]
    fn cmap_bfchar_and_surrogate_pair() {
        let data = b"/CIDInit /ProcSet findresource begin
\
begincmap
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
\
2 beginbfchar
<0003> <0044006F>
<0021> <d835dc99>
endbfchar
endcmap";
        let m = parse_to_unicode_cmap(data);
        // 多码点映射（连字）。
        assert_eq!(m.get(3), Some(&['D', 'o'][..]));
        // 星面字符的 UTF-16 代理对 → 单个 char（U+1D499）。
        let astral: Vec<char> = "\u{1D499}".chars().collect();
        assert_eq!(m.get(0x21), Some(astral.as_slice()));
        // 不在表内。
        assert_eq!(m.get(4), None);
    }

    #[test]
    fn cmap_bfrange_forms() {
        // 直区间：末码点随区间递增。
        let data = b"begincmap
1 begincodespacerange
<00> <FF>
endcodespacerange
\
1 beginbfrange
<0000> <0002> <0041>
endbfrange
endcmap";
        let m = parse_to_unicode_cmap(data);
        assert_eq!(m.get(0), Some(&['A'][..]));
        assert_eq!(m.get(1), Some(&['B'][..]));
        assert_eq!(m.get(2), Some(&['C'][..]));

        // 数组形式。
        let data = b"begincmap
1 beginbfrange
<000a> <000c> [<0066><0069><0020>]
endbfrange
endcmap";
        let m = parse_to_unicode_cmap(data);
        assert_eq!(m.get(0x0a), Some(&['f'][..]));
        assert_eq!(m.get(0x0b), Some(&['i'][..]));
        assert_eq!(m.get(0x0c), Some(&[' '][..]));

        // 空目的地 = 明确无映射（空 vec，区别于不在表内）。
        let data = b"begincmap
1 beginbfchar
<0001> <>
endbfchar
endcmap";
        let m = parse_to_unicode_cmap(data);
        assert_eq!(m.get(1), Some(&[][..]));
    }

    #[test]
    fn cmap_bfrange_scalar_supplementary() {
        for (dst, expected) in [
            ("D83DDE00", ['😀', '😁']),
            ("D835DC99", ['\u{1D499}', '\u{1D49A}']),
        ] {
            let data = format!("beginbfrange <0001> <0002> <{dst}> endbfrange");
            let m = parse_to_unicode_cmap(data.as_bytes());
            assert_eq!(m.get(1), Some(&expected[..1]));
            assert_eq!(m.get(2), Some(&expected[1..]));
        }
    }

    #[test]
    fn cmap_bfrange_array_max_source() {
        let m = parse_to_unicode_cmap(b"beginbfrange <FFFFFFFF> <FFFFFFFF> [<0041>] endbfrange");
        assert_eq!(m.get(u32::MAX), Some(&['A'][..]));
        assert_eq!(m.get(0), None);
    }

    #[test]
    fn cmap_bfrange_scalar_last_byte_boundary() {
        let m = parse_to_unicode_cmap(b"beginbfrange <0001> <0004> <00FE> endbfrange");
        assert_eq!(m.get(1), Some(&['þ'][..]));
        assert_eq!(m.get(2), Some(&['ÿ'][..]));
        assert_eq!(m.get(3), None);
        assert_eq!(m.get(4), None);
    }

    #[test]
    fn cmap_malformed_does_not_panic() {
        let m = parse_to_unicode_cmap(b"beginbfchar <zz> <0041> endbfchar");
        assert_eq!(m.get(0), None);
        let m = parse_to_unicode_cmap(b"beginbfrange <0000> <FFFF> <x>");
        assert!(m.get(0).is_none());
        let m = parse_to_unicode_cmap(b"garbage (str <not hex>");
        assert_eq!(m, ToUnicodeMap::default());
    }

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
        assert!(
            w.width_1000(200).is_nan(),
            "unknown width cannot be used for deletion"
        );
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
    fn font_widths_resolve_descendant_and_mixed_cid_width_forms() {
        let mut doc = Document::new();
        let indirect_width = doc.add_object(Object::Integer(600));
        let widths = doc.add_object(Object::Array(vec![500.into(), indirect_width.into()]));
        let mixed = doc.add_object(Object::Array(vec![
            10.into(),
            widths.into(),
            20.into(),
            22.into(),
            750.into(),
        ]));
        let descendant = doc.add_object(lopdf::dictionary! { "DW" => 900, "W" => mixed });
        let descendants = doc.add_object(Object::Array(vec![descendant.into()]));
        let font = doc.add_object(
            lopdf::dictionary! { "Subtype" => "Type0", "DescendantFonts" => descendants },
        );
        let widths = font_widths(&doc, font);
        for (code, expected) in [
            (10, 500.0),
            (11, 600.0),
            (20, 750.0),
            (22, 750.0),
            (23, 900.0),
        ] {
            assert_eq!(widths.width_1000(code), expected, "CID {code}");
        }
    }

    #[test]
    fn simple_invalid_width_does_not_shift_later_codes() {
        let mut doc = Document::new();
        let widths = doc.add_object(Object::Array(vec![500.into(), Object::Null, 700.into()]));
        let font = doc.add_object(
            lopdf::dictionary! { "Subtype" => "Type1", "FirstChar" => 65, "Widths" => widths },
        );
        let widths = font_widths(&doc, font);
        assert_eq!(widths.width_1000(65), 500.0);
        assert!(widths.width_1000(66).is_nan());
        assert_eq!(widths.width_1000(67), 700.0);
    }

    #[test]
    fn type3_explicit_horizontal_matrix_scales_and_rounds_widths() {
        let mut doc = Document::new();
        let font = doc.add_object(lopdf::dictionary! {
            "Subtype" => "Type3", "FirstChar" => 65, "Widths" => vec![Object::Real(501.25)],
            "FontMatrix" => vec![0.002.into(), 0.into(), 0.into(), 0.001.into(), 0.into(), 0.into()]
        });
        assert_eq!(font_widths(&doc, font).width_1000(65), 1003.0);
        doc.get_object_mut(font)
            .unwrap()
            .as_dict_mut()
            .unwrap()
            .set(
                "FontMatrix",
                vec![
                    0.002.into(),
                    0.001.into(),
                    0.into(),
                    0.001.into(),
                    0.into(),
                    0.into(),
                ],
            );
        assert!(font_widths(&doc, font).width_1000(65).is_nan());
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
    fn assign_glyphs_rejects_missing_groups_and_slots() {
        let key = OpKey::new(ObjRef::new(1, 0), 0);
        let mut items = vec![DisplayItem::Text { glyphs: vec![] }];
        let mut issues = Vec::new();
        assign_glyphs(&mut items, &[key], GlyphGroups::new(), &mut issues);
        assert!(issues[0].contains("missing glyph group"));
        issues.clear();
        assign_glyphs(&mut items, &[], GlyphGroups::new(), &mut issues);
        assert!(issues[0].contains("missing source operation"));
        issues.clear();
        let groups = GlyphGroups::from([((key.stream, key.op_index), vec![])]);
        assign_glyphs(&mut [], &[key], groups, &mut issues);
        assert!(issues[0].contains("missing text slots"));
    }

    #[test]
    fn assign_glyphs_fills_empty_text_items() {
        use syncpdf_core::ids::OpKey;
        let mut items = vec![DisplayItem::Text { glyphs: vec![] }];
        let mut groups = GlyphGroups::new();
        groups.insert((ObjRef::new(1, 0), 0u32), vec![]);
        let key = OpKey::new(ObjRef::new(1, 0), 0);
        let mut issues = Vec::new();
        assign_glyphs(&mut items, &[key], groups, &mut issues);
        assert!(issues.is_empty());
        match &items[0] {
            DisplayItem::Text { glyphs } => assert!(glyphs.is_empty()),
            _ => panic!("expect text"),
        }
    }
}
