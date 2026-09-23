//! 编排：把各阶段按 `02-技术路径与架构.md` §6 的顺序串起来。
//!
//! 阶段顺序（每阶段前后发 `stage_started` / `stage_finished`）：
//! preflight → source_analysis → layout_analysis → paragraph_analysis →
//! translating → typesetting → validating → publishing。
//!
//! **流式编译**：翻译阶段每交付一个已校验段落就立刻排版；一页的段落全部落定
//! （或翻译结束回退）时立刻回写并发 `page_ready`。
//!
//! # 快照策略（关键设计 #3）
//!
//! 全程只有**一份**主 `lopdf::Document`：它是原文减去已删字形，**不写译文**。
//! 每次某页就绪时：对该页 `delete_translated`（删除成功段落的原字形），然后
//! `render_snapshot` 克隆主文档、重放**全部已就绪页**的译文、`finalize`、
//! 原子保存到 `output`。最终发布再走一次同样的路径并记 `FontStats`。
//!
//! # 记忆体
//!
//! `BoundPage` 含整页 `PageIR`，而删字形（`PatchSet::delete_glyphs`）必须用到
//! `PageIR`，因此各页 `BoundPage` 在 run 期间常驻。up-vns（12 页）无压力；
//! 篇幅很大的文档（如 up-2602，58 页）会占用较多记忆体，见回报的已知缺口。

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use syncpdf_core::ir::{
    PageIR, Paragraph, ParagraphStatus, Region, Translatable, TypesetParagraph,
};
use syncpdf_core::ParagraphId;
use syncpdf_font::{FontProfile, FontStore, Role};
use syncpdf_pdf::bind::BoundPage;
use syncpdf_pdf::writer::FontStats;
use syncpdf_protocol::{Event, Request, Severity, Stage, Stats, PROTOCOL_VERSION};
use syncpdf_store::Store;
use syncpdf_typeset::{Obstacles, TypesetIssue};

use crate::cancel::CancellationToken;
use crate::events::{event_kind, SharedSink};
use crate::schedule::PageSchedule;
use crate::stages::{
    self, analyze_page, apply_coverage_fallback, check_cancelled, delete_translated,
    detect_regions, load_fonts, make_translator, preflight, preflight::Preflight, render_snapshot,
    DynTranslator, LayoutOpts, PipelineError, StoreShaper,
};

/// 源解析阶段的缓存键（阶段缓存）。
const STAGE_SOURCE: &str = "source_analysis";
/// 取消轮询间隔（`tokio::select!` 的 tick）。
const CANCEL_POLL: Duration = Duration::from_millis(100);

/// 一次 run 的配置：`configure`（通道 / 缓存目录）+ `run`（输入输出 / 语言）。
#[derive(Debug, Clone)]
pub struct RunConfig {
    /// 首条 `configure` 请求（必须是 `Request::Configure`）。
    pub configure: Request,
    /// `run` 请求（必须是 `Request::Run`）。
    pub run: Request,
    /// Recompile verified cached blocks; missing blocks remain source.
    pub cache_only: bool,
    /// Explicit target-only scale/relative leading (source typography by default).
    pub typography: stages::typeset::Typography,
}

impl RunConfig {
    /// 从两条协议请求构造；类型不对时返回 `PipelineError::Protocol`。
    pub fn new(configure: Request, run: Request) -> Result<Self, PipelineError> {
        if !matches!(configure, Request::Configure { .. }) {
            return Err(PipelineError::Protocol("首条请求必须是 configure".into()));
        }
        if !matches!(run, Request::Run { .. }) {
            return Err(PipelineError::Protocol("第二条请求必须是 run".into()));
        }
        Ok(Self {
            configure,
            run,
            cache_only: false,
            typography: stages::typeset::Typography::default(),
        })
    }

    /// 便捷构造：只要 `run`，`configure` 用给定翻译器名。
    pub fn with_fake(name: &str, input: PathBuf, output: PathBuf) -> Result<Self, PipelineError> {
        let cfg = Request::Configure {
            provider: syncpdf_protocol::TranslateProvider::Http,
            base_url: None,
            model: String::new(),
            api_key: None,
            concurrency: 1,
            cache_dir: std::env::temp_dir().join("syncpdf-cache"),
            translator: syncpdf_protocol::TranslatorKind::Fake {
                name: name.to_string(),
            },
        };
        let run = Request::Run {
            doc_id: "cli".into(),
            input,
            output,
            source_lang: "auto".into(),
            target_lang: "zh-CN".into(),
            pages: None,
            font_profile: None,
            terminology: None,
            mode: syncpdf_protocol::Mode::Full,
        };
        Self::new(cfg, run)
    }

    /// 拆出 `Run` 请求的字段（`None` 表示请求类型意外）。
    pub fn run_fields(&self) -> Option<RunFields<'_>> {
        match &self.run {
            Request::Run {
                doc_id,
                input,
                output,
                source_lang,
                target_lang,
                pages,
                font_profile,
                terminology,
                mode,
            } => Some(RunFields {
                doc_id,
                input,
                output,
                source_lang,
                target_lang,
                pages: pages.as_deref(),
                font_profile: font_profile.as_deref(),
                terminology: terminology.as_deref(),
                mode: *mode,
            }),
            _ => None,
        }
    }

    /// 拆出 `Configure` 请求的翻译器。
    pub fn translator_kind(&self) -> Option<&syncpdf_protocol::TranslatorKind> {
        match &self.configure {
            Request::Configure { translator, .. } => Some(translator),
            _ => None,
        }
    }

    /// 拆出 `Configure` 请求的缓存目录。
    pub fn cache_dir(&self) -> Option<&Path> {
        match &self.configure {
            Request::Configure { cache_dir, .. } => Some(cache_dir),
            _ => None,
        }
    }
}

/// `Run` 请求的借用视图。
#[derive(Debug, Clone, Copy)]
pub struct RunFields<'a> {
    pub doc_id: &'a str,
    pub input: &'a Path,
    pub output: &'a Path,
    pub source_lang: &'a str,
    pub target_lang: &'a str,
    pub pages: Option<&'a [u32]>,
    pub font_profile: Option<&'a str>,
    pub terminology: Option<&'a Path>,
    pub mode: syncpdf_protocol::Mode,
}

/// run 结束时的汇总（对应 `run_finished` 与 `document_finished`）。
#[derive(Debug, Clone, PartialEq)]
pub struct RunSummary {
    /// 是否完整完成；已保存的部分结果（含翻译/排版回退）为 false。
    pub ok: bool,
    /// 处理的页数。
    pub pages: u32,
    /// 落定的段落数。
    pub paragraphs: u32,
    /// 总耗时（毫秒）。
    pub elapsed_ms: u64,
    /// 发布统计（与 `document_finished` 一致）。
    pub stats: Stats,
}

/// 编排器：持有字体 / 模型目录与阶段缓存的路径。
#[derive(Debug, Clone)]
pub struct Pipeline {
    /// 内置字体包目录（`engine/vendor/fonts`）。
    pub fonts_dir: PathBuf,
    /// 布局模型目录（`engine/vendor/models`）。
    pub models_dir: PathBuf,
    /// 阶段缓存数据库路径（`store.put_stage` / `get_stage`）。
    pub store_path: PathBuf,
    /// layout 阶段参数。
    pub layout_opts: LayoutOpts,
}

