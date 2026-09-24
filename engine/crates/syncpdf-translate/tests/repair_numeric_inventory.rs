//! 数字违规补救：`protected_literal_count` 触发补译时，补救请求附与
//! `validate` 同源的逐块数字字面量多重集（含重复次数），作为只读上下文。
//! 主请求不新增该上下文；非数字补救不带清单；清单不放宽任何既有门禁。
//!
//! 全部使用受控脚本通道，不联网、不调用模型。

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use async_trait::async_trait;
use syncpdf_core::ParagraphId;
use syncpdf_translate::{
    ContextMap, DeltaSink, DocumentPrompt, DocumentResult, Engine, PromptSpec, TranslateError,
    Translator, Unit,
};

fn spec() -> PromptSpec {
    PromptSpec::new("en", "en")
}

fn pid(i: u32) -> ParagraphId {
    format!("P01-{i:03}").parse().unwrap()
}

fn unit(i: u32, body: &str) -> Unit {
    Unit {
        id: pid(i),
        html: format!("<p id=\"P01-{i:03}\">{body}</p>"),
        styles: 0,
        atoms: syncpdf_translate::parse_unit_html(&format!("<p id=\"P01-{i:03}\">{body}</p>"))
            .map(|p| p.atom_ids())
            .unwrap_or_default(),
        breaks: 0,
    }
}

fn markdown(html: &str) -> String {
    syncpdf_translate::markdown::serialize(&syncpdf_translate::parse_unit_html(html).unwrap())
}

/// 脚本通道：第 n 次调用返回 `scripts[n]`（越界则回显空），同时记录收到的提示词。
#[derive(Default)]
struct Scripted {
    scripts: Vec<String>,
    calls: AtomicUsize,
    prompts: Mutex<Vec<DocumentPrompt>>,
}

impl Scripted {
    fn new(scripts: Vec<String>) -> Self {
        Self {
            scripts,
            calls: AtomicUsize::new(0),
            prompts: Mutex::new(Vec::new()),
        }
    }
}

