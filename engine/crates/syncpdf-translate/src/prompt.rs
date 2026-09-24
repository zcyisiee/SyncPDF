//! Markdown one-shot 提示词：一个主请求，显式容量不足时报错，不按页自动切片。
use crate::unit::Unit;
use std::collections::HashMap;
use syncpdf_core::ParagraphId;

/// 默认不施加人为字符阈值；通道自身上下文容量错误仍明确传播。
pub const DEFAULT_MAX_CHARS: usize = usize::MAX;
pub const DOCUMENT_MARKER: &str = "DOCUMENT:";
/// TRANSLATE.md 的学术要求，适配 Rust 块/样式/KEEP 协议；同时进入缓存身份。
pub const ACADEMIC_RULES: &str = include_str!("academic_rules.md");

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
    /// `numeric`：`protected_literal_count` 违规块 → 与 `validate` 同源的源数字
    /// 字面量多重集；作为只读上下文附在补救说明里，不得进入译文。
    pub fn with_repair_note(
        mut self,
        codes: &[&str],
        numeric: &[(ParagraphId, Vec<String>)],
    ) -> Self {
        if !codes.is_empty() {
            let mut numeric_note = String::new();
            if codes.contains(&"protected_literal_count") {
                numeric_note.push_str(
                    "\nNumeric repair: reproduce the exact source digit sequences and their occurrence counts, including numbers inside technical names. Do not rescale magnitudes: 'N million' keeps the digits N unchanged and only the unit word is translated. Do not add digits when translating spelled-out quantities or spelled-out month names: they become target-language words, never new digits. Do not convert numeric units, or repeat a numbered technical name in an added English gloss. KEEP markers already carry their numbers; never repeat their values in prose.",
                );
                if !numeric.is_empty() {
                    numeric_note.push_str(
                        "\nRequired source digit multiset per block (read-only context; never emit it in the translation):",
                    );
                    for (id, lits) in numeric {
                        numeric_note.push_str(&format!("\n- {id}: {}", literal_multiset(lits)));
                    }
                }
            }
            self.text = format!("The previous response was invalid. Repair requirement: {}\nReturn the same restricted Markdown block IDs; use only known style IDs and preserve atom markers and hard breaks.{}\n\n{}", codes.join(", "), numeric_note, self.text);
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
        "Transport: {}\nTranslate natural-language text into {}. {}\n{}\n\n{}",
        crate::markdown::TRANSPORT_VERSION,
        spec.target_lang,
        source_clause(&spec.source_lang),
        r#"The input is one complete document of restricted Markdown blocks. Return only the translated blocks in the same order, with no code fences, explanations, or surrounding prose.
Each block begins with an exclusive line <!-- syncpdf:block P01-001 --> and ends with an exclusive line <!-- syncpdf:end P01-001 -->. Copy each source block ID exactly, including matching end ID. Never merge, split, omit, duplicate or invent blocks. Output each complete block as soon as translated.
Source styles use [text]{style=1}. Use only style IDs supplied in that source block. Choose style placement to suit the translated meaning and word order: you may reorder, split, merge, reuse or omit style spans. Style occurrence counts need not match the source; atom-only or empty style spans are allowed. Do not add Markdown bold, italic, headings, links or HTML tags; known style IDs provide font and size metadata.
Preserve every {{KEEP_N}} atom marker exactly once; markers may move with their referents and may enter a different known style span when the translated word order calls for it. ATOM_HINTS is read-only context, never substitute its value for a marker. Do not translate code, addresses or URLs.
A backslash at the end of a line represents a hard break; keep the same number. Ordinary newlines are text whitespace. Escape literal Markdown punctuation with a backslash, especially brackets, braces, backslashes, angle brackets, asterisks, underscores and backticks. Keep already escaped literals escaped.
Translate prose into the target language and regional standard. Preserve conventional proper names and technical terms in English or their original language when appropriate, including short labels; there is no target-language character percentage requirement. Preserve meaning, numbers, email addresses and URLs. Keep text already in the target language unless writing-standard conversion is needed. Do not report detected languages."#,
        ACADEMIC_RULES,
    )
}
/// 数字字面量多重集展示：按首次出现顺序，重复项标次数（`0.95 ×2`）；空集 `(none)`。
fn literal_multiset(lits: &[String]) -> String {
    if lits.is_empty() {
        return "(none)".into();
    }
    let mut counts: Vec<(&str, usize)> = Vec::new();
    for l in lits {
        match counts.iter_mut().find(|(s, _)| *s == l.as_str()) {
            Some(e) => e.1 += 1,
            None => counts.push((l.as_str(), 1)),
        }
    }
    counts
        .into_iter()
        .map(|(s, n)| {
            if n > 1 {
                format!("{s} ×{n}")
            } else {
                s.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(", ")
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
    fn academic_rules_apply_to_primary_and_repair_without_changing_transport() {
        let prompt = build_document_prompts(
            &PromptSpec::new("en", "zh-CN"),
            &[unit("P01-001", 12)],
            &HashMap::new(),
        )
        .unwrap()
        .remove(0);
        assert!(prompt.system.contains(ACADEMIC_RULES));
        assert!(prompt.system.contains("简洁优先"));
        assert!(prompt.system.contains("全篇术语统一"));
        assert!(prompt.system.contains("只复制当前尚未闭合的 block 行 ID"));
        assert!(prompt.system.contains("每段都必须先有自己的 block 行"));
        assert!(prompt.system.contains("一、引言"));
        assert!(prompt.system.contains("① ② ③"));
        assert!(prompt.system.contains("[文字]{style=N}"));
        assert!(!prompt.system.contains("[[S"));
        assert!(!prompt.system.contains("<!-- id="));
        let original_system = prompt.system.clone();
        let repaired = prompt.with_repair_note(&["placeholder_count"], &[]);
        assert_eq!(repaired.system, original_system);
        assert!(repaired.text.contains("<!-- syncpdf:block P01-001 -->"));
        // Target language remains a request parameter, not hardcoded to Chinese.
        assert!(system_prompt(&PromptSpec::new("en", "ja")).contains("into ja"));
    }

    #[test]
    fn factor_guidance_preserves_the_source_digit_inventory() {
        let mut source = unit("P01-001", 1);
        source.html = "<p id=\"P01-001\">Storage is reduced by a factor of 7.</p>".into();
        let ctx = crate::validate::ValidateCtx::new(&source, "zh-CN");
        assert!(crate::validate::validate(
            &ctx,
            "<p id=\"P01-001\">存储量降至原来的 7 分之一。</p>"
        )
        .is_ok());
        let bad = crate::validate::validate(&ctx, "<p id=\"P01-001\">存储量降至原来的 1/7。</p>")
            .unwrap_err();
        assert!(bad.contains(&crate::validate::Violation::ProtectedLiteralCount));
        assert!(ACADEMIC_RULES.contains("不增加源文未出现的阿拉伯数字"));
        assert!(!ACADEMIC_RULES.contains("1/N"));
    }

    #[test]
    fn numeric_repair_explains_literal_counts_without_mutating_source() {
        let source = unit("P01-001", 12);
        let prompt = build_document_prompts(&PromptSpec::default(), &[source], &HashMap::new())
            .unwrap()
            .remove(0);
        assert!(!prompt.text.contains("Numeric repair:"));
        let numeric = vec![(
            "P01-001".parse().unwrap(),
            vec!["0.95".to_string(), "100.6".to_string(), "0.95".to_string()],
        )];
        let repaired = prompt
            .clone()
            .with_repair_note(&["protected_literal_count"], &numeric);
        assert_eq!(repaired.system, prompt.system);
        assert!(repaired.text.ends_with(&prompt.text));
        assert!(repaired.text.contains("spelled-out quantities"));
        assert!(repaired.text.contains("occurrence counts"));
        assert!(repaired.text.contains("KEEP markers"));
        assert!(repaired.text.contains("- P01-001: 0.95 ×2, 100.6"));
        assert!(!prompt
            .with_repair_note(&["placeholder_count"], &[])
            .text
            .contains("Numeric repair:"));
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
            .with_repair_note(&["placeholder_count"], &[]);
        assert!(prompt.system.contains("dominant source language is en"));
        assert!(prompt.system.contains("zh-CN"));
        assert!(prompt.system.contains(crate::markdown::TRANSPORT_VERSION));
        assert!(prompt.text.contains("- transformer => 变换器"));
        assert!(prompt.text.contains("- P01-003 {{KEEP_1}} = x^2"));
        assert!(prompt.text.contains("<!-- syncpdf:block P01-003 -->"));
        assert!(prompt
            .text
            .contains("Repair requirement: placeholder_count"));
        assert!(prompt
            .system
            .contains("Style occurrence counts need not match"));
        assert!(prompt
            .system
            .contains("no target-language character percentage requirement"));
    }
}
