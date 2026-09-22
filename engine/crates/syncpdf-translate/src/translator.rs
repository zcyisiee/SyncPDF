//! `Translator` trait 与翻译 `Engine`。
//!
//! 设计基准：02-技术路径与架构.md §5.2/§5.4/§5.5；现版规约 #1/#2/#3
//! （research/01-current-project-audit.md §7.1）。
//!
//! 流程（one-shot + 流式）：
//! 1. 缓存命中的单元直接 `on_block` 交付，**不进提示词**；
//! 2. 其余单元走 `build_document_prompts`（能一片就一片，超限按页切）；
//! 3. 逐片 `translate`，delta 喂 `BlockStream`；每凑齐一个块立刻 `validate`，
//!    通过则写缓存并 `on_block`（上游据此边译边排版）；
//! 4. 失败块 + 漏译块按 §5.4 二分拆分重试，最多 `max_retry_rounds`（默认 3）轮；
//! 5. 仍失败 → `Fallback`（html = 原文）；空译 → `Empty`；未知 id → `extra_ids`。
//!
//! 安全：提示词与模型输出正文绝不进 tracing 日志，只记 id、码与计数。

use std::collections::{BTreeMap, HashMap, HashSet};

use async_trait::async_trait;
use syncpdf_core::ParagraphId;

use crate::cache::Cache;
use crate::prompt::{build_document_prompts, DocumentPrompt, PromptSpec};
use crate::stream::BlockStream;
use crate::unit::Unit;
use crate::validate::{validate, ValidateCtx, Violation, DEFAULT_EXPANSION_LIMIT};

/// 翻译通道失败。错误文案**不含**提示词或模型输出正文。
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum TranslateError {
    /// 外部 CLI / HTTP 通道失败（退出码非 0、启动失败、回包不可解析）。
    #[error("translation harness failed: {0}")]
    HarnessFailed(String),
    /// 通道未就绪（可执行文件不存在、未登录）。
    #[error("translation harness unavailable: {0}")]
    Unavailable(String),
    /// 超时。
    #[error("translation harness timed out after {0:?}")]
    Timeout(std::time::Duration),
    /// 被取消。
    #[error("translation cancelled")]
    Cancelled,
}

/// 一个块的最终状态。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BlockStatus {
    /// 校验通过的译文。
    Ok,
    /// 回退原文：校验反复失败，或模型压根没吐这个 id（`violations` 为空即漏译）。
    Fallback { violations: Vec<Violation> },
    /// 空译：结构合法但正文为空，按规约 #3 回退原文并记 `empty_ids`。
    Empty,
}

impl BlockStatus {
    pub fn is_ok(&self) -> bool {
        matches!(self, BlockStatus::Ok)
    }
}

/// 交付给上游的一个块。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranslatedBlock {
    pub id: ParagraphId,
    /// `Ok` 时是译文 HTML；`Fallback`/`Empty` 时是原文 HTML。
    pub html: String,
    pub status: BlockStatus,
    /// 是否来自缓存（不计入 LLM 用量）。
    pub from_cache: bool,
}

/// 每段的邻段上下文与原子提示。
#[derive(Debug, Clone, Default)]
pub struct ContextMap {
    pub before: HashMap<ParagraphId, String>,
    pub after: HashMap<ParagraphId, String>,
    pub hints: HashMap<ParagraphId, Vec<String>>,
}

impl ContextMap {
    /// 用单元序列自动填 before/after（取相邻单元的纯文本）。
    pub fn from_units(units: &[Unit]) -> Self {
        let texts: Vec<String> = units.iter().map(|u| u.plain_text()).collect();
        let mut m = Self::default();
        for (i, u) in units.iter().enumerate() {
            if i > 0 {
                m.before.insert(u.id.clone(), texts[i - 1].clone());
            }
            if i + 1 < units.len() {
                m.after.insert(u.id.clone(), texts[i + 1].clone());
            }
        }
        m
    }

    /// 补上某段的原子提示（`ATOM_HINTS`，按 `AtomId` 升序）。
    pub fn with_hints(mut self, id: ParagraphId, hints: Vec<String>) -> Self {
        self.hints.insert(id, hints);
        self
    }