impl Default for Pipeline {
    fn default() -> Self {
        Self {
            fonts_dir: syncpdf_core::fixtures::fonts_dir().unwrap_or_default(),
            models_dir: syncpdf_core::fixtures::models_dir().unwrap_or_default(),
            store_path: std::env::temp_dir().join("syncpdf-pipeline-store.db"),
            layout_opts: LayoutOpts::default(),
        }
    }
}

impl Pipeline {
    /// 用给定目录构造。
    pub fn new(fonts_dir: PathBuf, models_dir: PathBuf, store_path: PathBuf) -> Self {
        Self {
            fonts_dir,
            models_dir,
            store_path,
            layout_opts: LayoutOpts::default(),
        }
    }

    /// 跑一次完整流程。事件经 `sink` 出，`cancel` 可随时中止。
    pub async fn run(
        &self,
        cfg: &RunConfig,
        mut sink: SharedSink,
        cancel: CancellationToken,
    ) -> Result<RunSummary, PipelineError> {
        let started = Instant::now();
        let fields = cfg
            .run_fields()
            .ok_or_else(|| PipelineError::Protocol("缺少 run 请求".into()))?;

        // ── 0. 打开文档（preflight）────────────────────────────────────
        let worker = match syncpdf_pdf::pdfium::PdfiumWorker::spawn() {
            Ok(w) => w,
            Err(e) => {
                let err = PipelineError::Pdfium(e);
                fail(&mut sink, &err, started);
                return Err(err);
            }
        };

        // `run_started` 必须是首个事件；真实页数要 preflight 后才知道，这里先
        // 用请求里选定的页数（`pages` 为子集时即子集大小，全篇记 0）。
        let declared_pages = fields.pages.map_or(0, |p| p.len() as u32);
        sink.emit(Event::RunStarted {
            protocol_version: PROTOCOL_VERSION,
            engine_version: syncpdf_protocol::ENGINE_VERSION.to_string(),
            doc_id: fields.doc_id.to_string(),
            pages: declared_pages,
        });

        let pf = match self.stage_preflight(&mut sink, &worker, fields.input, &cancel) {
            Ok(pf) => pf,
            Err(e) => {
                fail(&mut sink, &e, started);
                return Err(e);
            }
        };

        // 选定的页（**0 基**）；`None` = 全部页。
        let selected: Vec<u32> = match fields.pages {
            Some(ps) => ps.to_vec(),
            None => (0..pf.pages).collect(),
        };

        let result = self
            .run_stages(
                &mut sink, &worker, &pf, &fields, cfg, &cancel, &selected, started,
            )
            .await;

        worker.close(pf.doc);

        match result {
            Ok(summary) => Ok(summary),
            Err(e) => {
                fail(&mut sink, &e, started);
                Err(e)
            }
        }
    }

    /// preflight 阶段（发 stage_started/stage_finished）。
    fn stage_preflight(
        &self,
        sink: &mut SharedSink,
        worker: &syncpdf_pdf::pdfium::PdfiumWorker,
        input: &Path,
        cancel: &CancellationToken,
    ) -> Result<Preflight, PipelineError> {
        emit_stage_started(sink, Stage::Preflight);
        let t = Instant::now();
        check_cancelled(cancel)?;
        let pf = preflight(worker, input)?;
        emit_stage_finished(sink, Stage::Preflight, t);
        Ok(pf)
    }

