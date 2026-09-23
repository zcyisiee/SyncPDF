//! Markdown one-shot 提示词：一个主请求，显式容量不足时报错，不按页自动切片。
use crate::unit::Unit;
use std::collections::HashMap;
use syncpdf_core::ParagraphId;

/// 默认不施加人为字符阈值；通道自身上下文容量错误仍明确传播。
pub const DEFAULT_MAX_CHARS: usize = usize::MAX;
pub const DOCUMENT_MARKER: &str = "DOCUMENT:";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PromptSpec {
    pub source_lang: String,
    pub target_lang: String,
    pub terminology: Vec<(String, String)>,
    /// 显式指定时限制完整请求的字符数（system + text），超限不发送。
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

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum PromptError {
    #[error(
        "one-shot request needs {actual} characters, configured limit is {limit}; no request sent"
    )]
    Capacity { actual: usize, limit: usize },
    #[error("source block {id} cannot be encoded: {message}")]
    Source { id: ParagraphId, message: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentPrompt {
    pub system: String,
    pub text: String,
    pub unit_ids: Vec<ParagraphId>,
}
impl DocumentPrompt {
    pub fn with_repair_note(mut self, codes: &[&str]) -> Self {
        if !codes.is_empty() {
            self.text = format!("The previous response was invalid. Repair requirement: {}\nReturn the same restricted Markdown blocks; preserve all style identities, atom markers and hard breaks.\n\n{}", codes.join(", "), self.text);
        }
        self
    }
    pub fn check_capacity(&self, limit: usize) -> Result<(), PromptError> {
        let actual = self
            .system
            .chars()
            .count()
            .saturating_add(self.text.chars().count());
        if actual > limit {
            Err(PromptError::Capacity { actual, limit })
        } else {
            Ok(())
        }
    }
}

pub fn system_prompt(spec: &PromptSpec) -> String {
    format!(
        "Transport: {}\nTranslate natural-language text into {}. {}\n{}",
        crate::markdown::TRANSPORT_VERSION,
        spec.target_lang,
        source_clause(&spec.source_lang),
        r#"The input is one complete document of restricted Markdown blocks. Return only the translated blocks in the same order, with no code fences, explanations, or surrounding prose.
Each block begins with an exclusive line <!-- syncpdf:block P01-001 --> and ends with an exclusive line <!-- syncpdf:end P01-001 -->. Copy each source block ID exactly, including matching end ID. Never merge, split, omit, duplicate or invent blocks. Output each complete block as soon as translated.
Source styles use [text]{style=1}. Preserve each style ID and its occurrence count, including adjacent repeated style nodes. Translate all text within its own source style. Do not add bold, italic, headings, links or HTML tags; the backend retains source typography. A style containing only an atom marker is not a substitute for its original prose.
Preserve every {{KEEP_N}} atom marker exactly once in original order. ATOM_HINTS is read-only context, never substitute its value for a marker. Do not translate code, addresses or URLs.
A backslash at the end of a line represents a hard break; keep the same number. Ordinary newlines are text whitespace. Escape literal Markdown punctuation with a backslash, especially brackets, braces, backslashes, angle brackets, asterisks, underscores and backticks. Keep already escaped literals escaped.
Follow the target writing system and regional standard. Preserve meaning, numbers, email addresses and URLs. Keep text already in the target language unless writing-standard conversion is needed. Do not report detected languages."#
    )
}
fn source_clause(source_lang: &str) -> String {
    let s = source_lang.trim();
    if s.is_empty() || s.eq_ignore_ascii_case("auto") {
        "The source may contain multiple languages; infer them from the current content.".into()
    } else {
        format!("The dominant source language is {s}; individual paragraphs may contain other languages.")
    }
}

/// Empty input yields no request; otherwise exactly one request or a capacity error.
pub fn build_document_prompts(
    spec: &PromptSpec,
    units: &[Unit],
    atom_hints: &HashMap<ParagraphId, Vec<String>>,
) -> Result<Vec<DocumentPrompt>, PromptError> {
    if units.is_empty() {
        return Ok(Vec::new());
    }
    let prompt = render(spec, &system_prompt(spec), units, atom_hints)?;
    prompt.check_capacity(spec.max_chars)?;
    Ok(vec![prompt])
}

/// 拼一片提示词正文。
fn render(
    spec: &PromptSpec,
    system: &str,
    units: &[Unit],
    atom_hints: &HashMap<ParagraphId, Vec<String>>,
) -> Result<DocumentPrompt, PromptError> {
    let mut t = String::new();
    t.push_str(&format!(
        "Translate the {n} Markdown block(s) under {DOCUMENT_MARKER} into {tgt}. \
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
        let parsed = crate::unit::parse_unit_html(&u.html).map_err(|e| PromptError::Source {
            id: u.id.clone(),
            message: e.to_string(),
        })?;
        if parsed.id != u.id {
            return Err(PromptError::Source {
                id: u.id.clone(),
                message: "source block identity mismatch".into(),
            });
        }
        t.push_str(&crate::markdown::serialize(&parsed));
        t.push('\n');
    }

    Ok(DocumentPrompt {
        system: system.to_string(),
        text: t,
        unit_ids: units.iter().map(|u| u.id.clone()).collect(),
    })
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
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

    #[test]
    fn large_document_remains_one_markdown_request() {
        let units: Vec<_> = (1..=4)
            .map(|p| unit(&format!("P{p:02}-001"), 20_000))
            .collect();
        let prompts =
            build_document_prompts(&PromptSpec::default(), &units, &HashMap::new()).unwrap();
        assert_eq!(prompts.len(), 1);
        assert_eq!(prompts[0].unit_ids.len(), 4);
        assert!(prompts[0].text.chars().count() > 60_000);
        assert!(!prompts[0].text.contains("<p id="));
        assert!(prompts[0].text.contains("<!-- syncpdf:block P01-001 -->"));
    }
    #[test]
    fn explicit_capacity_counts_system_and_never_splits_or_sends_oversized_page() {
        let units = vec![unit("P01-001", 100), unit("P02-001", 100)];
        let mut spec = PromptSpec::default();
        let prompt = build_document_prompts(&spec, &units, &HashMap::new())
            .unwrap()
            .remove(0);
        let exact = prompt.text.chars().count() + prompt.system.chars().count();
        spec.max_chars = exact;
        assert_eq!(
            build_document_prompts(&spec, &units, &HashMap::new())
                .unwrap()
                .len(),
            1
        );
        spec.max_chars = exact - 1;
        assert!(
            matches!(build_document_prompts(&spec, &units, &HashMap::new()), Err(PromptError::Capacity { actual, .. }) if actual == exact)
        );
        spec.max_chars = 0;
        assert!(build_document_prompts(&spec, &units[..1], &HashMap::new()).is_err());
        assert!(build_document_prompts(&spec, &[], &HashMap::new())
            .unwrap()
            .is_empty());
    }
    #[test]
    fn context_and_repair_notes_keep_markdown_contract() {
        let mut spec = PromptSpec::new("en", "zh-CN");
        spec.terminology
            .push(("transformer".into(), "变换器".into()));
        let units = vec![unit("P01-003", 20)];
        let hints = [(units[0].id.clone(), vec!["x^2".into()])]
            .into_iter()
            .collect();
        let prompt = build_document_prompts(&spec, &units, &hints)
            .unwrap()
            .remove(0)
            .with_repair_note(&["style_count"]);
        assert!(prompt.system.contains("dominant source language is en"));
        assert!(prompt.system.contains("zh-CN"));
        assert!(prompt.system.contains(crate::markdown::TRANSPORT_VERSION));
        assert!(prompt.text.contains("- transformer => 变换器"));
        assert!(prompt.text.contains("- P01-003 {{KEEP_1}} = x^2"));
        assert!(prompt.text.contains("<!-- syncpdf:block P01-003 -->"));
        assert!(prompt.text.contains("Repair requirement: style_count"));
    }
}