    fn ctx_for<'a>(&'a self, u: &'a Unit, target_lang: &'a str, limit: f32) -> ValidateCtx<'a> {
        const NONE: &str = "";
        const NO_HINTS: &[String] = &[];
        ValidateCtx {
            source: u,
            target_lang,
            context_before: self.before.get(&u.id).map(|s| s.as_str()).unwrap_or(NONE),
            context_after: self.after.get(&u.id).map(|s| s.as_str()).unwrap_or(NONE),
            atom_hints: self
                .hints
                .get(&u.id)
                .map(|v| v.as_slice())
                .unwrap_or(NO_HINTS),
            expansion_limit: limit,
        }
    }
}

/// 一次整文档翻译的统计。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Stats {
    pub units: usize,
    pub cache_hits: usize,
    /// 实际发出的提示词片数（含重试片）。
    pub prompts: usize,
    /// 实际执行的拆分重试轮数。
    pub retry_rounds: u32,
    /// 违规码 → 次数（含重试中被修好的）。
    pub violations: BTreeMap<String, u32>,
}

/// 整文档翻译结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentResult {
    /// 与输入 `units` 同序、同长。
    pub blocks: Vec<TranslatedBlock>,
    /// 回退原文的段（校验反复失败或漏译）。
    pub fallback_ids: Vec<ParagraphId>,
    /// 空译的段。
    pub empty_ids: Vec<ParagraphId>,
    /// 模型编出来的、不属于本次任务的段 id（一律不采用）。
    pub extra_ids: Vec<ParagraphId>,
    pub stats: Stats,
}

/// 流式增量回调。
///
/// `for<'s>` 必须显式写出：`async_trait` 会把签名里省略的生命周期具名化，
/// 那样回调就退化成「只接受某一个固定生命周期的 `&str`」，调用方连一个局部
/// `String` 的切片都传不进来。
pub type DeltaSink<'a> = &'a mut (dyn for<'s> FnMut(&'s str) + Send);

/// 翻译通道。实现者只管「把提示词发出去、把 delta 喂回来」。
#[async_trait]
pub trait Translator: Send + Sync {
    /// 发一片提示词；每收到一段增量文本就调一次 `on_delta`；返回完整回包文本。
    async fn translate(
        &self,
        prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError>;

    /// 通道标识（写进缓存 `origin`），如 `pi/deepseek-flash`。
    fn name(&self) -> &str;
}

/// 默认最大拆分重试轮数（§5.4）。
pub const DEFAULT_MAX_RETRY_ROUNDS: u32 = 3;

/// 翻译引擎：把 `Translator` 的流式输出变成逐段落定的结果。
pub struct Engine<T: Translator> {
    translator: T,
    expansion_limit: f32,
    max_retry_rounds: u32,
}

impl<T: Translator> std::fmt::Debug for Engine<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Engine")
            .field("translator", &self.translator.name())
            .field("expansion_limit", &self.expansion_limit)
            .field("max_retry_rounds", &self.max_retry_rounds)
            .finish()
    }
}

impl<T: Translator> Engine<T> {
    pub fn new(translator: T) -> Self {
        Self {
            translator,
            expansion_limit: DEFAULT_EXPANSION_LIMIT,
            max_retry_rounds: DEFAULT_MAX_RETRY_ROUNDS,
        }
    }

    pub fn with_expansion_limit(mut self, limit: f32) -> Self {
        self.expansion_limit = limit;
        self
    }

    pub fn with_max_retry_rounds(mut self, rounds: u32) -> Self {
        self.max_retry_rounds = rounds;
        self
    }

    pub fn translator(&self) -> &T {
        &self.translator
    }

