//! 结构损坏（块边界错误 / EOF 半块 / 块外非空白）在**通道本身成功**时的处置：
//! 丢弃损坏之后的所有后缀，把仍未落定的源 id 交给既有的有界二分补译，按
//! `invalid_markup` 记账；已严格通过的前缀照常交付与入缓存。通道失败
//! （断联、超时）仍原样上抛，不重试；源 id 已全部落定时损坏无法归因，仍 fatal。
//!
//! 全部使用受控脚本通道，不联网、不调用模型。

use std::sync::atomic::{AtomicUsize, Ordering};

use async_trait::async_trait;
use syncpdf_core::ParagraphId;
use syncpdf_translate::{
    Cache, ContextMap, DeltaSink, DocumentPrompt, DocumentResult, Engine, PromptSpec,
    TranslateError, TranslatedBlock, Translator, Unit,
};

fn spec() -> PromptSpec {
    PromptSpec::new("en", "en")
}

fn pid(i: u32) -> ParagraphId {
    format!("P01-{i:03}").parse().unwrap()
}

/// 正文不放数字：先撞 `protected_literal_count` 会测不到边界分支。
const WORDS: [&str; 3] = ["alpha", "beta", "gamma"];

fn body(i: u32) -> String {
    format!(
        "The {} paragraph of the sample document",
        WORDS[(i as usize - 1) % WORDS.len()]
    )
}

fn block_html(i: u32) -> String {
    format!("<p id=\"P01-{i:03}\">{}</p>", body(i))
}

/// 严格闭合的合法回包块（含 end 行）。
fn wire(i: u32) -> String {
    markdown(&block_html(i))
}

/// 与 `wire` 相同的块，但 end 行写成 `end_with` 的 id —— 模拟真实模型的错边界。
fn wire_bad_end(i: u32, end_with: u32) -> String {
    let good = markdown(&block_html(i));
    let marker = format!("<!-- syncpdf:end {} -->", pid(i));
    good.replace(&marker, &format!("<!-- syncpdf:end {} -->", pid(end_with)))
}

fn markdown(html: &str) -> String {
    syncpdf_translate::markdown::serialize(&syncpdf_translate::parse_unit_html(html).unwrap())
}

fn unit(i: u32) -> Unit {
    Unit {
        id: pid(i),
        html: block_html(i),
        styles: 0,
        atoms: vec![],
        breaks: 0,
    }
}

fn raw(prompt_html: &str) -> String {
    markdown(prompt_html)
}

fn ids(bs: &[TranslatedBlock]) -> Vec<ParagraphId> {
    bs.iter().map(|b| b.id.clone()).collect()
}

/// 脚本通道：第 n 次调用用 `scripts[n]`（越界则空脚本），可选尾部通道错误。
#[derive(Clone, Default)]
struct Script {
    deltas: Vec<String>,
    tail_error: Option<TranslateError>,
}

impl Script {
    fn ok(deltas: Vec<&str>) -> Self {
        Self {
            deltas: deltas.into_iter().map(str::to_owned).collect(),
            tail_error: None,
        }
    }

    fn err(deltas: Vec<&str>, e: TranslateError) -> Self {
        Self {
            deltas: deltas.into_iter().map(str::to_owned).collect(),
            tail_error: Some(e),
        }
    }
}

#[derive(Default)]
struct Scripted {
    scripts: Vec<Script>,
    calls: AtomicUsize,
    prompts: std::sync::Mutex<Vec<DocumentPrompt>>,
}

impl Scripted {
    fn new(scripts: Vec<Script>) -> Self {
        Self {
            scripts,
            calls: AtomicUsize::new(0),
            prompts: Default::default(),
        }
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
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
        let script = self.scripts.get(n).cloned().unwrap_or_default();
        let mut text = String::new();
        for d in &script.deltas {
            on_delta(d);
            text.push_str(d);
        }
        match script.tail_error {
            Some(e) => Err(e),
            None => Ok(text),
        }
    }

