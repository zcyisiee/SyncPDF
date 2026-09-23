//! 假翻译器：不联网、不调外部进程，但输出格式与真实模型**完全一致**。
//!
//! 每个变体都：从提示词正文里认出源块 → 按变体规则变换 →
//! **逐块且块中间切开**地流式吐出Markdown。这样 `MarkdownStream` / `validate` /
//! `Engine` 的测试走的就是真实模型会走的路径。

use std::sync::Mutex;
use std::time::Duration;

use async_trait::async_trait;
use syncpdf_core::ParagraphId;

use crate::markdown::{self, MarkdownStream};
use crate::prompt::{DocumentPrompt, DOCUMENT_MARKER};
use crate::translator::{DeltaSink, TranslateError, Translator};
#[cfg(test)]
use crate::unit::parse_unit_html;
use crate::unit::{ParsedUnit, Segment};

/// 流式分片大小（字符）。故意取个小质数，保证切点落在标签中间。
const CHUNK_CHARS: usize = 9;

/// 用于 `Cjk` 变体的汉字表（固定，保证确定性）。
const HAN: &[char] = &[
    '深', '度', '学', '习', '模', '型', '取', '得', '很', '强', '的', '效', '果', '与', '方', '法',
    '实', '验', '数', '据', '结', '论', '分', '析',
];

/// `Stretch` 用的填充词：互不相同，避免误触发 `pathological_text_repetition`。
const FILLER: &[&str] = &[
    "moreover",
    "furthermore",
    "additionally",
    "consequently",
    "specifically",
    "notably",
    "importantly",
    "accordingly",
];

/// 假翻译器。前六个变体是 brief 要求的；其余是为了覆盖 `Engine` 的异常分支。
#[derive(Debug, Clone, PartialEq)]
pub enum FakeTranslator {
    /// 原样返回（结构与文本都不变）。
    Echo,
    /// 把每个文本节点撑到约 `f` 倍长度（触发 `excessive_target_expansion`）。
    Stretch(f32),
    /// 把每个文本节点截到约 `f` 倍长度。
    Shrink(f32),
    /// 把字母串换成汉字（目标语言 zh 时的「正常译文」）。
    Cjk,
    /// 段内序号是 `n` 的倍数的单元返回非法标记（确定性，与分组无关，
    /// 所以拆分重试不会把它救回来 → 最终必然 `Fallback`）。
    FailEvery(u32),
    /// `Echo` + 每片之间 sleep，模拟慢通道。
    Slow(Duration),
    /// 只吐前 `n` 个块，其余漏译。
    Truncate(usize),
    /// `Echo` + 额外吐一个不属于本任务的段 id。
    ExtraBlock,
    /// `Echo` + 把第一个块再吐一遍。
    DuplicateFirst,
    /// 结构合法但正文为空（触发空译回退）。
    EmptyBody,
    /// 通道本身失败。
    Broken,
}

#[async_trait]
impl Translator for FakeTranslator {
    async fn translate(
        &self,
        prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError> {
        if matches!(self, FakeTranslator::Broken) {
            return Err(TranslateError::HarnessFailed(
                "fake translator is broken".into(),
            ));
        }
        let text = self.render(prompt);
        let chars: Vec<char> = text.chars().collect();
        for chunk in chars.chunks(CHUNK_CHARS) {
            let s: String = chunk.iter().collect();
            on_delta(&s);
            if let FakeTranslator::Slow(d) = self {
                tokio::time::sleep(*d).await;
            }
        }
        Ok(text)
    }

    fn name(&self) -> &str {
        match self {
            FakeTranslator::Echo => "fake/echo",
            FakeTranslator::Stretch(_) => "fake/stretch",
            FakeTranslator::Shrink(_) => "fake/shrink",
            FakeTranslator::Cjk => "fake/cjk",
            FakeTranslator::FailEvery(_) => "fake/fail-every",
            FakeTranslator::Slow(_) => "fake/slow",
            FakeTranslator::Truncate(_) => "fake/truncate",
            FakeTranslator::ExtraBlock => "fake/extra-block",
            FakeTranslator::DuplicateFirst => "fake/duplicate-first",
            FakeTranslator::EmptyBody => "fake/empty-body",
            FakeTranslator::Broken => "fake/broken",
        }
    }
}

impl FakeTranslator {
    /// 拼出严格 Markdown 回包。非法传输由专用测试覆盖。
    fn render(&self, prompt: &DocumentPrompt) -> String {
        let sources = source_blocks(prompt);
        let mut out = String::new();
        if let FakeTranslator::DuplicateFirst = self {
            if let Some(first) = sources.first() {
                out.push_str(&self.transform(first));
                out.push('\n');
            }
        }
        let take = match self {
            FakeTranslator::Truncate(n) => (*n).min(sources.len()),
            _ => sources.len(),
        };
        for u in &sources[..take] {
            out.push_str(&self.transform(u));
            out.push('\n');
        }
        if let FakeTranslator::ExtraBlock = self {
            out.push_str(&markdown::serialize(&ParsedUnit {
                id: "P42-999".parse().unwrap(),
                segments: vec![Segment::Text("A paragraph nobody asked for".into())],
            }));
            out.push('\n');
        }

        out
    }