    /// 翻译整份文档。`on_block` 在每个块落定的瞬间被调用（边译边排版）。
    pub async fn translate_document(
        &self,
        spec: &PromptSpec,
        units: Vec<Unit>,
        ctx: ContextMap,
        cache: Option<&Cache>,
        mut on_block: impl FnMut(TranslatedBlock) + Send,
    ) -> Result<DocumentResult, TranslateError> {
        let mut st = Stats {
            units: units.len(),
            ..Stats::default()
        };
        let known: HashSet<ParagraphId> = units.iter().map(|u| u.id.clone()).collect();
        let by_id: HashMap<ParagraphId, &Unit> = units.iter().map(|u| (u.id.clone(), u)).collect();
        let mut done: HashMap<ParagraphId, TranslatedBlock> = HashMap::new();
        let mut extra_ids: Vec<ParagraphId> = Vec::new();
        // 每段最近一次的违规码，用于组织重试提示词与最终 Fallback。
        let mut last_violations: HashMap<ParagraphId, Vec<Violation>> = HashMap::new();

        // ── 1. 缓存命中直接交付，不进提示词 ─────────────────────────────
        let mut pending: Vec<Unit> = Vec::new();
        for u in &units {
            let hit = cache
                .and_then(|c| c.get(&spec.source_lang, &spec.target_lang, &u.html).ok())
                .flatten();
            match hit {
                Some(html) => {
                    st.cache_hits += 1;
                    let b = TranslatedBlock {
                        id: u.id.clone(),
                        html,
                        status: BlockStatus::Ok,
                        from_cache: true,
                    };
                    done.insert(u.id.clone(), b.clone());
                    on_block(b);
                }
                None => pending.push(u.clone()),
            }
        }

        // ── 2. 首轮：整文档 one-shot（超限按页切） ───────────────────────
        if !pending.is_empty() {
            for p in build_document_prompts(spec, &pending, &ctx.hints) {
                st.prompts += 1;
                self.run_prompt(
                    &p,
                    spec,
                    &ctx,
                    cache,
                    &by_id,
                    &known,
                    &mut done,
                    &mut extra_ids,
                    &mut last_violations,
                    &mut st,
                    &mut on_block,
                )
                .await?;
            }
        }

        // ── 3. 拆分重试：按段二分，最多 max_retry_rounds 轮（§5.4）────────
        let mut groups: Vec<Vec<Unit>> = {
            let rest: Vec<Unit> = pending
                .iter()
                .filter(|u| !done.contains_key(&u.id))
                .cloned()
                .collect();
            if rest.is_empty() {
                Vec::new()
            } else {
                vec![rest]
            }
        };
        let mut round = 0u32;
        while round < self.max_retry_rounds && !groups.is_empty() {
            round += 1;
            st.retry_rounds = round;
            let mut next: Vec<Vec<Unit>> = Vec::new();
            for g in groups {
                for half in split_in_half(g) {
                    let codes: Vec<&str> = collect_codes(&half, &last_violations);
                    for p in build_document_prompts(spec, &half, &ctx.hints) {
                        st.prompts += 1;
                        let p = p.with_repair_note(&codes);
                        self.run_prompt(
                            &p,
                            spec,
                            &ctx,
                            cache,
                            &by_id,
                            &known,
                            &mut done,
                            &mut extra_ids,
                            &mut last_violations,
                            &mut st,
                            &mut on_block,
                        )
                        .await?;
                    }
                    let still: Vec<Unit> = half
                        .into_iter()
                        .filter(|u| !done.contains_key(&u.id))
                        .collect();
                    if !still.is_empty() {
                        next.push(still);
                    }
                }
            }
            groups = next;
        }

        // ── 4. 仍未落定 → Fallback 原文（含漏译）──────────────────────────
        for u in &units {
            if done.contains_key(&u.id) {
                continue;
            }
            let violations = last_violations.get(&u.id).cloned().unwrap_or_default();
            let b = TranslatedBlock {
                id: u.id.clone(),
                html: u.html.clone(),
                status: BlockStatus::Fallback { violations },
                from_cache: false,
            };
            done.insert(u.id.clone(), b.clone());
            on_block(b);
        }

        // ── 5. 汇总（保持输入顺序）──────────────────────────────────────
        let mut blocks = Vec::with_capacity(units.len());
        let mut fallback_ids = Vec::new();
        let mut empty_ids = Vec::new();
        for u in &units {
            let b = done
                .remove(&u.id)
                .expect("每个单元在第 4 步之后都必定有结果");
            match &b.status {
                BlockStatus::Fallback { .. } => fallback_ids.push(u.id.clone()),
                BlockStatus::Empty => empty_ids.push(u.id.clone()),
                BlockStatus::Ok => {}
            }
            blocks.push(b);
        }
        extra_ids.sort();
        extra_ids.dedup();

        tracing::debug!(
            units = st.units,
            cache_hits = st.cache_hits,
            prompts = st.prompts,
            retry_rounds = st.retry_rounds,
            fallbacks = fallback_ids.len(),
            empties = empty_ids.len(),
            extras = extra_ids.len(),
            "translate_document finished"
        );

        Ok(DocumentResult {
            blocks,
            fallback_ids,
            empty_ids,
            extra_ids,
            stats: st,
        })
    }

