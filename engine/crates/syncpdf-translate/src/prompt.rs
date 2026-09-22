//! 提示词组装：**one-shot 整文档**。
//!
//! 设计基准：02-技术路径与架构.md §5.2；系统提示词沿用 hjfy 原文
//! （research/02-hjfy-engine-deep-dive.md §8.1），改成「一次给整份文档」。
//!
//! 产品核心是「边译边编译」：整份文档的全部翻译单元放进**一个**提示词发出去，
//! 模型流式吐块，上游每收到一个完整块就立刻回写排版。只有当提示词超过
//! `max_chars` 时才按**页边界**切成尽量少的几片，顺序发送——页边界保证同一页的
//! 上下文不被拆散，上游也能按页推进编译。
//!
//! 安全：提示词正文绝不进日志（§约束）。本模块只负责拼字符串，不做任何 IO。

use std::collections::HashMap;

use syncpdf_core::ParagraphId;

use crate::unit::Unit;

/// 单片提示词的默认字符上限。
pub const DEFAULT_MAX_CHARS: usize = 60_000;

/// 正文区起始标记；假翻译器与测试据此定位单元区。
pub const DOCUMENT_MARKER: &str = "DOCUMENT:";

/// 一次翻译任务的静态参数。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PromptSpec {
    pub source_lang: String,
    pub target_lang: String,
    /// 术语表 `(源词, 目标词)`，可空。
    pub terminology: Vec<(String, String)>,
    /// 单片提示词的字符上限，超过才切片。
    pub max_chars: usize,
}

impl Default for PromptSpec {
    fn default() -> Self {
        Self {
            source_lang: "auto".into(),
            target_lang: "zh-CN".into(),
            terminology: Vec::new(),
            max_chars: DEFAULT_MAX_CHARS,
        }
    }
}

impl PromptSpec {
    pub fn new(source_lang: impl Into<String>, target_lang: impl Into<String>) -> Self {
        Self {
            source_lang: source_lang.into(),
            target_lang: target_lang.into(),
            ..Self::default()
        }
    }
}

/// 一片提示词：整文档，或整文档按页切出的一段。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentPrompt {
    /// 系统提示词（pi/genai 都要单独传，故随片携带）。
    pub system: String,
    /// 用户消息正文。
    pub text: String,
    /// 本片包含的单元 id，顺序即正文顺序。
    pub unit_ids: Vec<ParagraphId>,
}

impl DocumentPrompt {
    /// 在正文前插一条修复要求（拆分重试用）。不改 `unit_ids`。
    pub fn with_repair_note(mut self, codes: &[&str]) -> Self {
        if codes.is_empty() {
            return self;
        }
        self.text = format!(
            "The previous response was invalid. Repair requirement: {}\n\
             {}\n\n{}",
            codes.join(", "),
            REPAIR_HTML_NOTE,
            self.text
        );
        self
    }
}

/// hjfy 的 HTML 修复补充说明（§8.2）。
const REPAIR_HTML_NOTE: &str = "Return restricted HTML, keeping every `<span data-style=\"...\">` tag and literal {{KEEP_N}} marker. Do not return rendered plain text. Do not replace markers with their ATOM_HINTS values. Translate the text of EVERY source span inside that same span; do not merge its words into a neighboring style. A span containing only a marker is empty text and is invalid. Keep markers outside spans at their source positions.";

/// 系统提示词。沿用 hjfy 原文并改为整文档、多块输出。
pub fn system_prompt(spec: &PromptSpec) -> String {
    format!(
        "Translate only natural-language text into {tgt}. \
The input is a document made of restricted-HTML blocks, one `<p id=\"...\">...</p>` per source paragraph. \
Return only the translated blocks, one `<p id=\"...\">...</p>` per input block, in the same order, separated by newlines. \
Begin with the first block's `<p` and end with the last block's `</p>`. \
Do not wrap the output in JSON, Markdown, or code fences, and do not add explanations.\n\
Copy every `id` attribute byte-for-byte; never renumber, merge, split, drop or duplicate a block. \
Preserve every HTML tag, `data-style` attribute, and span count exactly. \
Never merge adjacent span tags, even when they have the same `data-style` value; three input span tags must remain three output span tags. \
Translate the complete sentence naturally: each source span must contain its own translated text, without merging differently styled words or dropping or duplicating source meaning. \
A span containing only a marker is empty text and is invalid. \
Keep every `<br>` and its count. \
Preserve every email address and URL byte-for-byte; translate only its surrounding label or prose. \
Every {{{{KEEP_N}}}} marker is immutable layout data: copy each marker exactly once, in the original order, even when several markers are adjacent. \
ATOM_HINTS explains what each opaque marker displays; use those hints only to choose grammatical words around markers, never copy hints into the output or alter marker order. \
Do not translate code atoms. \
The only allowed tags are `<p id>`, `<span data-style>` and `<br>`; escape a literal `<`, `>` or `&` as `&lt;`, `&gt;`, `&amp;`.\n\
Follow the target language's specified writing system and regional standard; do not substitute another script or region. \
The source may contain multiple languages, including within one paragraph{src}. \
When translating, use {tgt} and keep text already in that language unchanged unless conversion to the requested writing standard is needed. \
Do not report detected languages.",
        tgt = spec.target_lang,
        src = source_clause(&spec.source_lang),
    )
}