    fn name(&self) -> &str {
        "test/transport-scripted"
    }
}

async fn run(
    scripts: Vec<Script>,
    units: Vec<Unit>,
    cache: Option<&Cache>,
    rounds: Option<u32>,
) -> (
    Result<DocumentResult, TranslateError>,
    Vec<TranslatedBlock>,
    usize,
) {
    let mut engine = Engine::new(Scripted::new(scripts));
    if let Some(r) = rounds {
        engine = engine.with_max_retry_rounds(r);
    }
    let ctx = ContextMap::from_units(&units);
    let mut seen = Vec::new();
    let result = engine
        .translate_document(&spec(), units, ctx, cache, |b| seen.push(b))
        .await;
    let calls = engine.translator().calls();
    (result, seen, calls)
}

/// 第一块严格通过并即时交付；第二块的 end 写成第一个 id → 结构损坏。损坏
/// 之前的合法前缀照常交付/入缓存；损坏之后的后缀（即使看起来合法）不采纳。
/// 未落定的第二块由下一轮严格闭合响应补齐。
#[tokio::test]
async fn bad_end_id_drops_the_suffix_and_repairs_the_pending_unit() {
    let us = vec![unit(1), unit(2)];
    let first = Script::ok(vec![
        &format!("{}\n", wire(1)),
        // 第二块的 end 错写成第一个 id：损坏点。
        &format!("{}\n", wire_bad_end(2, 1)),
        // 损坏之后的“看起来合法”内容：绝不采纳。
        &format!("{}\n", wire(2)),
    ]);
    // 补译片：只有仍缺的段，严格闭合。
    let retry = Script::ok(vec![&format!("{}\n", wire(2))]);
    let cache = Cache::open_in_memory().unwrap();
    let (result, seen, calls) = run(vec![first, retry], us.clone(), Some(&cache), None).await;

    let r = result.expect("坏边界可由后续补译救回，不该 fatal");
    assert!(r.blocks.iter().all(|b| b.status.is_ok()), "{:?}", r.blocks);
    assert!(r.fallback_ids.is_empty());
    assert_eq!(ids(&r.blocks), vec![pid(1), pid(2)]);
    assert_eq!(r.stats.retry_rounds, 1, "应进入既有的一轮二分补译");
    assert_eq!(calls, 2, "损坏不 fatal：需要一次补译请求");
    // 只有严格通过的内容才交付：损坏后缀从未进入 on_block。
    assert_eq!(ids(&seen), vec![pid(1), pid(2)]);
    assert!(seen.iter().all(|b| b.status.is_ok()), "{:?}", seen);
    // 缓存只有严格闭合的真实内容，损坏响应不写入。
    assert_eq!(cache.len().unwrap(), 2);
    for u in &us {
        assert!(cache.get("en", "en", &u.html).unwrap().is_some());
    }
}

/// 损坏之后的后缀即使语法合法也不能被采纳：若补译缺失，未落定段必须回退
/// 原文（violations 记 invalid_markup 证据），不得把后缀当成它的译文。
#[tokio::test]
async fn suffix_after_damage_is_never_adopted_without_a_clean_retry() {
    let us = vec![unit(1), unit(2)];
    let bad = Script::ok(vec![
        &format!("{}\n", wire(1)),
        &format!("{}\n", wire_bad_end(2, 1)),
        // 与缺失的第二块 id 完全匹配、内容合法——仍不得采纳。
        &format!("{}\n", wire(2)),
    ]);
    let (result, seen, calls) = run(vec![bad], us.clone(), None, Some(0)).await;
    let r = result.expect("有可归因的未落定段，回退而非 fatal");
    assert_eq!(r.stats.retry_rounds, 0, "max_retry_rounds=0 不做补译");
    assert_eq!(calls, 1);
    assert_eq!(r.fallback_ids, vec![pid(2)]);
    let second = r.blocks.iter().find(|b| b.id == pid(2)).unwrap();
    assert_eq!(second.html, us[1].html, "回退必须是原文");
    assert!(
        matches!(&second.status, syncpdf_translate::BlockStatus::Fallback { violations }
            if violations == &vec![syncpdf_translate::Violation::InvalidMarkup]),
        "{:?}",
        second.status
    );
    assert_eq!(ids(&seen), vec![pid(1), pid(2)]);
    assert_eq!(seen[0].status, syncpdf_translate::BlockStatus::Ok);
    // 第二块以回退原文交付，绝不是损坏后缀里的内容。
    assert_eq!(seen[1].html, us[1].html);
    assert_eq!(
        seen[1].status,
        syncpdf_translate::BlockStatus::Fallback {
            violations: vec![syncpdf_translate::Violation::InvalidMarkup]
        }
    );
}