    /// 其余阶段：源解析 → 版面 → 段落 → 翻译（流式排版/回写）→ 校验 → 发布。
    #[allow(clippy::too_many_arguments)]
    async fn run_stages(
        &self,
        sink: &mut SharedSink,
        worker: &syncpdf_pdf::pdfium::PdfiumWorker,
        pf: &Preflight,
        fields: &RunFields<'_>,
        cfg: &RunConfig,
        cancel: &CancellationToken,
        selected: &[u32],
        started: Instant,
    ) -> Result<RunSummary, PipelineError> {
        // ── 1. source_analysis（结果入阶段缓存）────────────────────────
        emit_stage_started(sink, Stage::SourceAnalysis);
        let t = Instant::now();
        check_cancelled(cancel)?;
        let main_doc = load_lopdf(fields.input)?;
        let store = open_store(&self.store_path)?;
        let bound_pages =
            stages::source_analysis(worker, pf.doc, &main_doc, selected, cancel, |d, n| {
                tracing::debug!(done = d, total = n, "source_analysis 进度");
            })?;
        let pages_ir = stages::source::page_irs(&bound_pages);
        for b in &bound_pages {
            emit_bind_issues(sink, b);
        }
        // 阶段缓存只在**全篇** run（`pages` 未指定）时读写：页子集的结果不能
        // 覆盖全篇缓存，否则后续全篇 run 会拿到缺页的缓存。命中时只发
        // `stage_cached`，**不回写**（免得把子集结果固化成「缓存命中」）。
        let full_doc = fields.pages.is_none();
        if full_doc {
            match store.get_stage::<Vec<PageIR>>(&pf.source_sha, STAGE_SOURCE) {
                Ok(Some(hit)) => sink.emit(Event::Issue {
                    severity: Severity::Info,
                    code: "stage_cached".into(),
                    paragraph_id: None,
                    page: None,
                    message: format!(
                        "source_analysis 命中阶段缓存（{} 页，sha256={}）",
                        hit.len(),
                        pf.source_sha.to_hex()
                    ),
                }),
                Ok(None) => {
                    if let Err(e) = store.put_stage(&pf.source_sha, STAGE_SOURCE, &pages_ir) {
                        tracing::warn!(error = %e, "写 source_analysis 缓存失败");
                    }
                }
                Err(e) => tracing::warn!(error = %e, "读 source_analysis 缓存失败，按未命中处理"),
            }
        }
        emit_stage_finished(sink, Stage::SourceAnalysis, t);

        // ── 2. layout_analysis（结果入阶段缓存）────────────────────────
        emit_stage_started(sink, Stage::LayoutAnalysis);
        let t = Instant::now();
        let asset = stages::layout_model::resolve(&self.models_dir)?;
        let device = stages::layout_model::parse_device(
            std::env::var("SYNCPDF_LAYOUT_DEVICE").ok().as_deref(),
        )?;
        let options = syncpdf_layout::session::LayoutOptions {
            threads: 2,
            device,
            cache_dir: cfg
                .cache_dir()
                .map(|dir| dir.join("layout-coreml").join(&asset.sha256)),
            profile_path: std::env::var_os("SYNCPDF_LAYOUT_PROFILE").map(PathBuf::from),
        };
        let mut model = syncpdf_layout::LayoutModel::load_with_options(&asset.path, options)
            .map_err(|e| PipelineError::Layout(e.to_string()))?;
        sink.emit(Event::Issue {
            severity: Severity::Info,
            code: "layout_backend".into(),
            paragraph_id: None,
            page: None,
            message: format!(
                "PP-DocLayout-V3 sha256={}；requested={device:?}；configured={:?}；model={}",
                asset.sha256,
                model.configured_device(),
                asset.path.display()
            ),
        });
        let mut fallback_reported = false;
        let mut per_page_regions: Vec<(u32, Vec<Region>)> = Vec::new();
        let mut coverage_gaps = 0u32;
        for (i, page_ir) in pages_ir.iter().enumerate() {
            check_cancelled(cancel)?;
            let page = page_ir.page.0;
            let cache_key = format!(
                "layout-v3:{}:{:?}:{}:{}:{page}",
                asset.sha256,
                model.configured_device(),
                self.layout_opts.dpi,
                self.layout_opts.score_threshold
            );
            let mut regions: Vec<Region> = match store
                .get_stage::<Vec<Region>>(&pf.source_sha, &cache_key)
            {
                Ok(Some(cached)) => {
                    if full_doc {
                        sink.emit(Event::Issue {
                            severity: Severity::Info,
                            code: "stage_cached".into(),
                            paragraph_id: None,
                            page: Some(page + 1),
                            message: format!("layout_analysis 命中阶段缓存（第 {} 页）", page + 1),
                        });
                    }
                    cached
                }
                Ok(None) => {
                    let regions = self.detect_page(&mut model, worker, pf, page)?;
                    if full_doc {
                        if let Err(e) = store.put_stage(&pf.source_sha, &cache_key, &regions) {
                            tracing::warn!(error = %e, "写 layout 缓存失败");
                        }
                    }
                    regions
                }
                Err(e) => {
                    tracing::warn!(error = %e, "读 layout 缓存失败，按未命中处理");
                    self.detect_page(&mut model, worker, pf, page)?
                }
            };
            if !fallback_reported {
                if let Some(reason) = model.fallback_reason() {
                    sink.emit(Event::Issue {
                        severity: Severity::Warning,
                        code: "layout_backend_fallback".into(),
                        paragraph_id: None,
                        page: Some(page + 1),
                        message: format!("CoreML不可用，本次后续布局使用CPU：{reason}"),
                    });
                    fallback_reported = true;
                }
            }
            let report = apply_coverage_fallback(
                &mut regions,
                page_ir,
                page,
                self.layout_opts.coverage_limit,
            );
            if report.ratio > self.layout_opts.coverage_limit {
                coverage_gaps += 1;
                sink.emit(Event::Issue {
                    severity: Severity::Warning,
                    code: "coverage_gap".into(),
                    paragraph_id: None,
                    page: Some(page + 1),
                    message: format!(
                        "未覆盖字形比例 {:.4} 超过门禁 {:.4}，无法证明区域归属的字形保留原文",
                        report.ratio, self.layout_opts.coverage_limit
                    ),
                });
            }
            sink.emit(Event::Progress {
                stage: Stage::LayoutAnalysis,
                done: i as u32 + 1,
                total: pages_ir.len() as u32,
            });
            per_page_regions.push((page, regions));
        }
        if let Some(profile) = model
            .end_profiling()
            .map_err(|e| PipelineError::Layout(e.to_string()))?
        {
            tracing::info!(path = %profile.display(), "layout execution profile saved");
        }
        emit_stage_finished(sink, Stage::LayoutAnalysis, t);

        // ── 3. paragraph_analysis ─────────────────────────────────────
        emit_stage_started(sink, Stage::ParagraphAnalysis);
        let t = Instant::now();
        let mut all_paras: Vec<Paragraph> = Vec::new();
        for (page, regions) in &per_page_regions {
            check_cancelled(cancel)?;
            let ir = pages_ir
                .iter()
                .find(|p| p.page.0 == *page)
                .ok_or_else(|| PipelineError::Protocol("区域所属页缺少 IR".into()))?;
            let mut paragraphs = analyze_page(ir, regions);
            stages::source_policy::protect_front_matter(ir, regions, &mut paragraphs);
            all_paras.extend(paragraphs);
        }
        for p in &all_paras {
            sink.emit(Event::Paragraph {
                paragraph_id: p.id.clone(),
                page: p.id.page,
                status: ParagraphStatus::Pending,
                boxes: Some(vec![p.bbox]),
                coord_system: syncpdf_core::CoordSystem::PdfUser,
                translated_html: None,
            });
        }
        emit_stage_finished(sink, Stage::ParagraphAnalysis, t);

        // 不可译段落（原子/数字/单字符）不参与翻译，直接落定 `not_replaced`。
        let (translatable, not_replaced): (Vec<Paragraph>, Vec<Paragraph>) = all_paras
            .iter()
            .cloned()
            .partition(|p| matches!(p.translatable, Translatable::Yes));
        for p in &not_replaced {
            if matches!(&p.translatable, Translatable::No { reason } if matches!(reason.as_str(), "protected_source_overlap" | "rotated_source_text" | "translatable_region_overlap"))
            {
                sink.emit(Event::Issue {
                    severity: Severity::Warning,
                    code: match &p.translatable {
                        Translatable::No { reason } => reason.clone(),
                        _ => unreachable!(),
                    },
                    paragraph_id: Some(p.id.clone()),
                    page: Some(p.id.page),
                    message: "源段落的重叠归属或旋转方向尚未可靠处理，已保留原文".into(),
                });
            }
            sink.emit(Event::Paragraph {
                paragraph_id: p.id.clone(),
                page: p.id.page,
                status: ParagraphStatus::NotReplaced,
                boxes: Some(vec![p.bbox]),
                coord_system: syncpdf_core::CoordSystem::PdfUser,
                translated_html: None,
            });
        }

        // ── 4. translating + typesetting（流式）────────────────────────
        emit_stage_started(sink, Stage::Translating);
        let t = Instant::now();

        let (font_store, font_profile) = load_fonts(&self.fonts_dir, fields.target_lang)?;
        let kind = cfg
            .translator_kind()
            .cloned()
            .ok_or_else(|| PipelineError::Protocol("configure 里缺少 translator".into()))?;
        let translator = make_translator(&kind)?;

        // 页就绪表（**1 基**页号）：先登记可译段落，再把选定页全部补进 expected。
        let mut schedule =
            PageSchedule::new(translatable.iter().map(|p| (p.id.page, p.id.clone())));
        for p in selected {
            schedule.add_page(p + 1);
        }

        let bound: BTreeMap<u32, BoundPage> =
            bound_pages.into_iter().map(|b| (b.ir.page.0, b)).collect();
        let page_heights: BTreeMap<u32, f32> = bound
            .iter()
            .map(|(p, b)| (*p, b.ir.media_box.height()))
            .collect();

        let mut frames = BTreeMap::new();
        for (page, b) in &bound {
            if let Some((_, regions)) = per_page_regions.iter().find(|(p, _)| p == page) {
                frames.extend(stages::frame::page_frames(&b.ir, regions, &all_paras));
            }
        }

        let state = Arc::new(Mutex::new(RunState {
            doc: main_doc,
            bound,
            frames,
            targets: BTreeMap::new(),
            typography: cfg.typography,
            typeset_by_page: BTreeMap::new(),
            page_heights,
            pars: all_paras
                .iter()
                .map(|p| (p.id.clone(), p.clone()))
                .collect(),
            font_store,
            font_profile,
            schedule,
            output: fields.output.to_path_buf(),
            src_chars: 0,
            tgt_chars: 0,
            fallbacks: 0,
            settled: 0,
            settled_ids: BTreeSet::new(),
            ready: Vec::new(),
            revision: 0,
            font_stats: None,
            callback_error: None,
        }));

        // 无段落的选定页（含只有不可译段落的页）先转就绪并回写。
        {
            let mut s = lock_state(&state);
            let empty = {
                let selected_1based: Vec<u32> = selected.iter().map(|p| p + 1).collect();
                s.schedule.pages_without_paragraphs(&selected_1based)
            };
            for p in empty {
                if s.schedule.mark_page(p).is_some() {
                    writeback_page(&mut s, p - 1, sink)?;
                }
            }
        }

        let cache = open_cache(cfg.cache_dir());
        let total = translatable.len() as u32;
        let spec = syncpdf_translate::PromptSpec::new(fields.source_lang, fields.target_lang);
        let lookup = stages::glyph_text_lookup(&pages_ir);

        let callback_failed = CancellationToken::new();
        let on_block = {
            let state = state.clone();
            let sink = sink.clone();
            let cancel = cancel.clone();
            let callback_failed = callback_failed.clone();
            move |block: syncpdf_translate::TranslatedBlock| {
                if cancel.is_cancelled() {
                    return;
                }
                let mut s = lock_state(&state);
                if s.callback_error.is_some() {
                    return;
                }
                if let Err(error) = handle_block(&mut s, &sink, block, total) {
                    s.callback_error = Some(error);
                    callback_failed.cancel();
                }
            }
        };

        let translated = tokio::select! {
            biased;
            () = cancel_watch(cancel) => Err(PipelineError::Cancelled),
            () = cancel_watch(&callback_failed) => Err(lock_state(&state).callback_error.take()
                .expect("callback_failed 只在保存错误后触发")),
            r = stages::translate::translate_all_with_cache_policy(
                DynTranslator::new(translator),
                &spec,
                &translatable,
                lookup,
                cache.as_ref(),
                on_block,
                cfg.cache_only,
            ) => r,
        };
        drop(pages_ir);
        check_cancelled(cancel)?;
        if let Some(error) = lock_state(&state).callback_error.take() {
            return Err(error);
        }
        let translated = translated?;
        sink.emit(Event::Issue {
            severity: Severity::Info,
            code: "translation_requests".into(),
            paragraph_id: None,
            page: None,
            message: format!(
                "Markdown 主请求 {} 次，补救请求 {} 次，缓存命中 {} 段",
                translated.stats.primary_prompts,
                translated.stats.retry_prompts,
                translated.stats.cache_hits
            ),
        });
        let identity_errors = !translated.extra_ids.is_empty()
            || translated
                .stats
                .violations
                .get("duplicated_text_slots")
                .copied()
                .unwrap_or(0)
                > 0;
        if identity_errors {
            sink.emit(Event::Issue {
                severity: Severity::Warning,
                code: "translation_identity".into(),
                paragraph_id: None,
                page: None,
                message: "模型输出含未知或重复块，相关额外输出未采用".into(),
            });
        }
        emit_stage_finished(sink, Stage::Translating, t);

        // ── 4b. typesetting：兜底把未凑齐的页转 ready 并回写 ────────────
        emit_stage_started(sink, Stage::Typesetting);
        let t = Instant::now();
        {
            let mut s = lock_state(&state);
            // 漏译（引擎没吐、或回调里被取消跳过）的段落补发 fallback。
            let missing: Vec<ParagraphId> = translatable
                .iter()
                .map(|p| p.id.clone())
                .filter(|id| !s.settled_ids.contains(id))
                .collect();
            for id in missing {
                if let Some(p) = s.pars.get(&id).cloned() {
                    s.fallbacks += 1;
                    let n = p.text.chars().count() as u64;
                    s.src_chars += n;
                    s.tgt_chars += n;
                    s.settled += 1;
                    s.settled_ids.insert(id.clone());
                    sink.emit(Event::Issue {
                        severity: Severity::Warning,
                        code: "translate_missing".into(),
                        paragraph_id: Some(id.clone()),
                        page: Some(id.page),
                        message: "翻译未交付该段，回退原文".into(),
                    });
                    sink.emit(Event::Paragraph {
                        paragraph_id: id.clone(),
                        page: id.page,
                        status: ParagraphStatus::Fallback,
                        boxes: Some(vec![p.bbox]),
                        coord_system: syncpdf_core::CoordSystem::PdfUser,
                        translated_html: None,
                    });
                }
                if let Some(page) = s.schedule.mark(&id) {
                    writeback_page(&mut s, page - 1, sink)?;
                }
            }
            let rest = s.schedule.force_ready_rest();
            for p in rest {
                writeback_page(&mut s, p - 1, sink)?;
            }
            let expected: BTreeSet<u32> = selected.iter().copied().collect();
            if s.ready.iter().copied().collect::<BTreeSet<_>>() != expected {
                return Err(PipelineError::Protocol("部分选定页尚未成功保存".into()));
            }
        }
        emit_stage_finished(sink, Stage::Typesetting, t);

        // ── 5. validating：对已完成的输出做 self_check + 链接对照 ───────
        emit_stage_started(sink, Stage::Validating);
        let t = Instant::now();
        let cjk_pages = lock_state(&state).cjk_pages();
        validate_output(sink, fields.output, &cjk_pages, fields.input)?;
        emit_stage_finished(sink, Stage::Validating, t);

        // ── 6. publishing：发布最后一个已保存并通过校验的快照 ───────────
        emit_stage_started(sink, Stage::Publishing);
        let t = Instant::now();
        let (summary_stats, settled) = {
            let s = lock_state(&state);
            (s.stats(), s.settled)
        };
        // 按策略保留的 reference/脚注等不是回退；保护冲突阻断的可译内容则未完成。
        let protected = not_replaced.iter().filter(|p| {
            matches!(&p.translatable, Translatable::No { reason } if matches!(reason.as_str(), "protected_source_overlap" | "rotated_source_text" | "translatable_region_overlap"))
        }).count();
        let ok = summary_stats.fallbacks == 0
            && protected == 0
            && coverage_gaps == 0
            && !identity_errors;
        if !ok {
            sink.emit(Event::Issue {
                severity: Severity::Warning,
                code: "translation_incomplete".into(),
                paragraph_id: None,
                page: None,
                message: format!("已保存部分结果：{} 段回退、{protected} 段源区域冲突、{coverage_gaps} 页覆盖缺口", summary_stats.fallbacks),
            });
        }
        sink.emit(Event::DocumentFinished {
            output: fields.output.to_path_buf(),
            stats: summary_stats,
        });
        emit_stage_finished(sink, Stage::Publishing, t);

        let elapsed_ms = started.elapsed().as_millis() as u64;
        sink.emit(Event::RunFinished { ok, elapsed_ms });
        Ok(RunSummary {
            ok,
            pages: selected.len() as u32,
            paragraphs: settled,
            elapsed_ms,
            stats: summary_stats,
        })
    }

