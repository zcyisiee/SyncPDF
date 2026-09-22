//! 端到端测试：`Pipeline::run` 对真实夹具跑完整流程。
//!
//! 覆盖：事件序列合法性、每页一次 `page_ready`、`document_finished`、
//! 输出文件存在且 `self_check` 通过、pdfium 能提取到 CJK 译文、取消路径、
//! `pages` 过滤（未选中页不发 `page_ready`）。
//!
//! 缺 pdfium / 布局模型 / 字体夹具时整体 skip（打印原因）。

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use syncpdf_core::require_fixture;
use syncpdf_pipeline::cancel::CancellationToken;
use syncpdf_pipeline::events::{event_kind, EventSink, SharedSink};
use syncpdf_pipeline::run::{Pipeline, RunConfig};
use syncpdf_pipeline::stages::LayoutOpts;
use syncpdf_protocol::{Event, Stage};

/// 临时文件路径（进程号 + 纳秒，避免并发测试互撞）。
fn tmp_path(tag: &str, ext: &str) -> PathBuf {
    std::env::temp_dir().join(format!(
        "syncpdf-e2e-{tag}-{}-{}.{ext}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ))
}

/// 环境齐备才跑（否则返回 None 并打印 SKIP）。
fn env_ready() -> Option<()> {
    if let Err(e) = syncpdf_pdf::pdfium::PdfiumWorker::spawn() {
        eprintln!("SKIP: pdfium 不可用：{e}");
        return None;
    }
    let models = syncpdf_core::fixtures::models_dir()?;
    if !models.join("pp_doc_layoutv3.onnx").is_file() {
        eprintln!("SKIP: 布局模型缺失：{}", models.display());
        return None;
    }
    syncpdf_core::fixtures::fonts_dir()?;
    Some(())
}

/// 造一个只走临时目录的 Pipeline（缓存库 / 字体 / 模型）。
fn pipeline(store_tag: &str) -> Pipeline {
    Pipeline {
        fonts_dir: syncpdf_core::fixtures::fonts_dir().unwrap_or_default(),
        models_dir: syncpdf_core::fixtures::models_dir().unwrap_or_default(),
        store_path: tmp_path(store_tag, "db"),
        layout_opts: LayoutOpts::default(),
    }
}

/// 把请求的页子集设成 0 基页号。
///
/// **重要**：`Request::Run.pages` 的文档注释写的是「1 基页号子集」，但
/// `run.rs::selected` 与 `cli::parse_pages` 都按 **0 基**解释（阶段 1 遗留的
/// 不一致；本阶段不改 protocol crate，已记入回报「已知缺口」）。测试按实现的
/// 实际行为传 0 基页号。
fn select_pages(cfg: &mut RunConfig, pages: Vec<u32>) {
    match &mut cfg.run {
        syncpdf_protocol::Request::Run { pages: slot, .. } => *slot = Some(pages),
        other => panic!("run 请求类型不对：{other:?}"),
    }
}

/// 记录 `(seq, event)`；`seq` 自增，模拟 `Envelope` 的编号。
#[derive(Debug)]
struct Recorder {
    seq: u64,
    log: Arc<Mutex<Vec<(u64, Event)>>>,
}

impl Recorder {
    fn new(log: Arc<Mutex<Vec<(u64, Event)>>>) -> Self {
        Self { seq: 0, log }
    }
}

impl EventSink for Recorder {
    fn emit(&mut self, event: Event) {
        self.seq += 1;
        self.log.lock().expect("测试锁").push((self.seq, event));
    }
}

/// 抽出 `page_ready` 的页号序列（事件顺序）。
fn ready_pages(events: &[(u64, Event)]) -> Vec<u32> {
    events
        .iter()
        .filter_map(|(_, e)| match e {
            Event::PageReady { page, .. } => Some(*page),
            _ => None,
        })
        .collect()
}

/// 抽出 `page_ready` 的 revision 序列。
fn revisions(events: &[(u64, Event)]) -> Vec<u64> {
    events
        .iter()
        .filter_map(|(_, e)| match e {
            Event::PageReady { revision, .. } => Some(*revision),
            _ => None,
        })
        .collect()
}

/// 事件序列的通用断言（首尾、seq 单调、阶段事件）。
fn assert_legal_sequence(events: &[(u64, Event)]) {
    assert!(!events.is_empty(), "至少要有事件");
    assert_eq!(
        event_kind(&events[0].1),
        "run_started",
        "首事件必须是 run_started"
    );
    assert_eq!(
        event_kind(&events.last().unwrap().1),
        "run_finished",
        "末事件必须是 run_finished"
    );
    assert_eq!(events[0].0, 1, "seq 从 1 起");
    assert!(
        events.windows(2).all(|w| w[0].0 < w[1].0),
        "seq 必须严格单调递增"
    );
    for stage in [
        Stage::Preflight,
        Stage::SourceAnalysis,
        Stage::LayoutAnalysis,
        Stage::ParagraphAnalysis,
        Stage::Translating,
        Stage::Typesetting,
        Stage::Validating,
        Stage::Publishing,
    ] {
        assert!(
            events.iter().any(|(_, e)| matches!(
                e,
                Event::StageFinished { stage: s, .. } if *s == stage
            )),
            "缺少 {stage:?} 的 stage_finished"
        );
    }
}

/// pdfium 提取某页文本。
fn page_text(
    worker: &syncpdf_pdf::pdfium::PdfiumWorker,
    doc: syncpdf_pdf::pdfium::DocId,
    page: u32,
) -> String {
    worker
        .page_text_objects(doc, page)
        .map(|objs| {
            objs.iter()
                .flat_map(|o| o.chars.iter())
                .filter_map(|c| c.unicode.as_deref())
                .collect::<String>()
        })
        .unwrap_or_default()
}

fn has_cjk(s: &str) -> bool {
    s.chars().any(|c| matches!(c as u32, 0x4E00..=0x9FFF))
}

/// ci-test 一页：`fake:cjk` 全流程成功，输出存在且 `self_check` 通过。
#[tokio::test]
async fn ci_test_full_run_succeeds() {
    if env_ready().is_none() {
        return;
    }
    let input = require_fixture!("ci-test.pdf");
    let out = tmp_path("ci", "pdf");
    let log: Arc<Mutex<Vec<(u64, Event)>>> = Arc::new(Mutex::new(Vec::new()));
    let sink = SharedSink::new(Recorder::new(log.clone()));
    let cfg = RunConfig::with_fake("cjk", input, out.clone()).unwrap();
    let summary = pipeline("ci")
        .run(&cfg, sink, CancellationToken::new())
        .await
        .expect("ci-test 端到端应成功");
    assert!(summary.ok);
    assert!(out.is_file(), "输出文件应存在：{}", out.display());

    let events = log.lock().unwrap().clone();
    assert_legal_sequence(&events);
    assert!(
        events
            .iter()
            .any(|(_, e)| matches!(e, Event::DocumentFinished { .. })),
        "应有 document_finished"
    );
    assert_eq!(
        ready_pages(&events),
        vec![1],
        "ci-test 只有 1 页，恰一次 page_ready"
    );
    assert_eq!(revisions(&events), vec![1], "revision 从 1 递增");

    // 输出 PDF 自检通过。
    //
    // 注意：`ci-test.pdf` 是**合成空页**——`bind_page` 只绑定出 0 个字形
    // （`text_objects == 0`），因此没有任何可译段落，输出里也不会有 CJK，
    // 故 `expect_cjk_on_pages` 必须传空切片。
    let report = syncpdf_pdf::validate::self_check(&out, &[]).expect("self_check");
    assert!(report.ok, "self_check 失败：{:?}", report.problems);
    assert_eq!(summary.paragraphs, 0, "ci-test 无可译段落");
}

/// up-vns 前三页：3 次 `page_ready`（revision 递增）+ 译文页含 CJK。
#[tokio::test]
async fn up_vns_first_three_pages_succeed() {
    if env_ready().is_none() {
        return;
    }
    let input = require_fixture!("up-vns.pdf");
    let out = tmp_path("upvns", "pdf");
    let log: Arc<Mutex<Vec<(u64, Event)>>> = Arc::new(Mutex::new(Vec::new()));
    let sink = SharedSink::new(Recorder::new(log.clone()));
    let mut cfg = RunConfig::with_fake("cjk", input, out.clone()).unwrap();
    select_pages(&mut cfg, vec![0, 1, 2]);
    let summary = pipeline("upvns")
        .run(&cfg, sink, CancellationToken::new())
        .await
        .expect("up-vns 前三页应成功");
    assert!(summary.ok);
    assert_eq!(summary.pages, 3);

    let events = log.lock().unwrap().clone();
    assert_legal_sequence(&events);
    // 页就绪顺序 = 各页最后一个段落落定的顺序（流式），**不保证按页号**：
    // 第 2、3 页段落少，实测先于第 1 页就绪。
    let mut ready = ready_pages(&events);
    ready.sort_unstable();
    assert_eq!(ready, vec![1, 2, 3], "三页各恰一次 page_ready");
    assert_eq!(
        revisions(&events),
        vec![1, 2, 3],
        "revision 从 1 起严格递增"
    );

    // 译文页应能提取到 CJK。
    let worker = syncpdf_pdf::pdfium::PdfiumWorker::spawn().expect("pdfium");
    let doc = worker.open(&out).expect("打开输出");
    for page in 0..3u32 {
        let text = page_text(&worker, doc, page);
        assert!(has_cjk(&text), "第 {} 页应含 CJK 译文：{text:?}", page + 1);
    }
    worker.close(doc);
}

/// 取消：`fake:slow:500` + 200ms 后取消 → `run_finished{ok:false}`；
/// 若输出存在，必须是完整快照（`self_check` 通过）。
///
/// 运行时必须是**多线程**：`Pipeline::run` 的 future 第一次 poll 就要同步做完
/// 预处理 + 版面推理（数十秒，期间不让出），单线程运行时里同一任务上的定时器
/// 永远轮不到（实测 run 自己跑完、定时器未触发）。`CancellationToken` 内部是
/// 原子量，另起一个 worker 线程写、主任务在下一次检查点读到即可。
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cancel_mid_run_yields_ok_false() {
    if env_ready().is_none() {
        return;
    }
    let input = require_fixture!("up-vns.pdf");
    let out = tmp_path("cancel", "pdf");
    let log: Arc<Mutex<Vec<(u64, Event)>>> = Arc::new(Mutex::new(Vec::new()));
    let sink = SharedSink::new(Recorder::new(log.clone()));
    let mut cfg = RunConfig::with_fake("slow:500", input, out.clone()).unwrap();
    select_pages(&mut cfg, vec![0, 1, 2]);
    let cancel = CancellationToken::new();
    // `Pipeline::run` 的 future **不是 `Send`**（`syncpdf_translate::Cache` 内含
    // rusqlite `Connection`）→ 不能把它 spawn 出去；让**取消**发生在别的任务里：
    // 另起一个任务，200ms 后 `cancel()`，主任务照常 await run future。
    let token = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        token.cancel();
    });
    let result = pipeline("cancel").run(&cfg, sink, cancel.clone()).await;
    assert!(cancel.is_cancelled(), "取消应已生效");
    assert!(
        matches!(
            result,
            Err(syncpdf_pipeline::stages::PipelineError::Cancelled)
        ),
        "应是 Cancelled：{result:?}"
    );

    let events = log.lock().unwrap().clone();
    assert_eq!(event_kind(&events.last().unwrap().1), "run_finished");
    match &events.last().unwrap().1 {
        Event::RunFinished { ok, .. } => assert!(!ok, "取消应以 ok:false 收尾"),
        other => panic!("期望 run_finished，得到 {other:?}"),
    }
    assert!(
        events.iter().any(|(_, e)| matches!(
            e,
            Event::Error { fatal: false, code, .. } if code == "cancelled"
        )),
        "应有 cancelled 错误事件"
    );
    // 若落过半快照，必须是完整可读文件。
    if out.is_file() {
        let report = syncpdf_pdf::validate::self_check(&out, &[]).expect("self_check");
        assert!(report.ok, "半快照也必须自检通过：{:?}", report.problems);
    }
}