/// 成功通道但 EOF 半块：同样可补译；耗尽轮数后回退，不伪成功。
#[tokio::test]
async fn eof_half_block_is_repairable_and_exhaustion_falls_back() {
    let us = vec![unit(1), unit(2)];
    let half = Script::ok(vec![
        &format!("{}\n", wire(1)),
        "<!-- syncpdf:block P01-002 -->\nhalf",
    ]);
    // 补译片仍只吐半块 → 下一轮仍缺 → 轮数用尽回退。
    let still_half = Script::ok(vec!["<!-- syncpdf:block P01-002 -->\nhalf"]);
    let (result, seen, _) = run(vec![half, still_half], us.clone(), None, Some(1)).await;
    let r = result.expect("未落定段可回退，不该 fatal");
    assert_eq!(r.stats.retry_rounds, 1);
    assert_eq!(r.fallback_ids, vec![pid(2)]);
    // 第一块严格交付；第二块以回退原文交付，不是半块残缺内容。
    assert_eq!(ids(&seen), vec![pid(1), pid(2)]);
    assert_eq!(seen[1].html, us[1].html);
    assert!(r.blocks.iter().any(|b| b.id == pid(1) && b.status.is_ok()));

    // 半块由补译补上时应当全 Ok。
    let half = Script::ok(vec![
        &format!("{}\n", wire(1)),
        "<!-- syncpdf:block P01-002 -->\nhalf",
    ]);
    let full = Script::ok(vec![&format!("{}\n", wire(2))]);
    let (result, _, calls) = run(vec![half, full], us.clone(), None, None).await;
    let r = result.expect("补译补齐后应当成功");
    assert!(r.blocks.iter().all(|b| b.status.is_ok()), "{:?}", r.blocks);
    assert_eq!(r.stats.retry_rounds, 1);
    assert_eq!(calls, 2);
}

/// 通道失败优先原样上抛：保留已交付前缀，不进入补译，不换通道。
#[tokio::test]
async fn provider_failure_still_propagates_without_repair() {
    let us = vec![unit(1), unit(2)];
    let script = Script::err(
        vec![
            &format!("{}\n", wire(1)),
            "<!-- syncpdf:block P01-002 -->\nhalf",
        ],
        TranslateError::HarnessFailed("model died mid-response".into()),
    );
    let cache = Cache::open_in_memory().unwrap();
    let (result, seen, calls) = run(vec![script], us.clone(), Some(&cache), None).await;
    let err = result.expect_err("通道失败必须上抛");
    assert!(
        matches!(err, TranslateError::HarnessFailed(ref m) if m.contains("mid-response")),
        "{err:?}"
    );
    assert_eq!(ids(&seen), vec![pid(1)], "前缀交付保留");
    assert_eq!(calls, 1, "通道失败不触发补译");
    assert!(cache.get("en", "en", &us[0].html).unwrap().is_some());
    assert!(cache.get("en", "en", &us[1].html).unwrap().is_none());
}