    /// 按变体把一个源块变成「译文」块。
    fn transform(&self, u: &ParsedUnit) -> String {
        match self {
            FakeTranslator::FailEvery(n) if *n > 0 && u.id.seq % *n == 0 => {
                // 未知样式 → unknown_style，且与分组无关，重试救不回来。
                markdown::serialize(&ParsedUnit {
                    id: u.id.clone(),
                    segments: vec![Segment::Style {
                        id: syncpdf_core::StyleId(999),
                        inner: vec![Segment::Text(u.text())],
                    }],
                })
            }
            FakeTranslator::EmptyBody => markdown::serialize(&ParsedUnit {
                id: u.id.clone(),
                segments: vec![Segment::Text(" ".into())],
            }),
            FakeTranslator::Stretch(f) => rebuilt(u, |t| stretch(t, *f)),
            FakeTranslator::Shrink(f) => rebuilt(u, |t| shrink(t, *f)),
            FakeTranslator::Cjk => rebuilt(u, cjk),
            // Echo / Slow / FailEvery(非倍数) / Truncate / ExtraBlock / DuplicateFirst
            _ => markdown::serialize(u),
        }
    }
}

/// 从提示词正文里认出源块（只认 `DOCUMENT:` 之后、且 id 在 `unit_ids` 里的）。
fn source_blocks(prompt: &DocumentPrompt) -> Vec<ParsedUnit> {
    let body = prompt
        .text
        .split_once(DOCUMENT_MARKER)
        .map(|(_, b)| b)
        .unwrap_or(&prompt.text);
    let mut s = MarkdownStream::new();
    let mut raw = s.push(body);
    raw.extend(s.finish());
    let want: Vec<&ParagraphId> = prompt.unit_ids.iter().collect();
    raw.into_iter()
        .filter_map(Result::ok)
        .filter(|b| want.contains(&&b.id))
        .collect()
}

/// 映射所有文本节点后重新序列化。
fn rebuilt(u: &ParsedUnit, mut f: impl FnMut(&str) -> String) -> String {
    markdown::serialize(&ParsedUnit {
        id: u.id.clone(),
        segments: map_text(&u.segments, &mut f),
    })
}

fn map_text(segments: &[Segment], f: &mut impl FnMut(&str) -> String) -> Vec<Segment> {
    segments
        .iter()
        .map(|s| match s {
            Segment::Text(t) => Segment::Text(f(t)),
            Segment::Style { id, inner } => Segment::Style {
                id: *id,
                inner: map_text(inner, f),
            },
            other => other.clone(),
        })
        .collect()
}

/// 撑长到约 `f` 倍：追加互不相同的填充词（不加数字，保住受保护字面量）。
fn stretch(t: &str, f: f32) -> String {
    if t.trim().is_empty() || f <= 1.0 {
        return t.to_string();
    }
    let want = (t.chars().count() as f32 * f).ceil() as usize;
    let mut out = t.to_string();
    let mut i = 0usize;
    while out.chars().count() < want {
        out.push(' ');
        out.push_str(FILLER[i % FILLER.len()]);
        i += 1;
    }
    out
}

/// 截到约 `f` 倍长度（按字符）。
fn shrink(t: &str, f: f32) -> String {
    let keep = (t.chars().count() as f32 * f.clamp(0.0, 1.0)).ceil() as usize;
    t.chars().take(keep).collect()
}

/// 把字母串换成汉字；数字、空白、标点原样保留。
fn cjk(t: &str) -> String {
    let mut out = String::new();
    let mut run = String::new();
    let flush = |run: &mut String, out: &mut String| {
        if run.is_empty() {
            return;
        }
        // 用词本身的确定性散列选起点，长度取词长的一半（中文更短）。
        let seed = run
            .bytes()
            .fold(7usize, |a, b| a.wrapping_mul(31).wrapping_add(b as usize));
        let n = (run.chars().count() / 2).max(1);
        for k in 0..n {
            out.push(HAN[(seed + k * 5) % HAN.len()]);
        }
        run.clear();
    };
    for c in t.chars() {
        if c.is_alphabetic() {
            run.push(c);
        } else {
            flush(&mut run, &mut out);
            out.push(c);
        }
    }
    flush(&mut run, &mut out);
    out
}

/// 记录收到的提示词正文的包装器（验证「缓存命中不进提示词」）。
#[derive(Debug)]
pub struct RecordingTranslator {
    inner: FakeTranslator,
    seen: Mutex<Vec<String>>,
}

impl RecordingTranslator {
    pub fn new(inner: FakeTranslator) -> Self {
        Self {
            inner,
            seen: Mutex::new(Vec::new()),
        }
    }