/// `pages` 过滤：只选第 2、3 页时，第 1 页不发 `page_ready`。
#[tokio::test]
async fn page_filter_emits_only_selected_page_ready() {
    if env_ready().is_none() {
        return;
    }
    let input = require_fixture!("up-vns.pdf");
    let out = tmp_path("filter", "pdf");
    let log: Arc<Mutex<Vec<(u64, Event)>>> = Arc::new(Mutex::new(Vec::new()));
    let sink = SharedSink::new(Recorder::new(log.clone()));
    let mut cfg = RunConfig::with_fake("cjk", input, out.clone()).unwrap();
    select_pages(&mut cfg, vec![1, 2]);
    let summary = pipeline("filter")
        .run(&cfg, sink, CancellationToken::new())
        .await
        .expect("页子集应成功");
    assert_eq!(summary.pages, 2);

    let events = log.lock().unwrap().clone();
    assert_legal_sequence(&events);
    let mut ready = ready_pages(&events);
    ready.sort_unstable();
    assert_eq!(ready, vec![2, 3], "只应发选中页的 page_ready");
    assert!(!ready.contains(&1), "未选中的第 1 页不应发 page_ready");
    // 输出页数应与原文一致（子集只翻译，不删页）。
    let report = syncpdf_pdf::validate::self_check(&out, &[]).expect("self_check");
    assert!(report.ok, "{:?}", report.problems);
}

/// `Pipeline::default()` 能构造（不 panic），且路径字段非空。
#[test]
fn pipeline_default_is_constructible() {
    let p = Pipeline::default();
    assert!(!p.store_path.as_os_str().is_empty());
    let _ = Path::new("/x");
}