    /// 检测单页版面区域（`pf.page_infos` 是 0 基，`detect_regions` 收 0 基）。
    fn detect_page(
        &self,
        model: &mut syncpdf_layout::LayoutModel,
        worker: &syncpdf_pdf::pdfium::PdfiumWorker,
        pf: &Preflight,
        page: u32,
    ) -> Result<Vec<Region>, PipelineError> {
        let info = pf
            .page_infos
            .get(page as usize)
            .copied()
            .ok_or_else(|| PipelineError::Protocol(format!("页号越界：{page}")))?;
        detect_regions(model, worker, pf.doc, page, &info, &self.layout_opts)
    }
}

/// run 期间共享的可变状态（流式回调与主流程都经 `Arc<Mutex<..>>` 访问）。
///
/// 放进一个结构里，是为了让 `on_block` 闭包只捕获 `Arc` + `SharedSink` +
/// `CancellationToken`（都必然 `Send`）；字体、输出路径、页就绪表等随状态自身
/// 走，不必跨闭包借用。
struct RunState {
    /// 主文档：原文减去已删字形，**不写译文**（关键设计 #3）。
    doc: lopdf::Document,
    /// 页号（0 基）→ 绑定结果（删字形需要 `PageIR` 与 Form `Do` 记录）。
    bound: BTreeMap<u32, BoundPage>,
    /// 页号（0 基）→ 该页译文段落（按到达顺序）。
    typeset_by_page: BTreeMap<u32, Vec<TypesetParagraph>>,
    /// 页号（0 基）→ 页高（pt）。
    page_heights: BTreeMap<u32, f32>,
    /// 段落 id → 段落本体。
    pars: BTreeMap<ParagraphId, Paragraph>,
    frames: BTreeMap<ParagraphId, stages::frame::LayoutFrame>,
    targets: BTreeMap<ParagraphId, stages::link_text::Target>,
    typography: stages::typeset::Typography,
    /// 内置字体存储（`Writer` 需要的 `&FontStore`）。
    font_store: FontStore,
    /// 目标语言的默认字体 profile。
    font_profile: FontProfile,
    /// 页就绪判定（页号 1 基）。
    schedule: PageSchedule,
    /// 输出路径（每次快照都覆盖它）。
    output: PathBuf,
    /// 参与的原文总字符数（`expansion_ratio` 的分母）。
    src_chars: u64,
    /// 译文总字符数（回退段计入原文，分子）。
    tgt_chars: u64,
    /// 回退原文的段落数。
    fallbacks: u32,
    /// 已落定（译文通过或回退）的段落数。
    settled: u32,
    /// 已落定的段落 id（用于补齐漏译）。
    settled_ids: BTreeSet<ParagraphId>,
    /// 已就绪页（0 基，升序）。
    ready: Vec<u32>,
    /// `page_ready` 的 `revision` 计数。
    revision: u64,
    /// 最终发布的字体统计。
    font_stats: Option<FontStats>,
    /// 流式回调的首个回写错误；跨回调传播到主任务，后续块停止处理。
    callback_error: Option<PipelineError>,
}

