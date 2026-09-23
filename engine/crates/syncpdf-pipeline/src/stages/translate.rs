//! translating 阶段：`build_unit` → `Engine::translate_document`（one-shot + 流式）。
//!
//! 设计基准：02-技术路径与架构.md §5（翻译通道）与 §6。
//! 本模块把 cli 层的 [`TranslatorKind`] 变成具体的 `Translator`，并把段落
//! 序列喂给 one-shot 引擎；每个已校验块通过 `on_block` 立刻交给上游排版。

use std::collections::HashMap;
use std::time::Duration;

use async_trait::async_trait;
use syncpdf_core::ir::{PageIR, Paragraph};
use syncpdf_core::GlyphId;
use syncpdf_protocol::TranslatorKind;
use syncpdf_translate::{
    build_unit, Cache, ContextMap, DeltaSink, DocumentPrompt, DocumentResult, Engine,
    FakeTranslator, PiTranslator, PromptSpec, TranslateError, TranslatedBlock, Translator, Unit,
};

use super::PipelineError;

/// 把 `Box<dyn Translator>` 适配成 `Engine` 能用的具体类型。
///
/// `syncpdf-translate` 没有为 `Box<dyn Translator>` 实现 `Translator`
/// （`Engine<T: Translator>` 要具体类型），因此在本 crate 转发一次。
pub struct DynTranslator(Box<dyn Translator>);

impl DynTranslator {
    /// 包装一个通道。
    pub fn new(inner: Box<dyn Translator>) -> Self {
        Self(inner)
    }

    /// 取回内层通道（测试用）。
    pub fn inner(&self) -> &dyn Translator {
        self.0.as_ref()
    }
}

impl std::fmt::Debug for DynTranslator {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_tuple("DynTranslator")
            .field(&self.0.name())
            .finish()
    }
}

#[async_trait]
impl Translator for DynTranslator {
    async fn translate(
        &self,
        prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError> {
        self.0.translate(prompt, on_delta).await
    }

    fn name(&self) -> &str {
        self.0.name()
    }
}

/// 由 `configure` 的 [`TranslatorKind`] 构造通道。
///
/// - `Pi` → [`PiTranslator`]（真实子进程；测试不调用）
/// - `Fake` → [`fake_from_name`]
/// - `Http` → 尚未支持
pub fn make_translator(kind: &TranslatorKind) -> Result<Box<dyn Translator>, PipelineError> {
    match kind {
        TranslatorKind::Pi {
            program,
            model,
            thinking,
        } => Ok(Box::new(PiTranslator::new(
            program.clone(),
            model.clone(),
            thinking.clone(),
        ))),
        TranslatorKind::Fake { name } => fake_from_name(name)
            .map(|f| Box::new(f) as Box<dyn Translator>)
            .ok_or_else(|| PipelineError::UnsupportedTranslator(format!("fake:{name}"))),
        TranslatorKind::Http => Err(PipelineError::UnsupportedTranslator("http".into())),
    }
}

/// 解析假翻译器名：`echo` / `stretch:1.4` / `shrink:0.6` / `cjk` /
/// `fail-every:n` / `slow:ms`（毫秒）。
///
/// 其余 `syncpdf_translate::FakeTranslator` 变体是内部测试用的，不出现在协议里。
pub fn fake_from_name(name: &str) -> Option<FakeTranslator> {
    let name = name.trim();
    let (head, arg) = match name.split_once(':') {
        Some((h, a)) => (h, Some(a)),
        None => (name, None),
    };
    match head {
        "echo" => Some(FakeTranslator::Echo),
        "cjk" => Some(FakeTranslator::Cjk),
        "stretch" => {
            let f = arg?.parse::<f32>().ok()?;
            (f.is_finite() && f > 0.0).then_some(FakeTranslator::Stretch(f))
        }
        "shrink" => {
            let f = arg?.parse::<f32>().ok()?;
            (f.is_finite() && f > 0.0).then_some(FakeTranslator::Shrink(f))
        }
        "fail-every" => {
            let n = arg?.parse::<u32>().ok()?;
            (n > 0).then_some(FakeTranslator::FailEvery(n))
        }
        "slow" => {
            let ms = arg?.parse::<u64>().ok()?;
            Some(FakeTranslator::Slow(Duration::from_millis(ms)))
        }
        _ => None,
    }
}

/// 建字形取文闭包：`GlyphId` → 解码文本。
///
/// 段落里的字形来自多页，闭包必须能跨页查表，因此这里一次性建全量索引。
pub fn glyph_text_lookup(pages: &[PageIR]) -> impl Fn(GlyphId) -> Option<String> + '_ {
    let mut map: HashMap<GlyphId, String> = HashMap::new();
    for page in pages {
        for g in page.glyphs() {
            if g.unicode.is_empty() {
                continue;
            }
            map.insert(g.id, g.unicode.iter().collect());
        }
    }
    move |id: GlyphId| map.get(&id).cloned()
}

