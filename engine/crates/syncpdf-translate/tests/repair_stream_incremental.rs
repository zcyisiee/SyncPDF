//! R1-stream 行为守卫：闭合译文块必须在模型响应结束**之前**交付。
//!
//! 全部用受控 `Translator`（门控 / 脚本），不联网、零 LLM 消耗：
//!
//! - [`gated_translator_waits_for_first_block_delivery`]：通道吐完第一个完整
//!   块后停在门上，等 `on_block` 确认「第一块已交付」才继续吐第二块并返回。
//!   若引擎仍把交付推迟到响应结束（旧实现的形态），门永远等不到确认，
//!   严格超时把死锁变成确定性失败——证明的是时序，不是最终顺序，也不靠 sleep。
//! - 脚本通道在每个 delta 发完时快照「已交付块数」，把交付时刻钉死在
//!   delta 边界上：覆盖标签跨 delta、一个 delta 多块、未知/重复 id、
//!   坏 HTML、非标记类校验失败、缓存命中、尾部模型错误与重试只补漏。

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use syncpdf_core::ParagraphId;
use syncpdf_translate::{
    BlockStatus, Cache, ContextMap, DeltaSink, DocumentPrompt, DocumentResult, Engine, PromptSpec,
    TranslateError, TranslatedBlock, Translator, Unit,
};
use tokio::sync::Notify;

/// 门控等待与整测超时：对内存操作足够宽（CI 抖动），又足够紧（死锁不会
/// 挂死测试套件，超时即失败）。
const GATE_TIMEOUT: Duration = Duration::from_secs(5);
const TEST_TIMEOUT: Duration = Duration::from_secs(10);

/// 段落正文里**不放数字**：否则会先撞 `protected_literal_count`，
/// 测不到想测的分支（与 `translator.rs` 单测同一约束）。
const WORDS: [&str; 3] = ["alpha", "beta", "gamma"];

fn spec() -> PromptSpec {
    PromptSpec::new("en", "en")
}

fn pid(i: u32) -> ParagraphId {
    format!("P01-{i:03}").parse().unwrap()
}

fn body(i: u32) -> String {
    format!(
        "The {} paragraph of the sample document",
        WORDS[(i as usize - 1) % WORDS.len()]
    )
}