fn source_clause(source_lang: &str) -> String {
    let s = source_lang.trim();
    if s.is_empty() || s.eq_ignore_ascii_case("auto") {
        "; infer them from the current content".to_string()
    } else {
        format!("; the dominant source language is {s}")
    }
}

/// 组装整文档提示词。
///
/// 能一片放下就只返回一片；放不下时按**页边界**贪心打包成尽量少的几片。单页本身
/// 就超限时该页独占一片（不在页内再切，页内切会破坏段间上下文）。
pub fn build_document_prompts(
    spec: &PromptSpec,
    units: &[Unit],
    atom_hints: &HashMap<ParagraphId, Vec<String>>,
) -> Vec<DocumentPrompt> {
    if units.is_empty() {
        return Vec::new();
    }
    let system = system_prompt(spec);
    let limit = spec.max_chars.max(1);

    // 先试整份。
    let whole = render(spec, &system, units, atom_hints);
    if whole.text.chars().count() <= limit {
        return vec![whole];
    }

    // 超限：按页分组（保持给定顺序，只把连续同页的单元归成一组）。
    let mut groups: Vec<&[Unit]> = Vec::new();
    let mut start = 0usize;
    for i in 1..=units.len() {
        if i == units.len() || units[i].id.page != units[start].id.page {
            groups.push(&units[start..i]);
            start = i;
        }
    }

    // 贪心：连续页组尽量多地塞进一片。
    let mut out = Vec::new();
    let mut cur: Vec<Unit> = Vec::new();
    for g in groups {
        if !cur.is_empty() {
            let mut trial = cur.clone();
            trial.extend_from_slice(g);
            let p = render(spec, &system, &trial, atom_hints);
            if p.text.chars().count() <= limit {
                cur = trial;
                continue;
            }
            out.push(render(spec, &system, &cur, atom_hints));
            cur.clear();
        }
        cur.extend_from_slice(g);
    }
    if !cur.is_empty() {
        out.push(render(spec, &system, &cur, atom_hints));
    }
    out
}