#[async_trait]
impl Translator for Scripted {
    async fn translate(
        &self,
        prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError> {
        self.prompts.lock().unwrap().push(prompt.clone());
        let n = self.calls.fetch_add(1, Ordering::SeqCst);
        let text = self.scripts.get(n).cloned().unwrap_or_default();
        for d in text.as_bytes().chunks(11) {
            on_delta(std::str::from_utf8(d).unwrap());
        }
        Ok(text)
    }

    fn name(&self) -> &str {
        "test/numeric-scripted"
    }
}

async fn run(
    scripts: Vec<String>,
    units: Vec<Unit>,
    ctx: ContextMap,
) -> (Engine<Scripted>, DocumentResult) {
    let engine = Engine::new(Scripted::new(scripts));
    let r = engine
        .translate_document(&spec(), units, ctx, None, |_| {})
        .await
        .unwrap();
    (engine, r)
}

/// 补救提示词里某块的数字清单行（`- P01-001: …`）。
fn inventory_line(prompt_text: &str, id: &str) -> Option<String> {
    prompt_text
        .lines()
        .find(|l| l.starts_with(&format!("- {id}:")))
        .map(str::to_owned)
}

/// 数字坏响应触发补救：清单逐块列出源字面量；`100.6` 原样保留、不变成 `1.006`；
/// 重复数字带正确次数；主请求没有该清单；补译严格通过后正常落定。
#[tokio::test]
async fn numeric_violation_attaches_per_block_source_inventory() {
    let us = vec![
        unit(
            1,
            "batch size is 100.6 million tokens with momentum 0.95 and decay 0.95",
        ),
        unit(2, "the plain second paragraph stays digit free"),
    ];
    // 第一次响应：100.6 million 被换算成 1.006 亿 → protected_literal_count。
    let bad = markdown(
        "<p id=\"P01-001\">batch size is 1.006 hundred-million tokens with momentum 0.95 and decay 0.95</p>",
    );
    let good_first = markdown(&us[0].html);
    let echo_second = markdown(&us[1].html);
    let (engine, r) = run(
        vec![format!("{bad}\n{echo_second}"), good_first],
        us.clone(),
        ContextMap::from_units(&us),
    )
    .await;
    assert!(r.blocks.iter().all(|b| b.status.is_ok()), "{:?}", r.blocks);
    assert_eq!(r.stats.retry_rounds, 1);

    let prompts = engine.translator().prompts.lock().unwrap();
    assert_eq!(prompts.len(), 2, "主请求 + 一次补译");
    // 主请求不新增数字清单上下文。
    assert!(!prompts[0].text.contains("Numeric repair:"));
    // 补救请求含逐块清单；重复数字带次数。
    let line = inventory_line(&prompts[1].text, "P01-001").expect("数字违规块必须有字面量清单行");
    assert!(line.contains("100.6"), "{line}");
    assert!(!line.contains("1.006"), "{line}");
    assert!(line.contains("0.95 ×2"), "{line}");
    // 无数字违规的块不附清单。
    assert!(inventory_line(&prompts[1].text, "P01-002").is_none());
}

/// 拼写月份被译成数字是「加了源里没有的字面量」：清单列的是源数字，
/// 其中没有额外的 `9`；补译改回文字表述后通过。
#[tokio::test]
async fn spelled_month_adds_no_digit_to_source_inventory() {
    let us = vec![unit(
        1,
        "deployment launched in September 2026 with tiers 100 and 75",
    )];
    // September → 9：目标多出一个源没有的数字字面量。
    let bad = markdown("<p id=\"P01-001\">deployment launched in 9 2026 with tiers 100 and 75</p>");
    let good = markdown(&us[0].html);
    let (engine, r) = run(vec![bad, good], us.clone(), ContextMap::from_units(&us)).await;
    assert!(r.blocks.iter().all(|b| b.status.is_ok()), "{:?}", r.blocks);
    let prompts = engine.translator().prompts.lock().unwrap();
    let line = inventory_line(&prompts[1].text, "P01-001").unwrap();
    assert!(line.contains("2026"), "{line}");
    assert!(line.contains("100"), "{line}");
    assert!(line.contains("75"), "{line}");
    assert!(!line.contains('9'), "{line}");
}

/// KEEP 编号、style 编号、块 id 与 ATOM_HINTS 的数字都不是源文本数字，
/// 不得混入清单；清单只含跨 style 片段拼接后 text() 里的数字（与校验同源）。
#[tokio::test]
async fn inventory_excludes_keep_style_blockid_and_hint_digits() {
    // 文本数字只有 "5" 与跨两个 style 片段的 "10"+"0.5" = "100.5"。
    let us = vec![unit(
        1,
        "rate 5 stays <span data-style=\"8\">flat</span> {{KEEP_12}} over \
         <span data-style=\"2\">10</span><span data-style=\"3\">0.5</span> units",
    )];
    let ctx = ContextMap::from_units(&us).with_hints(pid(1), vec!["x^3 + 4.5".into()]);
    // 删掉 "5" → 数字多重集不等。
    let bad = markdown(
        "<p id=\"P01-001\">rate stays <span data-style=\"8\">flat</span> {{KEEP_12}} over \
         <span data-style=\"2\">10</span><span data-style=\"3\">0.5</span> units</p>",
    );
    let good = markdown(&us[0].html);
    let (engine, r) = run(vec![bad, good], us.clone(), ctx).await;
    assert!(r.blocks.iter().all(|b| b.status.is_ok()), "{:?}", r.blocks);
    let prompts = engine.translator().prompts.lock().unwrap();
    // ATOM_HINTS 仍以原样上下文出现，但清单行只能是 text() 的数字。
    assert!(prompts[1].text.contains("4.5"));
    let line = inventory_line(&prompts[1].text, "P01-001").unwrap();
    assert_eq!(line, "- P01-001: 5, 100.5", "{line}");
}

/// 非数字补救（unknown_style）不带数字说明与清单。
#[tokio::test]
async fn non_numeric_repair_carries_no_numeric_inventory() {
    let us = vec![unit(1, "rate 5 stays flat through training")];
    let bad = markdown(
        "<p id=\"P01-001\"><span data-style=\"9\">rate 5 stays flat through training</span></p>",
    );
    let good = markdown(&us[0].html);
    let (engine, r) = run(vec![bad, good], us.clone(), ContextMap::from_units(&us)).await;
    assert!(r.blocks.iter().all(|b| b.status.is_ok()), "{:?}", r.blocks);
    let prompts = engine.translator().prompts.lock().unwrap();
    assert_eq!(prompts.len(), 2);
    assert!(prompts[1].text.contains("unknown_style"));
    assert!(!prompts[1].text.contains("Numeric repair:"));
    assert!(inventory_line(&prompts[1].text, "P01-001").is_none());
}

#[tokio::test]
async fn numeric_inventory_is_checked_before_sending_oversized_repair() {
    let us = vec![unit(1, "the constant is 42 in every case")];
    let ctx = ContextMap::from_units(&us);
    let mut spec = spec();
    let initial = syncpdf_translate::build_document_prompts(&spec, &us, &Default::default())
        .unwrap()
        .remove(0);
    spec.max_chars = initial.system.chars().count() + initial.text.chars().count();
    let bad = markdown("<p id=\"P01-001\">the constant is 43 in every case</p>");
    let engine = Engine::new(Scripted::new(vec![bad]));
    let result = engine
        .translate_document(&spec, us, ctx, None, |_| {})
        .await;
    assert!(matches!(
        result,
        Err(TranslateError::Prompt(
            syncpdf_translate::PromptError::Capacity { .. }
        ))
    ));
    assert_eq!(engine.translator().calls.load(Ordering::SeqCst), 1);
}

/// 数字块反复违规：每轮补救都带清单，用尽轮数后回退原文，不伪成功；
/// 回退 violations 仍记 protected_literal_count。
#[tokio::test]
async fn persistent_numeric_violation_exhausts_rounds_and_falls_back() {
    let us = vec![unit(1, "the constant is 42 in every case")];
    let bad = markdown("<p id=\"P01-001\">the constant is 43 in every case</p>");
    let engine = Engine::new(Scripted::new(vec![
        bad.clone(),
        bad.clone(),
        bad.clone(),
        bad,
    ]));
    let r = engine
        .translate_document(
            &spec(),
            us.clone(),
            ContextMap::from_units(&us),
            None,
            |_| {},
        )
        .await
        .unwrap();
    assert_eq!(r.fallback_ids, vec![pid(1)]);
    let b = &r.blocks[0];
    assert_eq!(b.html, us[0].html, "回退必须是原文");
    assert!(
        matches!(&b.status, syncpdf_translate::BlockStatus::Fallback { violations }
            if violations.contains(&syncpdf_translate::Violation::ProtectedLiteralCount)),
        "{:?}",
        b.status
    );
    let prompts = engine.translator().prompts.lock().unwrap();
    // 每轮补救请求都带同一份清单。
    for p in prompts.iter().skip(1) {
        assert_eq!(
            inventory_line(&p.text, "P01-001").as_deref(),
            Some("- P01-001: 42")
        );
    }
}