    /// 发一片提示词，把流式块逐个校验并交付。
    #[allow(clippy::too_many_arguments)]
    async fn run_prompt(
        &self,
        prompt: &DocumentPrompt,
        spec: &PromptSpec,
        ctx: &ContextMap,
        cache: Option<&Cache>,
        by_id: &HashMap<ParagraphId, &Unit>,
        known: &HashSet<ParagraphId>,
        done: &mut HashMap<ParagraphId, TranslatedBlock>,
        extra_ids: &mut Vec<ParagraphId>,
        last_violations: &mut HashMap<ParagraphId, Vec<Violation>>,
        st: &mut Stats,
        on_block: &mut (impl FnMut(TranslatedBlock) + Send),
    ) -> Result<(), TranslateError> {
        // 先把整片流成块收下来，再统一裁决：`translate` 的回调不能借 `done`。
        let mut stream = BlockStream::new();
        let mut raw = Vec::new();
        {
            let mut sink = |d: &str| raw.extend(stream.push(d));
            self.translator.translate(prompt, &mut sink).await?;
        }
        let (tail, _residue) = stream.finish();
        raw.extend(tail);

        for block in raw {
            if !known.contains(&block.id) {
                // 规约 #1：段外 id 一律不采用。
                extra_ids.push(block.id.clone());
                bump(st, Violation::UnknownParagraph);
                continue;
            }
            if done.contains_key(&block.id) {
                // 规约 #1：同一 id 出现两次，后到的丢弃。
                bump(st, Violation::DuplicatedTextSlots);
                continue;
            }
            let Some(u) = by_id.get(&block.id) else {
                continue;
            };
            let vctx = ctx.ctx_for(u, &spec.target_lang, self.expansion_limit);
            match validate(&vctx, &block.html) {
                Ok(parsed) => {
                    let b = if parsed.text().trim().is_empty() && !u.plain_text().trim().is_empty()
                    {
                        // 规约 #3：空译回退原文并留证据。
                        TranslatedBlock {
                            id: block.id.clone(),
                            html: u.html.clone(),
                            status: BlockStatus::Empty,
                            from_cache: false,
                        }
                    } else {
                        if let Some(c) = cache {
                            let _ = c.put(
                                &spec.source_lang,
                                &spec.target_lang,
                                self.translator.name(),
                                &u.html,
                                &block.html,
                            );
                        }
                        TranslatedBlock {
                            id: block.id.clone(),
                            html: block.html.clone(),
                            status: BlockStatus::Ok,
                            from_cache: false,
                        }
                    };
                    done.insert(block.id.clone(), b.clone());
                    on_block(b);
                }
                Err(violations) => {
                    for v in &violations {
                        bump(st, *v);
                    }
                    tracing::debug!(
                        id = %block.id,
                        codes = %violations.iter().map(|v| v.code()).collect::<Vec<_>>().join(","),
                        "translation block rejected"
                    );
                    last_violations.insert(block.id.clone(), violations);
                }
            }
        }
        Ok(())
    }
}

fn bump(st: &mut Stats, v: Violation) {
    *st.violations.entry(v.code().to_string()).or_insert(0) += 1;
}

/// 本组里出现过的违规码（去重、稳定序），拼进重试提示词。
fn collect_codes(units: &[Unit], last: &HashMap<ParagraphId, Vec<Violation>>) -> Vec<&'static str> {
    let mut set: std::collections::BTreeSet<Violation> = Default::default();
    for u in units {
        if let Some(vs) = last.get(&u.id) {
            set.extend(vs.iter().copied());
        }
    }
    set.into_iter().map(|v| v.code()).collect()
}

