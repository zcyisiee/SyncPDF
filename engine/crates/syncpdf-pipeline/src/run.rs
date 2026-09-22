//! 编排：把各阶段按 `02-技术路径与架构.md` §6 的顺序串起来。
//!
//! 阶段顺序（每阶段前后发 `stage_started` / `stage_finished`）：
//! preflight → source_analysis → layout_analysis → paragraph_analysis →
//! translating → typesetting → validating → publishing。
//!
//! **流式编译**：翻译阶段每交付一个已校验段落就立刻排版、写入该页；
//! 一页的段落全部落定（或翻译结束回退）时立即回写并发 `page_ready`。
//!
//! 阶段 1 现状：`source_analysis` 依赖尚未合入的 `syncpdf-pdf::bind_page`，
//! 因此端到端会在此处提前返回 `NotYetAvailable`；但
//! `run_started` / `stage_started` / `error` / `run_finished{ok:false}`
//! 的事件序列仍然完整且合法。

use std::path::{Path, PathBuf};
use std::time::Instant;

use syncpdf_core::ir::Paragraph;
use syncpdf_protocol::{Event, Request, Stage, Stats, PROTOCOL_VERSION};
use syncpdf_store::Store;

use crate::cancel::CancellationToken;
use crate::events::{event_kind, SharedSink};
use crate::schedule::PageSchedule;
use crate::stages::{
    self, apply_coverage_fallback, check_cancelled, detect_regions, load_fonts, make_translator,
    preflight, regions_from_detections, preflight::Preflight, LayoutOpts, PipelineError,
};

/// 一次 run 的配置：`configure`（通道 / 缓存目录）+ `run`（输入输出 / 语言）。
///
/// 两个字段保留原始协议请求，方便阶段 2 从里面取 `translator`、`cache_dir`、
/// `font_profile` 等；阶段 1 只读其中一部分。
#[derive(Debug, Clone)]
pub struct RunConfig {
    /// 首条 `configure` 请求（必须是 `Request::Configure`）。
    pub configure: Request,
    /// `run` 请求（必须是 `Request::Run`）。
    pub run: Request,
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
        Ok(Self { configure, run })
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
    /// 任务是否成功完成。
    pub ok: bool,
    /// 处理的页数。
    pub pages: u32,
    /// 落定的段落数。
    pub paragraphs: u32,
    /// 总耗时（毫秒）。
    pub elapsed_ms: u64,
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
    /// 用默认目录构造。
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

        // run_started 必须是首个事件；页数在 preflight 前未知，先用请求里
        // 选定的页数（`pages` 子集时即是子集大小，全篇时是 0，preflight 后
        // 再用真实页数纠正为 `progress` 的 total）。
        let declared_pages = fields.pages.map_or(0, |p| p.len() as u32);
        sink.emit(Event::RunStarted {
            protocol_version: PROTOCOL_VERSION,
            engine_version: syncpdf_protocol::ENGINE_VERSION.to_string(),
            doc_id: fields.doc_id.to_string(),
            pages: declared_pages,
        });

        let pf = match self.stage_preflight(&mut sink, &worker, fields.input, &cancel, started) {
            Ok(pf) => pf,
            Err(e) => {
                fail(&mut sink, &e, started);
                return Err(e);
            }
        };

        // 选定的页（0 基）；`None` = 全部页。
        let selected: Vec<u32> = match fields.pages {
            Some(ps) => ps.to_vec(),
            None => (0..pf.pages).collect(),
        };