/// 拼一片提示词正文。
fn render(
    spec: &PromptSpec,
    system: &str,
    units: &[Unit],
    atom_hints: &HashMap<ParagraphId, Vec<String>>,
) -> DocumentPrompt {
    let mut t = String::new();
    t.push_str(&format!(
        "Translate the {n} restricted-HTML block(s) under {DOCUMENT_MARKER} into {tgt}. \
TERMINOLOGY and ATOM_HINTS are read-only context; do not include or translate them in the response. \
Use every source-to-target mapping in TERMINOLOGY when translating its matching source term.\n",
        n = units.len(),
        tgt = spec.target_lang,
    ));

    if !spec.terminology.is_empty() {
        t.push_str("\nTERMINOLOGY:\n");
        for (s, d) in &spec.terminology {
            t.push_str(&format!("- {s} => {d}\n"));
        }
    }

    let mut hint_lines = String::new();
    for u in units {
        let Some(hints) = atom_hints.get(&u.id) else {
            continue;
        };
        for (i, h) in hints.iter().enumerate() {
            if h.trim().is_empty() {
                continue;
            }
            hint_lines.push_str(&format!(
                "- {} {{{{KEEP_{}}}}} = {}\n",
                u.id,
                i + 1,
                h.trim()
            ));
        }
    }
    if !hint_lines.is_empty() {
        t.push_str("\nATOM_HINTS:\n");
        t.push_str(&hint_lines);
    }

    t.push('\n');
    t.push_str(DOCUMENT_MARKER);
    t.push('\n');
    for u in units {
        t.push_str(&u.html);
        t.push('\n');
    }

    DocumentPrompt {
        system: system.to_string(),
        text: t,
        unit_ids: units.iter().map(|u| u.id.clone()).collect(),
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// 造一个 `html` 长度约为 `len` 的单元。
    pub(crate) fn unit(id: &str, len: usize) -> Unit {
        let body = "w".repeat(len);
        Unit {
            id: id.parse().unwrap(),
            html: format!("<p id=\"{id}\">{body}</p>"),
            styles: 0,
            atoms: vec![],
            breaks: 0,
        }
    }

    fn spec(max: usize) -> PromptSpec {
        PromptSpec {
            max_chars: max,
            ..PromptSpec::new("en", "zh-CN")
        }
    }

    #[test]
    fn default_spec_uses_60k() {
        assert_eq!(PromptSpec::default().max_chars, DEFAULT_MAX_CHARS);
        assert_eq!(DEFAULT_MAX_CHARS, 60_000);
    }

    #[test]
    fn system_prompt_names_target_and_forbids_fences() {
        let s = system_prompt(&spec(100));
        assert!(s.contains("zh-CN"));
        assert!(s.contains("code fences"));
        assert!(s.contains("{{KEEP_N}}"));
        assert!(s.contains("the dominant source language is en"));
        let auto = system_prompt(&PromptSpec::default());
        assert!(auto.contains("infer them from the current content"));
    }

    #[test]
    fn whole_document_fits_in_one_prompt() {
        let units: Vec<Unit> = (1..=5).map(|i| unit(&format!("P0{i}-001"), 100)).collect();
        let ps = build_document_prompts(&spec(DEFAULT_MAX_CHARS), &units, &HashMap::new());
        assert_eq!(ps.len(), 1);
        assert_eq!(ps[0].unit_ids.len(), 5);
        for u in &units {
            assert!(ps[0].text.contains(&u.html));
        }
        // 正文区在 DOCUMENT: 之后。
        let (head, body) = ps[0].text.split_once(DOCUMENT_MARKER).unwrap();
        assert!(!head.contains("<p id="));
        assert!(body.contains("<p id=\"P01-001\">"));
    }

    #[test]
    fn empty_units_yield_no_prompt() {
        assert!(build_document_prompts(&spec(100), &[], &HashMap::new()).is_empty());
    }

    #[test]
    fn oversized_document_splits_on_page_boundaries_only() {
        // 3 页，每页 2 段，每段 ~400 字符。
        let mut units = Vec::new();
        for page in 1..=3 {
            for seq in 1..=2 {
                units.push(unit(&format!("P0{page}-00{seq}"), 400));
            }
        }
        let ps = build_document_prompts(&spec(1500), &units, &HashMap::new());
        assert!(ps.len() > 1, "should split");
        // 每片的 id 都按页成组，且没有一页被劈开。
        let mut seen_pages: Vec<u32> = Vec::new();
        for p in &ps {
            let pages: Vec<u32> = p.unit_ids.iter().map(|i| i.page).collect();
            for pg in &pages {
                assert!(!seen_pages.contains(pg), "page {pg} split across prompts");
            }
            let mut uniq = pages.clone();
            uniq.dedup();
            seen_pages.extend(uniq);
        }
        // 顺序保持、单元不丢不重。
        let all: Vec<ParagraphId> = ps.iter().flat_map(|p| p.unit_ids.clone()).collect();
        assert_eq!(all, units.iter().map(|u| u.id.clone()).collect::<Vec<_>>());
    }

    #[test]
    fn splitting_is_greedy_and_uses_as_few_prompts_as_possible() {
        let mut units = Vec::new();
        for page in 1..=4 {
            units.push(unit(&format!("P0{page}-001"), 100));
        }
        // 上限足够大 → 一片。
        assert_eq!(
            build_document_prompts(&spec(100_000), &units, &HashMap::new()).len(),
            1
        );
        // 上限很小 → 每页一片（页不可再分）。
        let ps = build_document_prompts(&spec(10), &units, &HashMap::new());
        assert_eq!(ps.len(), 4);
    }

    #[test]
    fn single_page_over_the_limit_stays_one_prompt() {
        let units = vec![unit("P01-001", 5000), unit("P01-002", 5000)];
        let ps = build_document_prompts(&spec(100), &units, &HashMap::new());
        assert_eq!(ps.len(), 1, "同页不再切");
        assert_eq!(ps[0].unit_ids.len(), 2);
    }

    #[test]
    fn terminology_and_atom_hints_are_rendered() {
        let mut s = spec(DEFAULT_MAX_CHARS);
        s.terminology = vec![("transformer".into(), "变换器".into())];
        let units = vec![unit("P01-003", 20)];
        let mut hints = HashMap::new();
        hints.insert(
            "P01-003".parse::<ParagraphId>().unwrap(),
            vec!["x^2 + y^2".to_string()],
        );
        let ps = build_document_prompts(&s, &units, &hints);
        assert!(ps[0].text.contains("- transformer => 变换器"));
        assert!(ps[0].text.contains("- P01-003 {{KEEP_1}} = x^2 + y^2"));
    }

    #[test]
    fn repair_note_prefixes_the_body() {
        let units = vec![unit("P01-001", 10)];
        let p = build_document_prompts(&spec(DEFAULT_MAX_CHARS), &units, &HashMap::new())
            .remove(0)
            .with_repair_note(&["placeholder_count", "style_count"]);
        assert!(p.text.starts_with("The previous response was invalid."));
        assert!(p.text.contains("placeholder_count, style_count"));
        assert!(p.text.contains("<p id=\"P01-001\">"));
        assert_eq!(p.unit_ids.len(), 1);
    }
}