    /// 迄今收到的提示词正文。
    pub fn recorded(&self) -> Vec<String> {
        self.seen.lock().expect("recorder mutex").clone()
    }
}

#[async_trait]
impl Translator for RecordingTranslator {
    async fn translate(
        &self,
        prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError> {
        self.seen
            .lock()
            .expect("recorder mutex")
            .push(prompt.text.clone());
        self.inner.translate(prompt, on_delta).await
    }

    fn name(&self) -> &str {
        self.inner.name()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::prompt::{build_document_prompts, PromptSpec};
    use crate::unit::Unit;
    use std::collections::HashMap;

    fn unit(id: &str, body: &str) -> Unit {
        Unit {
            id: id.parse().unwrap(),
            html: format!("<p id=\"{id}\">{body}</p>"),
            styles: 0,
            atoms: vec![],
            breaks: 0,
        }
    }

    fn prompt(units: &[Unit]) -> DocumentPrompt {
        build_document_prompts(&PromptSpec::new("en", "zh-CN"), units, &HashMap::new())
            .unwrap()
            .remove(0)
    }

    /// 走完整的流式路径，返回 (块, 完整文本)。
    async fn stream(t: &FakeTranslator, units: &[Unit]) -> (Vec<String>, String) {
        let p = prompt(units);
        let mut s = MarkdownStream::new();
        let mut blocks = Vec::new();
        let mut deltas = 0usize;
        let full = {
            let mut sink = |d: &str| {
                deltas += 1;
                blocks.extend(s.push(d).into_iter().map(|b| b.unwrap().to_html()));
            };
            t.translate(&p, &mut sink).await.unwrap()
        };
        blocks.extend(s.finish().into_iter().map(|b| b.unwrap().to_html()));
        assert!(deltas > 1, "假翻译器必须真的分多次流式输出");
        (blocks, full)
    }

    #[tokio::test]
    async fn echo_round_trips_structure() {
        let us = vec![
            unit(
                "P01-001",
                "Deep <span data-style=\"1\">learning</span> {{KEEP_1}}<br>works",
            ),
            unit("P01-002", "Second paragraph"),
        ];
        let (blocks, _) = stream(&FakeTranslator::Echo, &us).await;
        assert_eq!(blocks, vec![us[0].html.clone(), us[1].html.clone()]);
    }

    #[tokio::test]
    async fn blocks_survive_being_cut_mid_tag() {
        // 分片大小是 9 字符，块边界必然被切开；仍应还原出完整块。
        let us = vec![unit("P01-001", "alpha beta gamma delta epsilon zeta")];
        let (blocks, full) = stream(&FakeTranslator::Echo, &us).await;
        assert_eq!(blocks.len(), 1);
        assert!(full.starts_with("<!-- syncpdf:block P01-001 -->"));
        assert!(full.ends_with("<!-- syncpdf:end P01-001 -->\n"));
    }

    #[tokio::test]
    async fn stretch_and_shrink_change_length_but_not_structure() {
        let us = vec![unit(
            "P01-001",
            "alpha <span data-style=\"1\">beta</span> gamma",
        )];
        let (long, _) = stream(&FakeTranslator::Stretch(4.0), &us).await;
        let (short, _) = stream(&FakeTranslator::Shrink(0.5), &us).await;
        let src = parse_unit_html(&us[0].html).unwrap();
        let l = parse_unit_html(&long[0]).unwrap();
        let s = parse_unit_html(&short[0]).unwrap();
        assert_eq!(l.style_ids(), src.style_ids());
        assert_eq!(s.style_ids(), src.style_ids());
        assert!(l.text().chars().count() > src.text().chars().count() * 3);
        assert!(s.text().chars().count() < src.text().chars().count());
    }

    #[tokio::test]
    async fn cjk_keeps_markers_and_numbers() {
        let us = vec![unit("P01-001", "we trained {{KEEP_1}} for 200 epochs")];
        let (blocks, _) = stream(&FakeTranslator::Cjk, &us).await;
        let p = parse_unit_html(&blocks[0]).unwrap();
        assert_eq!(p.atom_ids(), vec![syncpdf_core::AtomId(1)]);
        assert!(p.text().contains("200"), "数字必须保留: {}", p.text());
        assert!(p
            .text()
            .chars()
            .any(|c| ('\u{4E00}'..='\u{9FFF}').contains(&c)));
        assert!(!p.text().chars().any(|c| c.is_ascii_alphabetic()));
    }

    #[tokio::test]
    async fn fail_every_corrupts_only_the_matching_sequence_numbers() {
        let us = vec![
            unit("P01-001", "one"),
            unit("P01-002", "two"),
            unit("P01-003", "three"),
            unit("P01-004", "four"),
        ];
        let (blocks, _) = stream(&FakeTranslator::FailEvery(2), &us).await;
        assert_eq!(blocks.len(), 4);
        assert!(parse_unit_html(&blocks[0]).is_ok());
        assert_eq!(
            parse_unit_html(&blocks[1]).unwrap().style_ids(),
            vec![syncpdf_core::StyleId(999)]
        );
        assert!(parse_unit_html(&blocks[2]).is_ok());
        assert_eq!(
            parse_unit_html(&blocks[3]).unwrap().style_ids(),
            vec![syncpdf_core::StyleId(999)]
        );
        // 单独重试 P01-002 仍然失败（与分组无关）。
        let (again, _) = stream(&FakeTranslator::FailEvery(2), &us[1..2]).await;
        assert_eq!(
            parse_unit_html(&again[0]).unwrap().style_ids(),
            vec![syncpdf_core::StyleId(999)]
        );
    }

    #[tokio::test]
    async fn slow_variant_still_streams_correctly() {
        let us = vec![unit("P01-001", "alpha beta gamma")];
        let (blocks, _) = stream(&FakeTranslator::Slow(Duration::from_millis(1)), &us).await;
        assert_eq!(blocks, vec![us[0].html.clone()]);
    }

    #[tokio::test]
    async fn truncate_extra_and_duplicate_variants() {
        let us = vec![unit("P01-001", "one"), unit("P01-002", "two")];
        let (t, _) = stream(&FakeTranslator::Truncate(1), &us).await;
        assert_eq!(t.len(), 1);
        let (e, _) = stream(&FakeTranslator::ExtraBlock, &us).await;
        assert_eq!(e.len(), 3);
        assert!(e[2].contains("P42-999"));
        let (d, _) = stream(&FakeTranslator::DuplicateFirst, &us).await;
        assert_eq!(d.len(), 3);
        assert_eq!(d[0], d[1]);
    }

    #[tokio::test]
    async fn broken_variant_reports_harness_failure() {
        let us = vec![unit("P01-001", "one")];
        let p = prompt(&us);
        let mut sink = |_: &str| {};
        let err = FakeTranslator::Broken
            .translate(&p, &mut sink)
            .await
            .unwrap_err();
        assert!(matches!(err, TranslateError::HarnessFailed(_)));
    }

    #[tokio::test]
    async fn recorder_captures_prompt_bodies() {
        let us = vec![unit("P01-001", "one")];
        let r = RecordingTranslator::new(FakeTranslator::Echo);
        let p = prompt(&us);
        let mut sink = |_: &str| {};
        r.translate(&p, &mut sink).await.unwrap();
        assert_eq!(r.recorded().len(), 1);
        assert!(r.recorded()[0].contains("<!-- syncpdf:block P01-001 -->"));
        assert_eq!(r.name(), "fake/echo");
    }

    #[tokio::test]
    async fn blocks_outside_the_document_marker_are_ignored() {
        // 提示词头部的说明文字里即使出现 <p id=…> 也不该被当成源块。
        let us = vec![unit("P01-001", "one")];
        let mut p = prompt(&us);
        p.text = format!("<p id=\"P99-001\">not a source block</p>\n{}", p.text);
        let mut s = MarkdownStream::new();
        let mut got = Vec::new();
        {
            let mut sink = |d: &str| got.extend(s.push(d).into_iter().map(|b| b.unwrap().id));
            FakeTranslator::Echo.translate(&p, &mut sink).await.unwrap();
        }
        got.extend(s.finish().into_iter().map(|b| b.unwrap().id));
        assert_eq!(got, vec!["P01-001".parse::<ParagraphId>().unwrap()]);
    }
}