        let result = self
            .run_stages(&mut sink, &worker, &pf, &fields, &cancel, &selected, started)
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
        started: Instant,
    ) -> Result<Preflight, PipelineError> {
        emit_stage_started(sink, Stage::Preflight);
        let t = Instant::now();
        check_cancelled(cancel)?;
        let pf = preflight(worker, input)?;
        emit_stage_finished(sink, Stage::Preflight, t);
        let _ = started;
        Ok(pf)
    }

    /// 其余阶段：源解析 → 版面 → 段落 → 翻译 → 排版 → 校验 → 发布。
    #[allow(clippy::too_many_arguments)]
    async fn run_stages(
        &self,
        sink: &mut SharedSink,
        worker: &syncpdf_pdf::pdfium::PdfiumWorker,
        pf: &Preflight,
        fields: &RunFields<'_>,
        cancel: &CancellationToken,
        selected: &[u32],
        started: Instant,
    ) -> Result<RunSummary, PipelineError> {
        // ── 1. source_analysis（阶段 2 接线点）──────────────────────────
        emit_stage_started(sink, Stage::SourceAnalysis);
        let t = Instant::now();
        check_cancelled(cancel)?;
        let mut doc = load_lopdf(fields.input)?;
        let pages_ir = stages::source_analysis(worker, &mut doc, selected, cancel)?;
        emit_stage_finished(sink, Stage::SourceAnalysis, t);

        // ── 2. layout_analysis（结果入阶段缓存）────────────────────────
        emit_stage_started(sink, Stage::LayoutAnalysis);
        let t = Instant::now();
        let store = open_store(&self.store_path)?;
        let model_path = self.models_dir.join("pp_doc_layoutv3.onnx");
        let mut model = syncpdf_layout::LayoutModel::load(&model_path, 2)
            .map_err(|e| PipelineError::Layout(e.to_string()))?;
        let mut per_page_regions = Vec::new();
        for (i, page_ir) in pages_ir.iter().enumerate() {
            check_cancelled(cancel)?;
            let page = page_ir.page.0;
            let key = pf.source_sha;
            let cache_key = format!("layout:{}", page);
            let mut regions: Vec<syncpdf_core::ir::Region> =
                match store.get_stage::<Vec<syncpdf_core::ir::Region>>(&key, &cache_key)? {
                    Some(cached) => cached,
                    None => {
                        let info = pf
                            .page_infos
                            .get(page as usize)
                            .copied()
                            .ok_or_else(|| PipelineError::Protocol("页号越界".into()))?;
                        let regions = detect_regions(
                            &mut model,
                            worker,
                            pf.doc,
                            page,
                            &info,
                            &self.layout_opts,
                        )?;
                        store.put_stage(&key, &cache_key, &regions)?;
                        regions
                    }
                };
            let report = apply_coverage_fallback(
                &mut regions,
                page_ir,
                page,
                self.layout_opts.coverage_limit,
            );
            if report.ratio > self.layout_opts.coverage_limit {
                sink.emit(Event::Issue {
                    severity: syncpdf_protocol::Severity::Warning,
                    code: "coverage_gap".into(),
                    paragraph_id: None,
                    page: Some(page + 1),
                    message: format!(
                        "未覆盖字形比例 {:.4} 超过门禁 {:.4}（已追加兜底区域）",
                        report.ratio, self.layout_opts.coverage_limit
                    ),
                });
            }
            sink.emit(Event::Progress {
                stage: Stage::LayoutAnalysis,
                done: i as u32 + 1,
                total: pages_ir.len() as u32,
            });
            per_page_regions.push((page_ir.page, regions, report));
        }
        emit_stage_finished(sink, Stage::LayoutAnalysis, t);

        // ── 3. paragraph_analysis ─────────────────────────────────────
        emit_stage_started(sink, Stage::ParagraphAnalysis);
        let t = Instant::now();
        let mut all_paras: Vec<Paragraph> = Vec::new();
        for (page, regions, _) in &per_page_regions {
            check_cancelled(cancel)?;
            let ir = pages_ir
                .iter()
                .find(|p| p.page == *page)
                .expect("区域来自本页 IR");
            all_paras.extend(stages::analyze_page(ir, regions));
        }
        for p in &all_paras {
            sink.emit(Event::Paragraph {
                paragraph_id: p.id.clone(),
                page: p.id.page,
                status: syncpdf_core::ir::ParagraphStatus::Pending,
                boxes: Some(vec![p.bbox]),
                coord_system: syncpdf_core::CoordSystem::PdfUser,
                translated_html: None,
            });
        }
        emit_stage_finished(sink, Stage::ParagraphAnalysis, t);

        // ── 4. translating（流式；阶段 1 到不了这里）────────────────────
        emit_stage_started(sink, Stage::Translating);
        let t = Instant::now();
        let translatable: Vec<Paragraph> = all_paras
            .iter()
            .filter(|p| matches!(p.translatable, syncpdf_core::ir::Translatable::Yes))
            .cloned()
            .collect();
        let mut schedule = PageSchedule::new(
            translatable
                .iter()
                .map(|p| (p.id.page, p.id.clone())),
        );
        schedule.add_all_pages(selected.len() as u32);

        let (font_store, font_profile) = load_fonts(&self.fonts_dir, fields.target_lang)?;
        let shaper = stages::StoreShaper::new(&font_store, &font_profile);
        let kind = cfg_translator(fields, pf)?;
        let _translator = make_translator(&kind)?;

        // 排版 → 回写 → 页就绪（阶段 2 前不会真正执行到这里）。
        let _ = &shaper;
        let _ = &mut schedule;
        let _ = regions_from_detections;
        emit_stage_finished(sink, Stage::Translating, t);

        // 阶段 2 会继续 typesetting / validating / publishing 并在最后
        // 发 document_finished + run_finished{ok:true}。
        let e = PipelineError::NotYetAvailable("writeback::bind_page");
        let _ = started;
        Err(e)
    }
}