impl RunState {
    fn cjk_pages(&self) -> Vec<u32> {
        self.ready
            .iter()
            .filter(|page| {
                self.typeset_by_page.get(page).is_some_and(|paras| {
                    paras
                        .iter()
                        .flat_map(|p| &p.lines)
                        .flat_map(|l| &l.glyphs)
                        .any(|g| g.text.chars().any(|c| matches!(c as u32, 0x4e00..=0x9fff)))
                })
            })
            .map(|page| page + 1)
            .collect()
    }
    /// `document_finished` 的统计。
    ///
    /// `expansion_ratio` 按 brief 取「译文总字符 / 原文总字符」（与
    /// `syncpdf_protocol::Stats` 的字段注释「输出/输入字节数之比」不一致，
    /// 见回报「已知缺口」）。
    fn stats(&self) -> Stats {
        let expansion_ratio = if self.src_chars == 0 {
            1.0
        } else {
            self.tgt_chars as f64 / self.src_chars as f64
        };
        Stats {
            fonts: self.font_stats.map_or(0, |f| f.fonts),
            expansion_ratio,
            fallbacks: self.fallbacks,
        }
    }
}

/// 锁住 `RunState`；中毒时取回内层（回调里 panic 不该连带整轮 run 失败）。
fn lock_state(state: &Arc<Mutex<RunState>>) -> MutexGuard<'_, RunState> {
    match state.lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    }
}

/// 每 100ms 检查一次取消令牌（`tokio::select!` 的另一臂）。
async fn cancel_watch(cancel: &CancellationToken) {
    loop {
        if cancel.is_cancelled() {
            return;
        }
        tokio::time::sleep(CANCEL_POLL).await;
    }
}

/// 打开翻译缓存（`cache_dir/translate.db`）；失败只记日志，按无缓存继续。
fn open_cache(dir: Option<&Path>) -> Option<syncpdf_translate::Cache> {
    let dir = dir?;
    if let Err(e) = std::fs::create_dir_all(dir) {
        tracing::warn!(error = %e, "缓存目录建不出来，按无缓存继续");
        return None;
    }
    match syncpdf_translate::Cache::open(&dir.join("translate.db")) {
        Ok(c) => Some(c),
        Err(e) => {
            tracing::warn!(error = %e, "翻译缓存打不开，按无缓存继续");
            None
        }
    }
}