/// 本请求源 id 已全部落定却仍有坏尾巴：损坏无法归因，仍 fatal，不吞错；
/// 且该路径也不放宽未知 id / 数字门禁。
#[tokio::test]
async fn damaged_tail_after_all_units_settled_stays_fatal() {
    let us = vec![unit(1), unit(2)];
    let script = Script::ok(vec![
        // 段外 id：只记账丢弃，不影响落定。
        &format!(
            "{}\n",
            raw("<p id=\"P42-999\">A paragraph nobody asked for</p>")
        ),
        &format!("{}\n{}\n", wire(1), wire(2)),
        // 全部落定之后的坏尾巴：块外非空白。
        "stray garbage outside any block\n",
    ]);
    let (result, seen, calls) = run(vec![script], us.clone(), None, None).await;
    let err = result.expect_err("无 pending 源 id 时损坏必须 fatal");
    assert!(matches!(err, TranslateError::Transport(_)), "{err:?}");
    assert_eq!(ids(&seen), vec![pid(1), pid(2)]);
    assert_eq!(calls, 1, "fatal 路径不补译");
}

/// 晚到的边界错误不能覆盖此前已发现的数字错误，否则补译会丢失针对性指引。
#[tokio::test]
async fn late_boundary_damage_preserves_earlier_numeric_repair_reason() {
    let mut us = vec![unit(1), unit(2)];
    us[0].html = "<p id=\"P01-001\">The alpha value is 7</p>".into();
    let bad = format!(
        "{}\n{}\n",
        markdown("<p id=\"P01-001\">The alpha value is 8</p>"),
        wire_bad_end(2, 1)
    );
    let engine = Engine::new(Scripted::new(vec![
        Script::ok(vec![&bad]),
        Script::ok(vec![&markdown(&us[0].html)]),
        Script::ok(vec![&wire(2)]),
    ]));
    let ctx = ContextMap::from_units(&us);
    let r = engine
        .translate_document(&spec(), us, ctx, None, |_| {})
        .await
        .unwrap();
    assert!(r.fallback_ids.is_empty());
    let prompts = engine.translator().prompts.lock().unwrap();
    assert_eq!(prompts.len(), 3);
    assert_eq!(prompts[1].unit_ids, vec![pid(1)]);
    assert!(prompts[1].text.contains("Numeric repair:"));
    assert!(prompts[1].text.contains("protected_literal_count"));
    assert!(prompts[1].text.contains("invalid_markup"));
    // 晚到的边界错误不覆盖数字提示：清单仍给出该块的源字面量。
    assert!(prompts[1].text.contains("- P01-001: 7"));
}

/// 补译响应里的未知样式仍被既有校验拒绝：结构修补路径不放宽身份门禁。
#[tokio::test]
async fn repair_response_still_obeys_style_identity_gate() {
    let us = vec![unit(1), unit(2)];
    let bad = Script::ok(vec![
        &format!("{}\n", wire(1)),
        &format!("{}\n", wire_bad_end(2, 1)),
    ]);
    // 补译片对 P01-002 回了未知样式 → unknown_style，不得落定。
    let wrong_style = Script::ok(vec![&format!(
        "{}\n",
        markdown("<p id=\"P01-002\"><span data-style=\"999\">The beta paragraph</span></p>")
    )]);
    let (result, _, calls) = run(vec![bad, wrong_style], us.clone(), None, Some(1)).await;
    let r = result.expect("未落定段回退原文，不该 fatal");
    assert_eq!(calls, 2);
    let second = r.blocks.iter().find(|b| b.id == pid(2)).unwrap();
    assert_eq!(second.html, us[1].html);
    assert!(
        matches!(&second.status, syncpdf_translate::BlockStatus::Fallback { violations }
            if violations.contains(&syncpdf_translate::Violation::UnknownStyle)),
        "{:?}",
        second.status
    );
}