/// 拆 `configure` 里的翻译器；`run` 的 `translator` 字段不在，故只能这样取。
fn cfg_translator(
    _fields: &RunFields<'_>,
    _pf: &Preflight,
) -> Result<syncpdf_protocol::TranslatorKind, PipelineError> {
    Err(PipelineError::Protocol(
        "translator 需从 configure 请求读取（阶段 2 接线）".into(),
    ))
}

/// 用 lopdf 载入 PDF（阶段 2 的 bind_page 需要）。
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

/// `document_finished` 的统计占位（阶段 2 用真实值）。
pub fn stats_placeholder() -> Stats {
    Stats::default()
}

/// 事件类型名（转发，便于 cli 断言）。
pub fn kind_of(e: &Event) -> &'static str {
    event_kind(e)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use syncpdf_core::require_fixture;

    fn vec_sink_path() -> PathBuf {
        std::env::temp_dir().join(format!(
            "syncpdf-pipeline-test-{}-{}.db",
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

    #[test]
    fn run_config_rejects_wrong_request_order() {
        let cancel = Request::Cancel;
        let e = RunConfig::new(cancel.clone(), cancel).unwrap_err();
        assert!(matches!(e, PipelineError::Protocol(_)), "{e:?}");
    }

    #[test]
    fn run_config_with_fake_builds_valid_pair() {
        let cfg =
            RunConfig::with_fake("cjk", "/in.pdf".into(), "/out.pdf".into()).unwrap();
        assert!(cfg.translator_kind().is_some());
        assert!(cfg.cache_dir().is_some());
        assert_eq!(cfg.run_fields().unwrap().target_lang, "zh-CN");
    }

    #[tokio::test]
    async fn run_emits_legal_event_sequence_on_ci_test() {
        let input = require_fixture!("ci-test.pdf");
        let out = vec_sink_path();
        let log: Arc<Mutex<Vec<(u64, Event)>>> = Arc::new(Mutex::new(Vec::new()));
        let shared = SharedSink::new(RunRecorder::new(log.clone()));
        let cfg = RunConfig::with_fake("cjk", input, out.clone()).unwrap();
        let p = pipeline(vec_sink_path());
        let result = p
            .run(&cfg, shared.clone(), CancellationToken::new())
            .await;

        // 阶段 1：source_analysis 未就绪 → 必定是 Err。
        assert!(result.is_err(), "阶段 1 应在 source_analysis 处失败");
        assert!(!out.exists(), "阶段 1 不应写出任何输出文件：{out:?}");

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
            Event::RunFinished { ok, .. } => assert!(!ok, "阶段 1 必须以 ok:false 收尾"),
            other => panic!("期望 run_finished，得到 {other:?}"),
        }
        // seq 单调递增且从 1 起。
        assert_eq!(events[0].0, 1);
        assert!(events.windows(2).all(|w| w[0].0 < w[1].0), "seq 必须单调");
        // 出现过 preflight 阶段与致命 error。
        assert!(
            events.iter().any(|(_, e)| matches!(
                e,
                Event::StageStarted {
                    stage: Stage::Preflight
                }
            )),
            "应有 StageStarted{{Preflight}}"
        );
        assert!(
            events.iter().any(|(_, e)| matches!(
                e,
                Event::StageFinished {
                    stage: Stage::Preflight,
                    ..
                }
            )),
            "preflight 应正常结束"
        );
        assert!(
            events
                .iter()
                .any(|(_, e)| matches!(e, Event::Error { fatal: true, .. })),
            "应有致命 error 事件"
        );
    }

    /// 记录 `(seq, event)` 的汇；`seq` 由自身维护，与 `SharedSink` 一致。
    #[derive(Debug)]
    struct RunRecorder {
        seq: u64,
        log: Arc<Mutex<Vec<(u64, Event)>>>,
    }

    impl RunRecorder {
        fn new(log: Arc<Mutex<Vec<(u64, Event)>>>) -> Self {
            Self { seq: 0, log }
        }
    }

    impl crate::events::EventSink for RunRecorder {
        fn emit(&mut self, event: Event) {
            self.seq += 1;
            self.log.lock().expect("测试锁").push((self.seq, event));
        }
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
}