/// 一个已落定块的处理：排版 + 事件 + 页就绪回写（关键设计 #2）。
///
/// 校验通过的块解析 HTML 后排版，存入 `typeset_by_page`（它的字形会在该页就绪
/// 时被删除）；解析失败/校验回退/空译一律**保留原文**（不删字形），并报 `issue`。
/// 每个块都会 `schedule.mark`，返回页号时立刻回写。
fn handle_block(
    state: &mut RunState,
    sink: &SharedSink,
    block: syncpdf_translate::TranslatedBlock,
    total: u32,
) -> Result<(), PipelineError> {
    let id = block.id.clone();
    if state.settled_ids.contains(&id) {
        return Ok(());
    }
    let Some(para) = state.pars.get(&id).cloned() else {
        return Err(PipelineError::Protocol(format!("收到未知段落：{id}")));
    };
    if !matches!(para.translatable, Translatable::Yes) {
        return Err(PipelineError::Protocol(format!("不可译段落收到译文：{id}")));
    }
    let src_len = para.text.chars().count() as u64;
    let mut fallback: Option<(&'static str, Option<String>, String)> = None;
    let mut out: Option<(String, Vec<syncpdf_core::Rect>)> = None;

    let parsed_result = syncpdf_translate::parse_unit_html(&block.html);
    let prepared = parsed_result.as_ref().ok().and_then(|parsed| {
        let bound = state.bound.get(&(id.page - 1))?;
        stages::link_text::prepare(&para, &bound.ir, &state.doc, parsed)
    });
    if parsed_result.is_ok() && block.status.is_ok() && prepared.is_none() {
        fallback = Some((
            if para.atoms.is_empty() {
                "link_target_unplaced"
            } else {
                "atom_source_unplaced"
            },
            None,
            "原子或链接目标无法唯一定位，保留原文".into(),
        ));
    } else if !state.frames.contains_key(&id) {
        fallback = Some((
            "layout_frame_missing",
            None,
            "无法确认安全排版框和源基线".into(),
        ));
    } else if block.status.is_ok() {
        match parsed_result {
            Ok(_) => {
                let target = prepared.expect("verified text and link anchors");
                if target.parsed.id != id {
                    return Err(PipelineError::Protocol(format!(
                        "译文块身份不一致：{id} / {}",
                        target.parsed.id
                    )));
                }
                state.targets.insert(id.clone(), target.clone());
                let shaper =
                    StoreShaper::new(&state.font_store, &state.font_profile).with_role(Role::Body);
                let result = stages::typeset::typeset_with_typography(
                    &shaper,
                    &target.para,
                    &target.parsed,
                    &Obstacles::default(),
                    state.frames.get(&id),
                    state.typography,
                );
                for issue in &result.issues {
                    match issue {
                        TypesetIssue::MinScaleHit => sink.emit(Event::Issue {
                            severity: Severity::Warning,
                            code: "typeset_min_scale".into(),
                            paragraph_id: Some(id.clone()),
                            page: Some(id.page),
                            message: "字号已降到下限，仍未能放下".into(),
                        }),
                        // 溢出在下方作为未替换结果处理，不登记为可删除原文的成功段。
                        TypesetIssue::Overflow { .. } => {}
                        TypesetIssue::Widened { by } => {
                            tracing::debug!(para = %id, by, "排版加宽生效");
                        }
                    }
                }
                if result.paragraph.overflow {
                    fallback = Some((
                        "typeset_overflow",
                        None,
                        "保持原文或指定字号时译文无法容纳，请调整译文或块的排版设置".into(),
                    ));
                } else if stages::link_text::geometry(&target, &result.paragraph, &shaper).is_none()
                {
                    fallback = Some(("link_target_unplaced", None, "链接缺少目标字形几何".into()));
                } else if result.paragraph.lines.is_empty() {
                    fallback = Some(("typeset_failed", None, "译文排不出任何行，回退原文".into()));
                } else {
                    let boxes = result.paragraph.lines.iter().map(|l| l.bbox).collect();
                    state
                        .typeset_by_page
                        .entry(id.page - 1)
                        .or_default()
                        .push(result.paragraph);
                    state.src_chars += src_len;
                    state.tgt_chars += target.parsed.text().chars().count() as u64;
                    out = Some((target.html, boxes));
                }
            }
            Err(e) => {
                fallback = Some((
                    "translate_invalid_html",
                    None,
                    format!("译文 HTML 解析失败：{e}"),
                ));
            }
        }
    } else {
        match &block.status {
            syncpdf_translate::BlockStatus::Fallback { violations } if violations.is_empty() => {
                fallback = Some(("translate_missing", None, "模型没有返回该段译文".into()));
            }
            syncpdf_translate::BlockStatus::Fallback { violations } => {
                let codes: Vec<String> = violations.iter().map(|v| v.code().to_string()).collect();
                fallback = Some((
                    "translate_fallback",
                    Some(codes.join(",")),
                    format!("校验未通过（{} 个违规）", violations.len()),
                ));
            }
            syncpdf_translate::BlockStatus::Empty => {
                fallback = Some(("translate_empty", None, "空译".into()));
            }
            syncpdf_translate::BlockStatus::Ok => {}
        }
    }

    match out {
        Some((html, boxes)) => sink.emit(Event::Paragraph {
            paragraph_id: id.clone(),
            page: id.page,
            status: ParagraphStatus::Typeset,
            boxes: Some(boxes),
            coord_system: syncpdf_core::CoordSystem::PdfUser,
            translated_html: Some(html),
        }),
        None => {
            let (code, detail, msg) =
                fallback.unwrap_or(("translate_fallback", None, "回退原文".into()));
            state.fallbacks += 1;
            state.src_chars += src_len;
            state.tgt_chars += src_len;
            let mut message = format!("{msg}，回退原文");
            if let Some(d) = detail {
                message.push_str(&format!("（违规：{d}）"));
            }
            sink.emit(Event::Issue {
                severity: Severity::Warning,
                code: code.into(),
                paragraph_id: Some(id.clone()),
                page: Some(id.page),
                message,
            });
            sink.emit(Event::Paragraph {
                paragraph_id: id.clone(),
                page: id.page,
                status: ParagraphStatus::Fallback,
                boxes: Some(vec![para.bbox]),
                coord_system: syncpdf_core::CoordSystem::PdfUser,
                translated_html: None,
            });
        }
    }

    state.settled += 1;
    state.settled_ids.insert(id.clone());
    sink.emit(Event::Progress {
        stage: Stage::Translating,
        done: state.settled,
        total,
    });

    if let Some(page) = state.schedule.mark(&id) {
        writeback_page(state, page - 1, sink)?;
    }
    Ok(())
}

/// 页就绪回写：删除该页成功段落的原字形，然后重放全部就绪页 → 快照 → 事件。
fn writeback_page(state: &mut RunState, page: u32, sink: &SharedSink) -> Result<(), PipelineError> {
    if state.ready.contains(&page) {
        return Ok(());
    }
    let RunState {
        doc,
        bound,
        typeset_by_page,
        targets,
        font_profile,
        page_heights,
        pars,
        font_store,
        output,
        ready,
        revision,
        font_stats,
        ..
    } = state;
    let b = bound
        .get(&page)
        .ok_or_else(|| PipelineError::Protocol(format!("就绪页 {} 缺少绑定结果", page + 1)))?;
    // 只删「译文排版成功」段落的字形；回退/未替换的段落保留原文。
    let ids: Vec<ParagraphId> = typeset_by_page
        .get(&page)
        .map(|v| v.iter().map(|t| t.id.clone()).collect())
        .unwrap_or_default();
    let paras: Vec<&Paragraph> = ids
        .iter()
        .map(|id| {
            pars.get(id)
                .ok_or_else(|| PipelineError::Protocol(format!("已排版段 {id} 缺少源段落")))
        })
        .collect::<Result<_, _>>()?;
    let mut candidate = doc.clone();
    delete_translated(&mut candidate, b, &paras)?;
    let shaper = StoreShaper::new(font_store, font_profile);
    for laid in typeset_by_page.get(&page).into_iter().flatten() {
        if let Some(target) = targets.get(&laid.id) {
            let plans = stages::link_text::geometry(target, laid, &shaper)
                .ok_or_else(|| PipelineError::Validation("链接缺少目标几何".into()))?;
            stages::link_text::apply(&mut candidate, &plans)?;
        }
    }
    let published: BTreeMap<_, _> = typeset_by_page
        .iter()
        .filter(|(p, _)| **p == page || ready.contains(p))
        .map(|(p, paras)| (*p, paras.clone()))
        .collect();
    let fs = render_snapshot(&candidate, font_store, &published, page_heights, output)?;
    *doc = candidate;
    *font_stats = Some(fs);
    *revision += 1;
    ready.push(page);
    ready.sort_unstable();
    sink.emit(Event::PageReady {
        page: page + 1,
        preview_path: None,
        revision: *revision,
    });
    Ok(())
}

/// bind 阶段的问题转 `issue`；文本对象数与 text-show 操作数不符另报一条。
fn emit_bind_issues(sink: &mut SharedSink, bound: &BoundPage) {
    let page = bound.ir.page.number();
    for msg in &bound.issues {
        sink.emit(Event::Issue {
            severity: Severity::Warning,
            code: "bind_degraded".into(),
            paragraph_id: None,
            page: Some(page),
            message: msg.clone(),
        });
    }
    if bound.stats.text_objects != bound.stats.text_ops {
        sink.emit(Event::Issue {
            severity: Severity::Warning,
            code: "bind_mismatch".into(),
            paragraph_id: None,
            page: Some(page),
            message: format!(
                "pdfium 文本对象 {} 个、内容流 text-show 操作 {} 个，数量不符",
                bound.stats.text_objects, bound.stats.text_ops
            ),
        });
    }
}

/// 输出校验：`self_check`（problems→Error / warnings→Warning）+ 链接对照。
fn validate_output(
    sink: &mut SharedSink,
    output: &Path,
    cjk_pages: &[u32],
    input: &Path,
) -> Result<(), PipelineError> {
    let mut problems = Vec::new();
    match syncpdf_pdf::validate::self_check(output, cjk_pages) {
        Ok(report) => {
            for p in &report.problems {
                sink.emit(Event::Issue {
                    severity: Severity::Error,
                    code: "self_check".into(),
                    paragraph_id: None,
                    page: None,
                    message: p.clone(),
                });
            }
            problems.extend(report.problems);
            for w in report.warnings {
                sink.emit(Event::Issue {
                    severity: Severity::Warning,
                    code: "self_check".into(),
                    paragraph_id: None,
                    page: None,
                    message: w,
                });
            }
        }
        Err(e) => {
            let message = format!("self_check 无法执行：{e}");
            sink.emit(Event::Issue {
                severity: Severity::Error,
                code: "self_check".into(),
                paragraph_id: None,
                page: None,
                message: message.clone(),
            });
            problems.push(message);
        }
    }
    let links = match (lopdf::Document::load(input), lopdf::Document::load(output)) {
        (Ok(a), Ok(b)) => syncpdf_pdf::links::links_check(&a, &b),
        (Err(e), _) => vec![format!("链接对照无法载入原文：{e}")],
        (_, Err(e)) => vec![format!("链接对照无法载入输出：{e}")],
    };
    for message in &links {
        sink.emit(Event::Issue {
            severity: Severity::Error,
            code: "links_check".into(),
            paragraph_id: None,
            page: None,
            message: message.clone(),
        });
    }
    problems.extend(links);
    if problems.is_empty() {
        Ok(())
    } else {
        Err(PipelineError::Validation(problems.join("；")))
    }
}

/// 用 lopdf 载入 PDF（`bind_page` 与回写都需要）。
fn load_lopdf(path: &Path) -> Result<lopdf::Document, PipelineError> {
    lopdf::Document::load(path).map_err(|e| PipelineError::Protocol(format!("lopdf 载入失败：{e}")))
}

/// 打开阶段缓存库。
fn open_store(path: &Path) -> Result<Store, PipelineError> {
    Store::open(path).map_err(|e| PipelineError::Store(e.to_string()))
}

fn emit_stage_started(sink: &mut SharedSink, stage: Stage) {
    sink.emit(Event::StageStarted { stage });
}

fn emit_stage_finished(sink: &mut SharedSink, stage: Stage, t: Instant) {
    sink.emit(Event::StageFinished {
        stage,
        elapsed_ms: t.elapsed().as_millis() as u64,
    });
}

/// 发错误事件 + `run_finished{ok:false}`（事件序列的收尾保证）。
fn fail(sink: &mut SharedSink, err: &PipelineError, started: Instant) {
    sink.emit(err.to_event());
    sink.emit(Event::RunFinished {
        ok: false,
        elapsed_ms: started.elapsed().as_millis() as u64,
    });
}

/// 事件类型名（转发，便于 cli 断言）。
pub fn kind_of(e: &Event) -> &'static str {
    event_kind(e)
}
#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::require_fixture;

    fn tmp_path(ext: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "syncpdf-pipeline-run-{}-{}.{ext}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    fn pipeline(tmp_store: PathBuf) -> Pipeline {
        Pipeline {
            fonts_dir: syncpdf_core::fixtures::fonts_dir().unwrap_or_default(),
            models_dir: syncpdf_core::fixtures::models_dir().unwrap_or_default(),
            store_path: tmp_store,
            layout_opts: LayoutOpts::default(),
        }
    }

    /// pdfium / 模型 / 字体任一缺失就跳过（返回 None）。
    fn env_ready() -> Option<()> {
        if syncpdf_pdf::pdfium::PdfiumWorker::spawn().is_err() {
            eprintln!("SKIP: pdfium 不可用");
            return None;
        }
        let models = syncpdf_core::fixtures::models_dir()?;
        if !models.join("pp_doc_layoutv3.onnx").is_file() {
            eprintln!("SKIP: 布局模型缺失：{models:?}");
            return None;
        }
        syncpdf_core::fixtures::fonts_dir()?;
        Some(())
    }

    #[test]
    fn run_config_rejects_wrong_request_order() {
        let cancel = Request::Cancel;
        let e = RunConfig::new(cancel.clone(), cancel).unwrap_err();
        assert!(matches!(e, PipelineError::Protocol(_)), "{e:?}");
    }

    #[test]
    fn run_config_with_fake_builds_valid_pair() {
        let cfg = RunConfig::with_fake("cjk", "/in.pdf".into(), "/out.pdf".into()).unwrap();
        assert!(cfg.translator_kind().is_some());
        assert!(cfg.cache_dir().is_some());
        assert_eq!(cfg.run_fields().unwrap().target_lang, "zh-CN");
    }

    /// 记录 `(seq, event)` 的汇；`seq` 由自身维护，与 `SharedSink` 一致。
    #[derive(Debug)]
    pub(super) struct RunRecorder {
        seq: u64,
        log: Arc<Mutex<Vec<(u64, Event)>>>,
    }

    impl RunRecorder {
        pub(super) fn new(log: Arc<Mutex<Vec<(u64, Event)>>>) -> Self {
            Self { seq: 0, log }
        }
    }

    impl crate::events::EventSink for RunRecorder {
        fn emit(&mut self, event: Event) {
            self.seq += 1;
            self.log.lock().expect("测试锁").push((self.seq, event));
        }
    }

    /// 端到端跑一次 ci-test：事件序列合法 + 输出存在 + 每页一次 `page_ready`。
    #[tokio::test]
    async fn run_emits_legal_event_sequence_on_ci_test() {
        if env_ready().is_none() {
            return;
        }
        let input = require_fixture!("ci-test.pdf");
        let out = tmp_path("pdf");
        let log: Arc<Mutex<Vec<(u64, Event)>>> = Arc::new(Mutex::new(Vec::new()));
        let shared = SharedSink::new(RunRecorder::new(log.clone()));
        let cfg = RunConfig::with_fake("cjk", input, out.clone()).unwrap();
        let p = pipeline(tmp_path("db"));
        let result = p.run(&cfg, shared.clone(), CancellationToken::new()).await;
        assert!(result.is_ok(), "端到端应成功：{result:?}");
        assert!(out.exists(), "输出文件应存在：{out:?}");

        let events = log.lock().unwrap().clone();
        assert!(!events.is_empty(), "至少要有事件");
        assert_eq!(
            crate::events::event_kind(&events[0].1),
            "run_started",
            "首个事件必须是 run_started"
        );
        assert_eq!(
            crate::events::event_kind(&events.last().unwrap().1),
            "run_finished",
            "末个事件必须是 run_finished"
        );
        match &events.last().unwrap().1 {
            Event::RunFinished { ok, .. } => assert!(ok, "应以 ok:true 收尾"),
            other => panic!("期望 run_finished，得到 {other:?}"),
        }
        // seq 从 1 起且单调递增。
        assert_eq!(events[0].0, 1);
        assert!(events.windows(2).all(|w| w[0].0 < w[1].0), "seq 必须单调");
        // 关键阶段事件齐全。
        assert!(events.iter().any(|(_, e)| matches!(
            e,
            Event::StageFinished {
                stage: Stage::Preflight,
                ..
            }
        )));
        assert!(
            events
                .iter()
                .any(|(_, e)| matches!(e, Event::DocumentFinished { .. })),
            "应有 document_finished"
        );
        let summary = result.unwrap();
        let ready: Vec<u32> = events
            .iter()
            .filter_map(|(_, e)| match e {
                Event::PageReady { page, .. } => Some(*page),
                _ => None,
            })
            .collect();
        assert_eq!(ready.len() as u32, summary.pages, "每页恰发一次 page_ready");
        assert_eq!(ready, vec![1], "ci-test 只有 1 页");
    }

    /// 取消：`fake:slow` + 立刻取消 → `run_finished{ok:false}`，且有 cancelled 事件。
    #[tokio::test]
    async fn cancellation_yields_run_finished_false() {
        if env_ready().is_none() {
            return;
        }
        let input = require_fixture!("ci-test.pdf");
        let out = tmp_path("pdf");
        let log: Arc<Mutex<Vec<(u64, Event)>>> = Arc::new(Mutex::new(Vec::new()));
        let shared = SharedSink::new(RunRecorder::new(log.clone()));
        let cfg = RunConfig::with_fake("slow:500", input, out.clone()).unwrap();
        let p = pipeline(tmp_path("db"));
        let cancel = CancellationToken::new();
        cancel.cancel();
        let result = p.run(&cfg, shared.clone(), cancel).await;
        assert!(result.is_err(), "取消后应是 Err：{result:?}");
        let events = log.lock().unwrap().clone();
        assert_eq!(
            crate::events::event_kind(&events.last().unwrap().1),
            "run_finished"
        );
        match &events.last().unwrap().1 {
            Event::RunFinished { ok, .. } => assert!(!ok, "取消应以 ok:false 收尾"),
            other => panic!("期望 run_finished，得到 {other:?}"),
        }
        assert!(
            events.iter().any(|(_, e)| matches!(
                e,
                Event::Error {
                    fatal: false,
                    code,
                    ..
                } if code == "cancelled"
            )),
            "应有 cancelled 错误事件"
        );
    }

    #[test]
    fn run_config_run_fields_match_input() {
        let cfg = RunConfig::with_fake("echo", "/a.pdf".into(), "/b.pdf".into()).unwrap();
        let f = cfg.run_fields().unwrap();
        assert_eq!(f.input, Path::new("/a.pdf"));
        assert_eq!(f.output, Path::new("/b.pdf"));
        assert_eq!(f.source_lang, "auto");
    }

    #[test]
    fn load_lopdf_reports_error_on_missing_file() {
        let e = load_lopdf(Path::new("/nonexistent/x.pdf")).unwrap_err();
        assert_eq!(e.code(), "protocol");
    }

    #[test]
    fn kind_of_matches_protocol_tag() {
        assert_eq!(
            kind_of(&Event::RunFinished {
                ok: true,
                elapsed_ms: 1
            }),
            "run_finished"
        );
        assert_eq!(
            kind_of(&Event::Error {
                fatal: false,
                code: "x".into(),
                message: "y".into()
            }),
            "error"
        );
    }

    #[test]
    fn oversized_translation_preserves_source_and_reports_fixed_size_failure() {
        use syncpdf_core::ir::{Align, RegionKind, StyleRun};
        use syncpdf_core::{Color, PageId, Rect, StyleId};
        let id = ParagraphId { page: 1, seq: 1 };
        let para = Paragraph {
            id: id.clone(),
            page: PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox: Rect::new(0.0, 0.0, 20.0, 12.0),
            lines: Vec::new(),
            glyphs: Vec::new(),
            text_spans: Vec::new(),
            style_runs: vec![StyleRun {
                id: StyleId(0),
                glyph_range: (0, 1),
                font: 0,
                size: 10.0,
                color: Color::default(),
                bold: false,
                italic: false,
                serif: false,
                mono: false,
            }],
            atoms: Vec::new(),
            text: "Source".into(),
            align: Align::Left,
            first_indent: 0.0,
            line_height: 12.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        };
        let fonts = syncpdf_core::fixtures::fonts_dir().expect("字体夹具必需");
        let (font_store, font_profile) = load_fonts(&fonts, "zh-CN").unwrap();
        // 另一段尚未到达：直接观察溢出块是否进入删除/写入队列。
        let schedule = PageSchedule::new(
            [id.clone(), ParagraphId { page: 1, seq: 2 }]
                .into_iter()
                .map(|id| (1, id)),
        );
        let dir = tmp_path("oversized-fixture");
        std::fs::create_dir_all(&dir).unwrap();
        let (fixture, _) = transaction_tests::state(&dir);
        let mut state = RunState {
            doc: fixture.doc,
            bound: fixture.bound,
            targets: BTreeMap::new(),
            typography: stages::typeset::Typography::default(),
            frames: [(
                id.clone(),
                stages::frame::LayoutFrame {
                    bbox: para.bbox,
                    first_baseline: para.bbox.y1 - 10.0,
                    obstacles: vec![],
                },
            )]
            .into_iter()
            .collect(),
            typeset_by_page: BTreeMap::new(),
            page_heights: BTreeMap::new(),
            pars: [(id.clone(), para)].into_iter().collect(),
            font_store,
            font_profile,
            schedule,
            output: tmp_path("pdf"),
            src_chars: 0,
            tgt_chars: 0,
            fallbacks: 0,
            settled: 0,
            settled_ids: BTreeSet::new(),
            ready: Vec::new(),
            revision: 0,
            font_stats: None,
            callback_error: None,
        };
        let log = Arc::new(Mutex::new(Vec::new()));
        let sink = SharedSink::new(RunRecorder::new(log.clone()));
        handle_block(
            &mut state,
            &sink,
            syncpdf_translate::TranslatedBlock {
                id: id.clone(),
                html: format!("<p id=\"{id}\">{}</p>", "译文".repeat(40)),
                status: syncpdf_translate::BlockStatus::Ok,
                from_cache: false,
            },
            2,
        )
        .unwrap();
        assert!(
            state.typeset_by_page.is_empty(),
            "溢出段不得进入删除原文队列"
        );
        assert_eq!(state.fallbacks, 1);
        assert!(state.settled_ids.contains(&id));
        assert_eq!(state.revision, 0);
        assert!(!state.output.exists());
        let log = log.lock().unwrap();
        assert!(log.iter().any(|(_, event)| matches!(event,
            Event::Issue { code, paragraph_id: Some(p), message, .. }
            if code == "typeset_overflow" && p == &id
                && message.contains("字号") && message.contains("回退原文"))));
        assert!(log.iter().any(|(_, event)| matches!(event,
            Event::Paragraph { paragraph_id, status: ParagraphStatus::Fallback, translated_html: None, .. }
            if paragraph_id == &id)));
        assert!(!log.iter().any(|(_, event)| matches!(
            event,
            Event::Paragraph {
                status: ParagraphStatus::Typeset,
                ..
            }
        )));
    }
}

#[cfg(test)]
#[path = "run/transaction_tests.rs"]
mod transaction_tests;