/// 翻译整批段落，返回引擎结果。
///
/// `on_block` 在每个块落定的瞬间被调用（上游据此立刻排版 + 回写该页）。
/// 泛型而非 `&dyn Translator`：`Engine<T>` 需要具体类型，`&dyn` 的借用
/// 又活不过一次 `Box::new`。
pub async fn translate_all<T: Translator>(
    translator: T,
    spec: &PromptSpec,
    paras: &[Paragraph],
    lookup: impl Fn(GlyphId) -> Option<String>,
    cache: Option<&Cache>,
    on_block: impl FnMut(TranslatedBlock) + Send,
) -> Result<DocumentResult, PipelineError> {
    translate_all_with_cache_policy(translator, spec, paras, lookup, cache, on_block, false).await
}

pub async fn translate_all_with_cache_policy<T: Translator>(
    translator: T,
    spec: &PromptSpec,
    paras: &[Paragraph],
    lookup: impl Fn(GlyphId) -> Option<String>,
    cache: Option<&Cache>,
    on_block: impl FnMut(TranslatedBlock) + Send,
    cache_only: bool,
) -> Result<DocumentResult, PipelineError> {
    let units: Vec<Unit> = paras.iter().map(|p| build_unit(p, &lookup)).collect();
    let mut ctx = ContextMap::from_units(&units);
    for para in paras {
        let mut hints = Vec::new();
        for atom in &para.atoms {
            if atom.id.0 > 0 {
                hints.resize(hints.len().max(atom.id.0 as usize), String::new());
                hints[atom.id.0 as usize - 1] = atom.text.clone();
            }
        }
        if !hints.is_empty() {
            ctx.hints.insert(para.id.clone(), hints);
        }
    }
    let engine = Engine::new(translator).with_cache_only(cache_only);
    engine
        .translate_document(spec, units, ctx, cache, on_block)
        .await
        .map_err(|e| PipelineError::Translate(e.to_string()))
}

/// 与 [`translate_all`] 相同，但接受借来的 `&dyn Translator`。
pub async fn translate_with_dyn(
    translator: &dyn Translator,
    spec: &PromptSpec,
    paras: &[Paragraph],
    lookup: impl Fn(GlyphId) -> Option<String>,
    cache: Option<&Cache>,
    on_block: impl FnMut(TranslatedBlock) + Send,
) -> Result<DocumentResult, PipelineError> {
    translate_all(
        TranslatorRef(translator),
        spec,
        paras,
        lookup,
        cache,
        on_block,
    )
    .await
}

/// 把 `&dyn Translator` 借成 `Translator`。
struct TranslatorRef<'a>(&'a dyn Translator);

#[async_trait]
impl Translator for TranslatorRef<'_> {
    async fn translate(
        &self,
        prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError> {
        self.0.translate(prompt, on_delta).await
    }

    fn name(&self) -> &str {
        self.0.name()
    }
}