fn block_html(i: u32) -> String {
    format!("<p id=\"P01-{i:03}\">{}</p>", body(i))
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

fn ids(bs: &[TranslatedBlock]) -> Vec<ParagraphId> {
    bs.iter().map(|b| b.id.clone()).collect()
}

async fn run_engine<E: Translator>(
    engine: &Engine<E>,
    units: &[Unit],
    cache: Option<&Cache>,
    on_block: impl FnMut(TranslatedBlock) + Send,
) -> Result<DocumentResult, TranslateError> {
    let ctx = ContextMap::from_units(units);
    engine
        .translate_document(&spec(), units.to_vec(), ctx, cache, on_block)
        .await
}

// ── 受控通道 ─────────────────────────────────────────────────────────────

/// 门控通道：吐完第一个完整块后停在门上，等测试侧 `on_block` 确认后继续。
struct GatedTranslator {
    first_block_delivered: Arc<Notify>,
    /// 通道返回前置位；`on_block` 交付时读它，证明交付早于响应完成。
    response_finished: Arc<AtomicBool>,
}

#[async_trait]
impl Translator for GatedTranslator {
    async fn translate(
        &self,
        _prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError> {
        let mut text = String::from("Sure:\n```html\n");
        // 第一块：开标签与正文故意切开（跨 delta）。
        let a = "<p id=\"P0";
        let b = format!("1-001\">{}</p>\n", body(1));
        for d in [a, b.as_str()] {
            text.push_str(d);
            on_delta(d);
        }
        // 关键门：引擎必须在响应结束前把第一块交进 on_block，否则死等超时。
        if tokio::time::timeout(GATE_TIMEOUT, self.first_block_delivered.notified())
            .await
            .is_err()
        {
            return Err(TranslateError::HarnessFailed(
                "gated translator: the first closed block was not delivered before the response \
                 finished"
                    .into(),
            ));
        }
        let second = [
            format!("<p id=\"P01-002\">{}</p>\n", body(2)),
            "```\nDone.".to_string(),
        ];
        for d in &second {
            text.push_str(d);
            on_delta(d);
        }
        self.response_finished.store(true, Ordering::SeqCst);
        Ok(text)
    }

    fn name(&self) -> &str {
        "test/gated"
    }
}

/// 一次调用的脚本：逐 delta 输出，可选在结尾返回错误（模拟尾部模型错误）。
#[derive(Clone, Default)]
struct Script {
    deltas: Vec<String>,
    tail_error: Option<TranslateError>,
}

impl Script {
    fn ok(deltas: Vec<String>) -> Self {
        Self {
            deltas,
            tail_error: None,
        }
    }

    fn err(deltas: Vec<String>, e: TranslateError) -> Self {
        Self {
            deltas,
            tail_error: Some(e),
        }
    }
}

/// 脚本通道：第 n 次调用用 `scripts[n]`（越界则空脚本）；每发完一个 delta
/// 快照当时的已交付块数，把交付时刻钉在 delta 边界上。
struct ScriptedTranslator {
    scripts: Vec<Script>,
    calls: Arc<AtomicUsize>,
    delivered: Arc<AtomicUsize>,
    snapshots: Arc<Mutex<Vec<usize>>>,
}

impl ScriptedTranslator {
    fn new(scripts: Vec<Script>) -> (Self, Arc<AtomicUsize>, Arc<Mutex<Vec<usize>>>) {
        let delivered = Arc::new(AtomicUsize::new(0));
        let snapshots = Arc::new(Mutex::new(Vec::new()));
        (
            Self {
                scripts,
                calls: Arc::new(AtomicUsize::new(0)),
                delivered: delivered.clone(),
                snapshots: snapshots.clone(),
            },
            delivered,
            snapshots,
        )
    }
}

#[async_trait]
impl Translator for ScriptedTranslator {
    async fn translate(
        &self,
        _prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError> {
        let n = self.calls.fetch_add(1, Ordering::SeqCst);
        let script = self.scripts.get(n).cloned().unwrap_or_default();
        let mut text = String::new();
        for d in &script.deltas {
            on_delta(d);
            text.push_str(d);
            self.snapshots
                .lock()
                .expect("snapshot mutex")
                .push(self.delivered.load(Ordering::SeqCst));
        }
        match script.tail_error {
            Some(e) => Err(e),
            None => Ok(text),
        }
    }

    fn name(&self) -> &str {
        "test/scripted"
    }
}

/// 跑一台脚本引擎，记录交付的块；返回（引擎引用、交付计数、delta 快照）。
async fn run_scripted(
    scripts: Vec<Script>,
    units: &[Unit],
    cache: Option<&Cache>,
    rounds: Option<u32>,
) -> (
    Result<DocumentResult, TranslateError>,
    Vec<TranslatedBlock>,
    Arc<AtomicUsize>,
    Arc<Mutex<Vec<usize>>>,
    Arc<AtomicUsize>,
) {
    let (translator, delivered, snapshots) = ScriptedTranslator::new(scripts);
    let mut engine = Engine::new(translator);
    if let Some(r) = rounds {
        engine = engine.with_max_retry_rounds(r);
    }
    let calls = engine.translator().calls.clone();
    let mut seen = Vec::new();
    let result = run_engine(&engine, units, cache, |b| {
        seen.push(b.clone());
        delivered.fetch_add(1, Ordering::SeqCst);
    })
    .await;
    (result, seen, delivered, snapshots, calls)
}

// ── 测试 ─────────────────────────────────────────────────────────────────

/// 核心证明：第一块的 `on_block` 发生在 `translate` 返回之前。
/// 通道在第一块吐完后物理停住（门），只有引擎真的在 delta 回调里交付，
/// 门才会被打开；交付被推迟到响应之后的实现会等门超时 → 测试失败。
#[tokio::test]
async fn gated_translator_waits_for_first_block_delivery() {
    let us = vec![unit(1), unit(2)];
    let gate = Arc::new(Notify::new());
    let response_finished = Arc::new(AtomicBool::new(false));
    let engine = Engine::new(GatedTranslator {
        first_block_delivered: gate.clone(),
        response_finished: response_finished.clone(),
    });
    let ctx = ContextMap::from_units(&us);
    let mut seen: Vec<TranslatedBlock> = Vec::new();
    let mut finished_at_delivery: Vec<bool> = Vec::new();

    let result = tokio::time::timeout(
        TEST_TIMEOUT,
        engine.translate_document(&spec(), us.clone(), ctx, None, |b| {
            seen.push(b.clone());
            finished_at_delivery.push(response_finished.load(Ordering::SeqCst));
            if seen.len() == 1 {
                // 放行第二块：此刻通道仍卡在 await 上，响应远未完成。
                gate.notify_one();
            }
        }),
    )
    .await
    .expect("严格超时内未完成：交付仍被推迟到响应之后（死锁形态）");

    let r = result.expect("门控通道不该失败；失败说明第一块没在响应内交付");
    assert_eq!(ids(&seen), [pid(1), pid(2)], "两块都应交付且各一次");
    assert_eq!(ids(&r.blocks), [pid(1), pid(2)], "结果与输入同序");
    assert!(r.blocks.iter().all(|b| b.status.is_ok()), "{:?}", r.blocks);
    assert_eq!(r.stats.prompts, 1);
    assert!(
        finished_at_delivery.iter().all(|f| !f),
        "每次交付时响应都必须还没结束：{finished_at_delivery:?}"
    );
}

/// 标签跨 delta、一个 delta 闭合多块：交付紧跟 delta 边界，而不是攒到响应末尾。
#[tokio::test]
async fn delivery_follows_delta_boundaries_with_split_tags_and_multi_blocks() {
    let us = vec![unit(1), unit(2), unit(3)];
    let script = Script::ok(vec![
        // 开标签只吐一半。
        "Sure:\n```html\n<p id=".to_string(),
        // 补全开标签并闭合第一块；随后切开第二块的开标签。
        format!("\"P01-001\">{}</p>\n<p id=\"P01-002\">The beta", body(1)),
        // 一个 delta 里闭合第二块 **和** 整个第三块。
        format!(" paragraph of the sample document</p>\n{}", block_html(3)),
        // 收尾围栏：块外垃圾。
        "\n```\nDone.".to_string(),
    ]);
    let (result, seen, _delivered, snapshots, calls) =
        run_scripted(vec![script], &us, None, None).await;
    let r = result.expect("脚本合法，不该失败");
    // 每个 delta 发完时的已交付块数：0（未闭合）→ 1（第一块闭合）→
    // 3（同 delta 闭合两块）→ 3（围栏无块）。
    let snaps = snapshots.lock().expect("snapshot mutex").clone();
    assert_eq!(snaps, vec![0, 1, 3, 3], "交付必须紧跟 delta 边界");
    assert_eq!(ids(&seen), [pid(1), pid(2), pid(3)]);
    assert_eq!(ids(&r.blocks), [pid(1), pid(2), pid(3)]);
    assert!(r.blocks.iter().all(|b| b.status.is_ok()));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(r.stats.prompts, 1);
}

/// 未知 id 与重复 id：既不进排版也不重复交付，只记账。
#[tokio::test]
async fn unknown_and_duplicate_ids_never_reach_layout() {
    let us = vec![unit(1), unit(2)];
    let script = Script::ok(vec![
        // 段外 id：一律不采用。
        "<p id=\"P42-999\">A paragraph nobody asked for</p>\n".to_string(),
        // 合法第一块。
        format!("{}\n", block_html(1)),
        // 重复 id：后到的丢弃。
        format!("{}\n", block_html(1)),
        // 合法第二块收尾。
        format!("{}\n```\n", block_html(2)),
    ]);
    let (result, seen, _delivered, snapshots, calls) =
        run_scripted(vec![script], &us, None, None).await;
    let r = result.expect("未知/重复 id 不构成通道失败");
    assert_eq!(ids(&seen), [pid(1), pid(2)], "未知与重复块都不得交付");
    assert_eq!(
        r.extra_ids,
        vec!["P42-999".parse::<ParagraphId>().unwrap()],
        "段外 id 进 extra_ids"
    );
    assert_eq!(
        r.stats.violations.get("unknown_paragraph"),
        Some(&1),
        "{:?}",
        r.stats.violations
    );
    assert_eq!(
        r.stats.violations.get("duplicated_text_slots"),
        Some(&1),
        "{:?}",
        r.stats.violations
    );
    assert!(r.blocks.iter().all(|b| b.status.is_ok()));
    assert_eq!(calls.load(Ordering::SeqCst), 1, "无需重试");
    assert_eq!(
        snapshots.lock().expect("snapshot mutex").clone(),
        vec![0, 1, 1, 2]
    );
}

/// 坏 HTML：校验失败 → 有界拆分重试 → 仍失败回退原文，只交付一次。
#[tokio::test]
async fn bad_markup_falls_back_after_bounded_retry() {
    let us = vec![unit(1)];
    let bad = format!("<p id=\"P01-001\"><b>{}</b></p>", body(1));
    let script = Script::ok(vec![format!("```html\n{bad}\n```\n")]);
    let (result, seen, _delivered, _snapshots, calls) =
        run_scripted(vec![script.clone(), script], &us, None, Some(1)).await;
    let r = result.expect("校验失败以 Fallback 收场，不是 Err");
    assert_eq!(r.fallback_ids, vec![pid(1)]);
    assert_eq!(seen.len(), 1, "回退块只交付一次");
    assert!(matches!(&seen[0].status, BlockStatus::Fallback { .. }));
    assert_eq!(seen[0].html, us[0].html, "回退必须是原文");
    assert_eq!(
        r.stats.violations.get("invalid_markup"),
        Some(&2),
        "首轮 + 1 轮重试各失败一次：{:?}",
        r.stats.violations
    );
    assert_eq!(r.stats.retry_rounds, 1);
    assert_eq!(r.stats.prompts, 2);
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}

/// 非标记类校验失败（多出 span）：同样走有界重试后回退，不进排版。
#[tokio::test]
async fn style_violation_falls_back_after_bounded_retry() {
    let us = vec![unit(1)];
    let bad = format!(
        "<p id=\"P01-001\">{} <span data-style=\"7\">extra</span></p>",
        body(1)
    );
    let script = Script::ok(vec![format!("```html\n{bad}\n```\n")]);
    let (result, seen, _delivered, _snapshots, calls) =
        run_scripted(vec![script.clone(), script], &us, None, Some(1)).await;
    let r = result.expect("校验失败以 Fallback 收场，不是 Err");
    assert_eq!(r.fallback_ids, vec![pid(1)]);
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].html, us[0].html, "回退必须是原文");
    // 源文无 span、译文多一个 span → 结构合法但校验不过。
    assert_eq!(
        r.stats.violations.get("style_count"),
        Some(&2),
        "{:?}",
        r.stats.violations
    );
    assert_eq!(r.stats.violations.get("unknown_style"), Some(&2));
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}

/// 缓存命中：先于任何模型输出交付；结果仍按输入序；新块照常入缓存。
#[tokio::test]
async fn cache_hit_delivers_first_and_result_keeps_input_order() {
    let us = vec![unit(1), unit(2)];
    let cache = Cache::open_in_memory().unwrap();
    let cached = "<p id=\"P01-002\">cached translation of the second paragraph</p>";
    cache.put_manual("en", "en", &us[1].html, cached).unwrap();

    let script = Script::ok(vec![format!("```html\n{}\n```\n", block_html(1))]);
    let (result, seen, _delivered, snapshots, calls) =
        run_scripted(vec![script], &us, Some(&cache), None).await;
    let r = result.expect("缓存 + 单块合法，不该失败");

    assert_eq!(r.stats.cache_hits, 1);
    // 交付顺序：缓存块在最前（早于任何 delta）。
    assert_eq!(ids(&seen), [pid(2), pid(1)]);
    assert!(seen[0].from_cache);
    assert_eq!(seen[0].html, cached);
    // DocumentResult 仍与输入同序（与交付顺序解耦）。
    assert_eq!(ids(&r.blocks), [pid(1), pid(2)]);
    // 第一个 delta 里模型块已闭合：此时缓存块（先于一切 delta）与刚闭合的
    // 模型块都已交付，无需等响应收尾。
    assert_eq!(snapshots.lock().expect("snapshot mutex").clone(), vec![2]);
    // 模型产的新块按协议入缓存，origin 是通道名。
    assert_eq!(cache.len().unwrap(), 2);
    assert_eq!(
        cache.get("en", "en", &us[0].html).unwrap().as_deref(),
        Some(block_html(1).as_str())
    );
    assert_eq!(
        cache.origin_of("en", "en", &us[0].html).unwrap().as_deref(),
        Some("test/scripted")
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}

/// 尾部模型错误：必须返回 Err；此前已交付的完整块保留（不撤回、入缓存），
/// 残缺尾巴不交付，也不触发对漏译块的重试（通道失败优先上抛）。
#[tokio::test]
async fn tail_model_error_returns_err_but_keeps_delivered_blocks() {
    let us = vec![unit(1), unit(2)];
    let script = Script::err(
        vec![
            // 第一块跨两个 delta 闭合。
            "```html\n<p id=\"P0".to_string(),
            format!("1-001\">{}</p>\n", body(1)),
            // 第二块只吐一半就死：不闭合，绝不能交付。
            "<p id=\"P01-002\">hal".to_string(),
        ],
        TranslateError::HarnessFailed("model died mid-response".into()),
    );
    let cache = Cache::open_in_memory().unwrap();
    let (result, seen, _delivered, snapshots, calls) =
        run_scripted(vec![script], &us, Some(&cache), None).await;

    let err = result.expect_err("尾部失败必须上抛，不得宣称全篇成功");
    assert!(
        matches!(err, TranslateError::HarnessFailed(ref m) if m.contains("mid-response")),
        "{err:?}"
    );
    // 部分交付保留：失败前完整闭合的第一块已经交付，不撤回。
    assert_eq!(ids(&seen), [pid(1)]);
    assert!(seen[0].status.is_ok());
    assert_eq!(seen[0].html, block_html(1));
    // 未闭合的半个块不能进排版。
    assert!(!seen.iter().any(|b| b.id == pid(2)));
    // 已交付的有效块按协议入缓存：晚到的失败不追溯否定它们。
    assert_eq!(
        cache.get("en", "en", &us[0].html).unwrap().as_deref(),
        Some(block_html(1).as_str())
    );
    assert!(cache.get("en", "en", &us[1].html).unwrap().is_none());
    // 通道失败不进入拆分重试。
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(
        snapshots.lock().expect("snapshot mutex").clone(),
        vec![0, 1, 1],
        "交付进度：第二块始终未闭合"
    );
}

/// 重试只处理未通过块：首轮漏译的第二块由重试片补上；已交付块即使被
/// 模型重复吐出也不重复交付，DocumentResult 仍与输入同序。
#[tokio::test]
async fn retry_only_settles_missing_blocks_without_redelivery() {
    let us = vec![unit(1), unit(2)];
    let first = Script::ok(vec![format!("```html\n{}\n```\n", block_html(1))]);
    // 重试片：模型把已交付的第一块又吐一遍，再补上漏掉的第二块。
    let retry = Script::ok(vec![format!(
        "```html\n{}\n{}\n```\n",
        block_html(1),
        block_html(2)
    )]);
    let (result, seen, _delivered, snapshots, calls) =
        run_scripted(vec![first, retry], &us, None, None).await;
    let r = result.expect("重试补漏后应当全 Ok");
    assert_eq!(ids(&seen), [pid(1), pid(2)], "已交付块不重复交付");
    assert_eq!(ids(&r.blocks), [pid(1), pid(2)], "结果与输入同序");
    assert!(r.blocks.iter().all(|b| b.status.is_ok()));
    assert_eq!(r.stats.retry_rounds, 1);
    assert_eq!(r.stats.prompts, 2);
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    assert_eq!(
        r.stats.violations.get("duplicated_text_slots"),
        Some(&1),
        "重试片里重复的第一块被记账丢弃"
    );
    // 首片交付 1 块；重试片只新增第二块。
    assert_eq!(
        snapshots.lock().expect("snapshot mutex").clone(),
        vec![1, 2]
    );
}