/// §5.4 按段二分；单段组不再切（进入「整段重试」），交由轮数上限兜底。
fn split_in_half(g: Vec<Unit>) -> Vec<Vec<Unit>> {
    if g.len() <= 1 {
        return vec![g];
    }
    let mid = g.len().div_ceil(2);
    let mut a = g;
    let b = a.split_off(mid);
    vec![a, b]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::{FakeTranslator, RecordingTranslator};
    use crate::prompt::tests::unit as mkunit;

    /// 段落正文里**不放数字**：否则 `EmptyBody` / `Shrink` 会先撞
    /// `protected_literal_count`，测不到想测的分支。
    const WORDS: [&str; 6] = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"];

    fn spec() -> PromptSpec {
        PromptSpec::new("en", "en")
    }

    fn units(n: u32) -> Vec<Unit> {
        (1..=n)
            .map(|i| {
                let w = WORDS[(i as usize - 1) % WORDS.len()];
                Unit {
                    id: format!("P01-{i:03}").parse().unwrap(),
                    html: format!(
                        "<p id=\"P01-{i:03}\">The {w} paragraph of the sample document</p>"
                    ),
                    styles: 0,
                    atoms: vec![],
                    breaks: 0,
                }
            })
            .collect()
    }

    async fn run(
        t: FakeTranslator,
        us: Vec<Unit>,
        cache: Option<&Cache>,
    ) -> (DocumentResult, Vec<TranslatedBlock>) {
        let engine = Engine::new(t);
        let ctx = ContextMap::from_units(&us);
        let mut seen = Vec::new();
        let r = engine
            .translate_document(&spec(), us, ctx, cache, |b| seen.push(b))
            .await
            .unwrap();
        (r, seen)
    }

    #[tokio::test]
    async fn echo_translates_five_units_all_ok() {
        let us = units(5);
        let (r, seen) = run(FakeTranslator::Echo, us.clone(), None).await;
        assert_eq!(r.blocks.len(), 5);
        assert!(r.blocks.iter().all(|b| b.status.is_ok()), "{:?}", r.blocks);
        assert!(r.fallback_ids.is_empty());
        assert!(r.empty_ids.is_empty());
        assert!(r.extra_ids.is_empty());
        assert_eq!(r.stats.prompts, 1, "one-shot：应该只发一片");
        assert_eq!(r.stats.retry_rounds, 0);
        // 流式交付顺序 == 文档顺序。
        assert_eq!(
            seen.iter().map(|b| b.id.clone()).collect::<Vec<_>>(),
            us.iter().map(|u| u.id.clone()).collect::<Vec<_>>()
        );
    }

    #[tokio::test]
    async fn fail_every_two_triggers_split_retry_and_fallback() {
        // seq 为 2 的倍数的段永远失败 → 拆分重试用光后回退原文。
        let us = units(5);
        let (r, _) = run(FakeTranslator::FailEvery(2), us.clone(), None).await;
        assert_eq!(r.blocks.len(), 5);
        assert_eq!(
            r.fallback_ids,
            vec![
                "P01-002".parse::<ParagraphId>().unwrap(),
                "P01-004".parse::<ParagraphId>().unwrap()
            ]
        );
        assert_eq!(r.stats.retry_rounds, DEFAULT_MAX_RETRY_ROUNDS);
        assert!(r.stats.prompts > 1, "应该发生了拆分重试");
        assert!(r.stats.violations.contains_key("invalid_markup"));
        for b in &r.blocks {
            let u = us.iter().find(|u| u.id == b.id).unwrap();
            match &b.status {
                BlockStatus::Fallback { violations } => {
                    assert_eq!(b.html, u.html, "回退必须是原文");
                    assert!(!violations.is_empty());
                }
                BlockStatus::Ok => assert_eq!(b.html, u.html, "Echo 路径原样返回"),
                BlockStatus::Empty => panic!("不该有空译"),
            }
        }
    }

    #[tokio::test]
    async fn missing_ids_fall_back_with_no_violation_code() {
        // 彻底漏译：模型一个块都不吐 → 重试用光后全部回退原文，且无违规码
        //（违规码为空正是「漏译」与「校验失败」的区分标志）。
        let us = units(2);
        let (r, _) = run(FakeTranslator::Truncate(0), us.clone(), None).await;
        assert_eq!(
            r.fallback_ids,
            us.iter().map(|u| u.id.clone()).collect::<Vec<_>>()
        );
        assert_eq!(r.stats.retry_rounds, DEFAULT_MAX_RETRY_ROUNDS);
        for (b, u) in r.blocks.iter().zip(&us) {
            assert!(
                matches!(&b.status, BlockStatus::Fallback { violations } if violations.is_empty())
            );
            assert_eq!(b.html, u.html);
        }
    }

    #[tokio::test]
    async fn split_retry_rescues_partially_missing_ids() {
        // 模型每片只吐前两块：首轮漏掉后两段，拆分重试后每片都能吐全。
        let us = units(4);
        let (r, _) = run(FakeTranslator::Truncate(2), us.clone(), None).await;
        assert!(r.fallback_ids.is_empty(), "{:?}", r.fallback_ids);
        assert!(r.blocks.iter().all(|b| b.status.is_ok()));
        assert!(r.stats.retry_rounds >= 1, "应该发生了拆分重试");
    }

    #[tokio::test]
    async fn unknown_ids_are_recorded_and_dropped() {
        let us = units(3);
        let (r, _) = run(FakeTranslator::ExtraBlock, us.clone(), None).await;
        assert_eq!(r.blocks.len(), 3, "只交付本次任务的段");
        assert_eq!(r.extra_ids, vec!["P42-999".parse::<ParagraphId>().unwrap()]);
        assert!(r.blocks.iter().all(|b| b.status.is_ok()));
    }

    #[tokio::test]
    async fn duplicated_ids_keep_the_first_block() {
        let us = units(2);
        let (r, seen) = run(FakeTranslator::DuplicateFirst, us.clone(), None).await;
        assert_eq!(seen.len(), 2, "重复 id 不重复交付");
        assert_eq!(r.stats.violations.get("duplicated_text_slots"), Some(&1));
        assert!(r.blocks.iter().all(|b| b.status.is_ok()));
    }

    #[tokio::test]
    async fn empty_translation_falls_back_and_is_recorded() {
        let us = units(2);
        let (r, _) = run(FakeTranslator::EmptyBody, us.clone(), None).await;
        assert_eq!(r.empty_ids.len(), 2);
        for b in &r.blocks {
            assert_eq!(b.status, BlockStatus::Empty);
            let u = us.iter().find(|u| u.id == b.id).unwrap();
            assert_eq!(b.html, u.html, "空译回退原文");
        }
    }

    #[tokio::test]
    async fn cache_hits_skip_the_prompt_entirely() {
        let us = units(3);
        let cache = Cache::open_in_memory().unwrap();
        // 预置第 2 段的缓存。
        cache
            .put(
                "en",
                "en",
                "manual-test",
                &us[1].html,
                "<p id=\"P01-002\">cached</p>",
            )
            .unwrap();

        let engine = Engine::new(RecordingTranslator::new(FakeTranslator::Echo));
        let ctx = ContextMap::from_units(&us);
        let r = engine
            .translate_document(&spec(), us.clone(), ctx, Some(&cache), |_| {})
            .await
            .unwrap();

        assert_eq!(r.stats.cache_hits, 1);
        let cached = r.blocks.iter().find(|b| b.id == us[1].id).unwrap();
        assert!(cached.from_cache);
        assert_eq!(cached.html, "<p id=\"P01-002\">cached</p>");
        // 缓存命中的单元不在提示词里。
        let seen = engine.translator().recorded();
        assert_eq!(seen.len(), 1);
        assert!(!seen[0].contains("<p id=\"P01-002\">"));
        assert!(seen[0].contains("<p id=\"P01-001\">"));
        assert!(seen[0].contains("<p id=\"P01-003\">"));
    }

    #[tokio::test]
    async fn successful_blocks_are_written_to_the_cache() {
        let us = units(2);
        let cache = Cache::open_in_memory().unwrap();
        let (r, _) = run(FakeTranslator::Echo, us.clone(), Some(&cache)).await;
        assert!(r.blocks.iter().all(|b| b.status.is_ok()));
        assert_eq!(cache.len().unwrap(), 2);
        assert!(cache.get("en", "en", &us[0].html).unwrap().is_some());
    }

    #[tokio::test]
    async fn harness_failure_propagates() {
        let engine = Engine::new(FakeTranslator::Broken);
        let us = units(2);
        let ctx = ContextMap::from_units(&us);
        let err = engine
            .translate_document(&spec(), us, ctx, None, |_| {})
            .await
            .unwrap_err();
        assert!(matches!(err, TranslateError::HarnessFailed(_)));
    }

    #[tokio::test]
    async fn no_units_is_a_no_op() {
        let (r, seen) = run(FakeTranslator::Echo, vec![], None).await;
        assert!(r.blocks.is_empty());
        assert!(seen.is_empty());
        assert_eq!(r.stats.prompts, 0);
    }

    #[test]
    fn split_in_half_is_balanced_and_lossless() {
        let g: Vec<Unit> = (1..=5).map(|i| mkunit(&format!("P01-00{i}"), 3)).collect();
        let parts = split_in_half(g.clone());
        assert_eq!(parts.len(), 2);
        assert_eq!(parts[0].len(), 3);
        assert_eq!(parts[1].len(), 2);
        let flat: Vec<_> = parts.concat();
        assert_eq!(flat, g);
        assert_eq!(split_in_half(vec![g[0].clone()]).len(), 1);
    }

    #[test]
    fn context_map_links_neighbours() {
        let us = units(3);
        let m = ContextMap::from_units(&us);
        assert!(!m.before.contains_key(&us[0].id));
        assert_eq!(m.before.get(&us[1].id).unwrap(), &us[0].plain_text());
        assert_eq!(m.after.get(&us[1].id).unwrap(), &us[2].plain_text());
        assert!(!m.after.contains_key(&us[2].id));
    }
}