impl std::fmt::Debug for TranslatorRef<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_tuple("TranslatorRef")
            .field(&self.0.name())
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::{Align, Atom, AtomKind, Line, RegionKind, StyleRun, Translatable};
    use syncpdf_core::{Color, ObjRef, OpKey, PageId, Rect, StyleId};

    fn paragraph(id: &str, text: &str) -> Paragraph {
        Paragraph {
            id: id.parse().unwrap(),
            page: PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox: Rect::new(0.0, 0.0, 200.0, 20.0),
            lines: vec![],
            glyphs: vec![],
            text_spans: Vec::new(),
            style_runs: vec![],
            atoms: vec![],
            text: text.into(),
            align: Align::Left,
            first_indent: 0.0,
            line_height: 12.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        }
    }

    #[test]
    fn fake_from_name_parses_every_documented_form() {
        assert_eq!(fake_from_name("echo"), Some(FakeTranslator::Echo));
        assert_eq!(fake_from_name("cjk"), Some(FakeTranslator::Cjk));
        assert_eq!(
            fake_from_name("stretch:1.4"),
            Some(FakeTranslator::Stretch(1.4))
        );
        assert_eq!(
            fake_from_name("shrink:0.6"),
            Some(FakeTranslator::Shrink(0.6))
        );
        assert_eq!(
            fake_from_name("fail-every:3"),
            Some(FakeTranslator::FailEvery(3))
        );
        assert_eq!(
            fake_from_name("slow:500"),
            Some(FakeTranslator::Slow(Duration::from_millis(500)))
        );
        // 空白容错。
        assert_eq!(fake_from_name(" echo "), Some(FakeTranslator::Echo));
    }

    #[test]
    fn fake_from_name_rejects_garbage() {
        for bad in [
            "",
            "nope",
            "stretch",
            "stretch:x",
            "stretch:0",
            "stretch:-1",
            "shrink:abc",
            "fail-every:0",
            "fail-every:",
            "slow:",
            "slow:-5",
            "truncate:2",
        ] {
            assert_eq!(fake_from_name(bad), None, "{bad:?} 应被拒");
        }
    }

    #[test]
    fn make_translator_dispatches_and_reports_unsupported() {
        let fake = make_translator(&TranslatorKind::Fake { name: "cjk".into() }).unwrap();
        assert_eq!(fake.name(), "fake/cjk");
        let err = match make_translator(&TranslatorKind::Fake {
            name: "nope".into(),
        }) {
            Ok(_) => panic!("未知 fake 名应报错"),
            Err(e) => e,
        };
        assert!(matches!(err, PipelineError::UnsupportedTranslator(_)));
        let err = match make_translator(&TranslatorKind::Http) {
            Ok(_) => panic!("http 通道尚未支持"),
            Err(e) => e,
        };
        assert_eq!(err.code(), "unsupported_translator");
    }

    #[test]
    fn glyph_text_lookup_indexes_all_pages() {
        use syncpdf_core::ir::{DisplayItem, FontRef, Glyph, GlyphFlags, GlyphSource};
        let mk = |ord: u16, ch: char| Glyph {
            id: GlyphId {
                page: PageId(0),
                op: OpKey::new(ObjRef::new(1, 0), 0),
                ordinal: ord,
            },
            unicode: [ch].into_iter().collect(),
            code: ord as u32,
            font: 0,
            size: 10.0,
            matrix: Default::default(),
            bbox: Rect::new(0.0, 0.0, 6.0, 10.0),
            advance: 6.0,
            fill: Color::default(),
            render_mode: 0,
            source: GlyphSource {
                element_index: 0,
                string_operand_range: (0, 0),
                decoded_code_range: (0, 0),
            },
            flags: GlyphFlags::default(),
        };
        let page = PageIR {
            page: PageId(0),
            media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            crop_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            rotation: 0,
            fonts: vec![FontRef {
                resource_name: "F1".into(),
                base_font: "F1".into(),
                is_serif: false,
                is_fixed_pitch: false,
                is_italic: false,
                is_bold: false,
            }],
            items: vec![DisplayItem::Text {
                glyphs: vec![mk(0, 'A'), mk(1, '中')],
            }],
        };
        let pages = [page];
        let lookup = glyph_text_lookup(&pages);
        let id0 = GlyphId {
            page: PageId(0),
            op: OpKey::new(ObjRef::new(1, 0), 0),
            ordinal: 0,
        };
        assert_eq!(lookup(id0).as_deref(), Some("A"));
        let id1 = GlyphId { ordinal: 1, ..id0 };
        assert_eq!(lookup(id1).as_deref(), Some("中"));
        let missing = GlyphId { ordinal: 99, ..id0 };
        assert_eq!(lookup(missing), None);
    }

    #[tokio::test]
    async fn translate_all_delivers_one_block_per_paragraph() {
        let paras = vec![
            paragraph("P01-001", "alpha paragraph"),
            paragraph("P01-002", "beta paragraph"),
            paragraph("P01-003", "gamma paragraph"),
        ];
        let translator = FakeTranslator::Cjk;
        let mut seen = Vec::new();
        let spec = PromptSpec::new("en", "zh-CN");
        let result = translate_all(
            translator.clone(),
            &spec,
            &paras,
            |_| None,
            None,
            |b: TranslatedBlock| seen.push(b),
        )
        .await
        .unwrap();
        assert_eq!(result.blocks.len(), 3);
        assert_eq!(seen.len(), 3, "每个段落都应有块交付");
        assert_eq!(seen[0].id, "P01-001".parse().unwrap());
        assert!(seen.iter().all(|b| b.status.is_ok()), "{seen:?}");
        assert_eq!(result.stats.prompts, 1, "one-shot：整份文档一片");
    }

    #[tokio::test]
    async fn translate_all_maps_harness_failure_to_translate_error() {
        let paras = vec![paragraph("P01-001", "alpha")];
        let translator = FakeTranslator::Broken;
        let spec = PromptSpec::new("en", "zh-CN");
        let err = translate_all(translator.clone(), &spec, &paras, |_| None, None, |_| {})
            .await
            .unwrap_err();
        assert!(matches!(err, PipelineError::Translate(_)), "{err:?}");
        assert_eq!(err.code(), "translate");
    }

    #[tokio::test]
    async fn empty_paragraph_list_is_a_no_op() {
        let translator = FakeTranslator::Echo;
        let spec = PromptSpec::new("en", "zh-CN");
        let mut seen = 0;
        let r = translate_all(
            translator.clone(),
            &spec,
            &[],
            |_| None,
            None,
            |_| seen += 1,
        )
        .await
        .unwrap();
        assert!(r.blocks.is_empty());
        assert_eq!(seen, 0);
    }

    #[test]
    fn dyn_translator_forwards_name() {
        let d = DynTranslator::new(Box::new(FakeTranslator::Cjk));
        assert_eq!(d.name(), "fake/cjk");
        assert!(format!("{d:?}").contains("fake/cjk"));
    }

    #[test]
    fn paragraph_with_atoms_builds_units_with_keep_tokens() {
        let mut p = paragraph("P01-001", "see {{KEEP_1}} here");
        p.atoms = vec![Atom {
            id: syncpdf_core::AtomId(1),
            glyph_range: (4, 5),
            kind: AtomKind::Formula,
            text: "x".into(),
        }];
        p.style_runs = vec![StyleRun {
            id: StyleId(1),
            glyph_range: (0, 0),
            font: 0,
            size: 10.0,
            color: Color::default(),
            bold: false,
            italic: false,
            serif: false,
            mono: false,
        }];
        let unit = build_unit(&p, |_| None);
        assert!(unit.html.contains("{{KEEP_1}}"), "{}", unit.html);
        let _ = Line {
            glyphs: vec![],
            baseline_y: 0.0,
            bbox: Rect::new(0.0, 0.0, 1.0, 1.0),
        };
    }
}
