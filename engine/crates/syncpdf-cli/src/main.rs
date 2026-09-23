//! SyncPDF 引擎命令行入口（sidecar）。
//!
//! - `run`：读 stdin 的 JSONL 请求（首条 `configure`、随后 `run`），事件打到
//!   stdout（每行一个 `Envelope`）；日志走 stderr。stdin EOF 或 `cancel`
//!   请求都会立刻取消当前任务。
//! - `translate`：便捷入口，内部构造同样的 configure+run 请求。
//! - `inspect`：调试用，打印 preflight 信息与每页几何（阶段 1）。
//! - `version`：打印版本。
//!
//! 约定：**stdout 只出 JSONL 事件**，任何人类可读信息都走 stderr。

#![forbid(unsafe_code)]

use std::io::BufRead;
use std::path::PathBuf;

use clap::{Parser, Subcommand};
use syncpdf_pipeline::cancel::CancellationToken;
use syncpdf_pipeline::events::{SharedSink, StdoutSink};
use syncpdf_pipeline::run::{Pipeline, RunConfig, RunSummary};
use syncpdf_pipeline::stages::{preflight, PipelineError};
use syncpdf_protocol::{
    decode_request, Event, Mode, Request, TranslateProvider, TranslatorKind, PROTOCOL_VERSION,
};

#[derive(Parser, Debug)]
#[command(name = "syncpdf-cli", version, about = "SyncPDF engine")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// 读 stdin JSONL 请求（configure → run）并输出事件 JSONL。
    Run {
        /// 前端（Electron）约定的协议版本；只支持 1，其它值报致命错误并退出码 2。
        #[arg(long, default_value_t = 1)]
        protocol: u32,
    },
    /// 便捷入口：翻译一份 PDF。
    Translate {
        /// 输入 PDF。
        #[arg(long)]
        input: PathBuf,
        /// 输出 PDF。
        #[arg(long)]
        output: PathBuf,
        /// 翻译器：`fake:echo` / `fake:cjk` / `fake:slow:500` / `pi`。
        #[arg(long, default_value = "fake:echo")]
        translator: String,
        /// 模型名（`pi` 通道用）。
        #[arg(long)]
        model: Option<String>,
        /// thinking 档位（`pi` 通道用）。
        #[arg(long)]
        thinking: Option<String>,
        /// 目标语言。
        #[arg(long, default_value = "zh-CN")]
        target_lang: String,
        /// 源语言（默认 auto）。
        #[arg(long, default_value = "auto")]
        source_lang: String,
        /// 页子集，如 `1-3` 或 `1,3,5`（1 基）。
        #[arg(long)]
        pages: Option<String>,
        /// 缓存目录。
        #[arg(long)]
        cache_dir: Option<PathBuf>,
        /// 只重新编译已通过校验的缓存译文；不调用模型，缺失块保留原文。
        #[arg(long)]
        cache_only: bool,
        /// 译文字号相对源字号的倍数；保留样式层级，不自动缩字。
        #[arg(long, default_value_t = 1.0)]
        font_scale: f32,
        /// 行距/译文段落字号的倍数，如 1.3（非 pt）；缺省保留源比例。
        #[arg(long)]
        line_height: Option<f32>,
    },
    /// 打印 preflight 信息、每页几何与段落摘要（调试用）。
    Inspect {
        /// 输入 PDF。
        #[arg(long)]
        input: PathBuf,
        /// 只看某一页（1 基）；缺省打印全部页。
        #[arg(long)]
        page: Option<u32>,
        /// 布局模型路径；缺省用 `engine/vendor/models/pp_doc_layoutv3.onnx`。
        /// 模型缺失时跳过 layout，用「整页一个区域」兜底。
        #[arg(long)]
        model: Option<PathBuf>,
        /// 只打印 id/kind/bbox/前 40 字符/translatable。
        #[arg(long)]
        paragraphs: bool,
    },
    /// 打印版本与构建信息。
    Version,
}

fn main() -> anyhow::Result<()> {
    init_tracing();
    let cli = Cli::parse();
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    match cli.command {
        Command::Version => {
            println!("syncpdf-cli {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        Command::Run { protocol } => {
            // 前端启动命令是 `syncpdf-cli run --protocol 1`。协议版本不对时只能
            // 说「这条连接用不了」：发一条致命 error 事件（前端按 JSONL 解析），
            // 然后以退出码 2 结束，不做任何后续工作。
            if protocol != 1 {
                SharedSink::new(StdoutSink::new()).emit(Event::Error {
                    fatal: true,
                    code: "protocol_unsupported".to_string(),
                    message: format!("不支持的协议版本 {protocol}（本引擎只支持 --protocol 1）"),
                });
                eprintln!("syncpdf-cli: 不支持的协议版本 {protocol}（只支持 1）");
                std::process::exit(2);
            }
            rt.block_on(cmd_run())
        }
        Command::Translate {
            input,
            output,
            translator,
            model,
            thinking,
            target_lang,
            source_lang,
            pages,
            cache_dir,
            cache_only,
            font_scale,
            line_height,
        } => rt.block_on(cmd_translate(
            input,
            output,
            translator,
            model,
            thinking,
            target_lang,
            source_lang,
            pages,
            cache_dir,
            cache_only,
            font_scale,
            line_height,
        )),
        Command::Inspect {
            input,
            page,
            model,
            paragraphs,
        } => cmd_inspect(input, page, model, paragraphs),
    }
}

/// tracing 到 stderr（`RUST_LOG` 控制级别）；stdout 留给事件 JSONL。
fn init_tracing() {
    use tracing_subscriber::EnvFilter;
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .with_target(false)
        .try_init();
}

/// `run`：stdin JSONL → configure/run → Pipeline；EOF 或 cancel 都触发取消。
async fn cmd_run() -> anyhow::Result<()> {
    let sink = SharedSink::new(StdoutSink::new());
    let cancel = CancellationToken::new();

    // stdin 的所有权交给读线程（`Stdin` 是 Send），读到的行经 channel 送回。
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    let cancel_reader = cancel.clone();
    std::thread::spawn(move || {
        let stdin = std::io::stdin();
        for line in stdin.lock().lines() {
            let Ok(line) = line else { break };
            if line.trim().is_empty() {
                continue;
            }
            if tx.send(line).is_err() {
                // 主流程已结束。
                return;
            }
        }
        // EOF：主进程走了或输入结束，取消任务。
        cancel_reader.cancel();
    });

    let mut configure: Option<Request> = None;
    let (configure, run_req) = loop {
        let Ok(line) = rx.recv() else {
            // 读线程结束（EOF）且还没收到 run：无事可做。
            return Ok(());
        };
        let req = match decode_request(&line) {
            Ok(r) => r,
            Err(e) => {
                emit_error(&sink, "protocol", &format!("请求解析失败：{e}"), false);
                continue;
            }
        };
        match req {
            req @ Request::Configure { .. } => configure = Some(req),
            req @ Request::Run { .. } => {
                let Some(cfg) = configure.take() else {
                    emit_error(&sink, "protocol", "run 之前必须先给 configure", true);
                    continue;
                };
                break (cfg, req);
            }
            Request::Cancel => {
                cancel.cancel();
                return Ok(());
            }
            other => emit_error(
                &sink,
                "unsupported_request",
                &format!("阶段 1 不支持的请求：{}", tag_of(&other)),
                false,
            ),
        }
    };

    let cfg = RunConfig::new(configure, run_req)?;
    let pipeline = Pipeline::default();
    let result = pipeline.run(&cfg, sink.clone(), cancel.clone()).await;
    if cancel.is_cancelled() {
        eprintln!("syncpdf-cli: 任务已取消");
    }
    match result {
        Ok(s) => {
            print_summary(&s);
            anyhow::ensure!(s.ok, "已保存部分结果，但翻译未完整完成；详见 issue 事件");
            Ok(())
        }
        Err(e) => {
            // `run` 内部已经发过 error/run_finished，这里只在 stderr 留痕。
            Err(e.into())
        }
    }
}

/// `translate`：用同样的事件通道跑一遍（内部构造 configure+run）。
#[allow(clippy::too_many_arguments)]
async fn cmd_translate(
    input: PathBuf,
    output: PathBuf,
    translator: String,
    model: Option<String>,
    thinking: Option<String>,
    target_lang: String,
    source_lang: String,
    pages: Option<String>,
    cache_dir: Option<PathBuf>,
    cache_only: bool,
    font_scale: f32,
    line_height: Option<f32>,
) -> anyhow::Result<()> {
    let typography = syncpdf_pipeline::stages::typeset::Typography::new(font_scale, line_height)?;
    let pages = match pages.as_deref() {
        Some(spec) => Some(parse_pages(spec)?),
        None => None,
    };
    let kind = translator_kind(&translator, model, thinking)?;
    let cache_dir = cache_dir.unwrap_or_else(|| std::env::temp_dir().join("syncpdf-cache"));
    let configure = Request::Configure {
        provider: match kind {
            TranslatorKind::Pi { .. } => TranslateProvider::Pi,
            _ => TranslateProvider::Http,
        },
        base_url: None,
        model: String::new(),
        api_key: None,
        concurrency: 1,
        cache_dir,
        translator: kind,
    };
    let run = Request::Run {
        doc_id: "cli".into(),
        input,
        output,
        source_lang,
        target_lang,
        pages,
        font_profile: None,
        terminology: None,
        mode: Mode::Full,
    };
    let mut cfg = RunConfig::new(configure, run)?;
    cfg.cache_only = cache_only;
    cfg.typography = typography;
    let sink = SharedSink::new(StdoutSink::new());
    let pipeline = Pipeline::default();
    match pipeline.run(&cfg, sink, CancellationToken::new()).await {
        Ok(s) => {
            print_summary(&s);
            anyhow::ensure!(s.ok, "已保存部分结果，但翻译未完整完成；详见 issue 事件");
            Ok(())
        }
        Err(e) => Err(e.into()),
    }
}

/// `inspect`：preflight → 每页 bind → （模型可用时）layout → 段落摘要。
///
/// 输出走 **stdout**（这是调试工具，不是 sidecar 的 JSONL 通道）。模型缺失时
/// 跳过 layout，用「整页一个区域」兜底，保证段落摘要仍然可看。
fn cmd_inspect(
    input: PathBuf,
    page: Option<u32>,
    model: Option<PathBuf>,
    only_paragraphs: bool,
) -> anyhow::Result<()> {
    use syncpdf_core::ir::{PageIR, Region};
    use syncpdf_pipeline::stages::{analyze_page, apply_coverage_fallback, LayoutOpts};

    let worker = syncpdf_pdf::pdfium::PdfiumWorker::spawn()
        .map_err(|e| anyhow::anyhow!("pdfium 不可用：{e}"))?;
    let pf = preflight(&worker, &input).map_err(|e: PipelineError| anyhow::anyhow!("{e}"))?;
    if !only_paragraphs {
        println!(
            "{}: {} 页, 加密={}, 签名={}, sha256={}",
            input.display(),
            pf.pages,
            pf.encrypted,
            pf.signed,
            pf.source_sha.to_hex()
        );
    }
    let lo = lopdf::Document::load(&input)
        .map_err(|e| anyhow::anyhow!("lopdf 载入 {} 失败：{e}", input.display()))?;

    // 模型路径：显式给了就用，否则探 `engine/vendor/models`。
    let model_path = model.or_else(|| {
        let dir = syncpdf_core::fixtures::models_dir()?;
        syncpdf_pipeline::stages::layout_model::resolve(&dir)
            .ok()
            .map(|asset| asset.path)
    });
    let mut layout_model = match &model_path {
        Some(p) => match syncpdf_layout::LayoutModel::load(p, 2) {
            Ok(m) => Some(m),
            Err(e) => {
                eprintln!("syncpdf-cli: 布局模型载入失败（改用整页兜底区域）：{e}");
                None
            }
        },
        None => {
            eprintln!("syncpdf-cli: 未找到布局模型（改用整页兜底区域）");
            None
        }
    };

    let one_based = page.unwrap_or(0);
    if one_based != 0 && one_based > pf.pages {
        anyhow::bail!("页号 {one_based} 越界（共 {} 页）", pf.pages);
    }
    let opts = LayoutOpts::default();
    for (i, info) in pf.page_infos.iter().enumerate() {
        let n = i as u32 + 1;
        if one_based != 0 && n != one_based {
            continue;
        }
        let bound = syncpdf_pdf::bind::bind_page(&worker, pf.doc, &lo, n)
            .map_err(|e| anyhow::anyhow!("第 {n} 页 bind_page 失败：{e}"))?;
        let ir: PageIR = bound.ir.clone();
        if !only_paragraphs {
            println!(
                "  第 {n} 页: {:.1}×{:.1} pt, rotate={}, media={:?}, crop={:?}",
                info.width, info.height, info.rotation, info.media_box, info.crop_box
            );
            println!(
                "    字形={} 文本对象={} 匹配={} 降级={} 长度={:?}",
                ir.glyphs().count(),
                bound.stats.text_objects,
                bound.stats.matched,
                bound.stats.degraded,
                ir.media_box
            );
            for issue in &bound.issues {
                println!("    [bind issue] {issue}");
            }
        }

        // 区域：模型可用则检测 + 覆盖门禁，否则整页一个区域。
        let mut regions: Vec<Region> = match &mut layout_model {
            Some(m) => match syncpdf_pipeline::stages::detect_regions(
                m, &worker, pf.doc, i as u32, info, &opts,
            ) {
                Ok(r) => r,
                Err(e) => {
                    eprintln!("syncpdf-cli: 第 {n} 页 layout 失败（改用整页兜底区域）：{e}");
                    vec![full_page_region(i as u32, info)]
                }
            },
            None => vec![full_page_region(i as u32, info)],
        };
        let report = apply_coverage_fallback(&mut regions, &ir, i as u32, opts.coverage_limit);
        if !only_paragraphs {
            println!("    区域={} 未覆盖比例={:.4}", regions.len(), report.ratio);
        }

        let paras = analyze_page(&ir, &regions);
        println!("    段落={}", paras.len());
        for p in &paras {
            let head: String = p.text.chars().take(40).collect();
            let kind = format!("{:?}", p.kind);
            println!(
                "      [{}] 第{}页#{} kind={} bbox=({:.1},{:.1})-({:.1},{:.1}) \
                 align={:?} 缩进={:.1} 行高={:.1} 可译={:?} text={:?}",
                p.id,
                p.id.page,
                p.region,
                kind,
                p.bbox.x0,
                p.bbox.y0,
                p.bbox.x1,
                p.bbox.y1,
                p.align,
                p.first_indent,
                p.line_height,
                p.translatable,
                head
            );
        }
    }
    worker.close(pf.doc);
    Ok(())
}

/// 整页兜底区域：模型缺失 / 检测失败时用，保证段落分析仍有区域可切。
fn full_page_region(page: u32, info: &syncpdf_pdf::pdfium::PageInfo) -> syncpdf_core::ir::Region {
    use syncpdf_core::ir::{Region, RegionKind};
    use syncpdf_core::{PageId, Rect};
    Region {
        page: PageId(page),
        index: 0,
        kind: RegionKind::Text,
        bbox: Rect {
            x0: 0.0,
            y0: 0.0,
            x1: info.width,
            y1: info.height,
        },
        score: 0.0,
        order: 0,
    }
}

/// `--pages` 解析：`1-3` / `1,3,5` / `1-3,7`（1 基，输出 0 基）。
fn parse_pages(spec: &str) -> anyhow::Result<Vec<u32>> {
    let mut out: Vec<u32> = Vec::new();
    for part in spec.split(',') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        match part.split_once('-') {
            Some((a, b)) => {
                let a: u32 = a
                    .trim()
                    .parse()
                    .map_err(|_| anyhow::anyhow!("页号区间起点非法：{a:?}"))?;
                let b: u32 = b
                    .trim()
                    .parse()
                    .map_err(|_| anyhow::anyhow!("页号区间终点非法：{b:?}"))?;
                if a == 0 || b < a {
                    anyhow::bail!("页号区间非法：{part}");
                }
                out.extend((a - 1)..b);
            }
            None => {
                let n: u32 = part
                    .parse()
                    .map_err(|_| anyhow::anyhow!("页号非法：{part:?}"))?;
                if n == 0 {
                    anyhow::bail!("页号必须从 1 开始：{part}");
                }
                out.push(n - 1);
            }
        }
    }
    out.sort_unstable();
    out.dedup();
    Ok(out)
}

/// `--translator` 解析：`pi` / `fake:<name>`。
fn translator_kind(
    spec: &str,
    model: Option<String>,
    thinking: Option<String>,
) -> anyhow::Result<TranslatorKind> {
    match spec {
        "pi" => Ok(TranslatorKind::Pi {
            program: PathBuf::from("pi"),
            model: model.unwrap_or_else(|| "deepseek/deepseek-flash".into()),
            thinking: thinking.unwrap_or_else(|| "low".into()),
        }),
        other => {
            let name = other.strip_prefix("fake:").unwrap_or(other);
            if syncpdf_pipeline::stages::fake_from_name(name).is_none() {
                anyhow::bail!(
                    "未知翻译器 {other:?}；可用：pi, fake:echo, fake:cjk, fake:stretch:1.4, \
                     fake:shrink:0.6, fake:fail-every:3, fake:slow:500"
                );
            }
            Ok(TranslatorKind::Fake {
                name: name.to_string(),
            })
        }
    }
}

/// 失败时给 stdout 补一个 error 事件（非致命，任务继续读 stdin）。
fn emit_error(sink: &SharedSink, code: &str, message: &str, fatal: bool) {
    sink.emit(Event::Error {
        fatal,
        code: code.to_string(),
        message: message.to_string(),
    });
}

/// 请求的 `type` 标签（日志用）。
fn tag_of(req: &Request) -> &'static str {
    match req {
        Request::Configure { .. } => "configure",
        Request::Run { .. } => "run",
        Request::Retranslate { .. } => "retranslate",
        Request::ApplyEdit { .. } => "apply_edit",
        Request::Export { .. } => "export",
        Request::Cancel => "cancel",
    }
}

/// 汇总打到 stderr（stdout 只走 JSONL）。
fn print_summary(s: &RunSummary) {
    eprintln!(
        "syncpdf-cli: 完成 ok={} 页={} 段={} 耗时={}ms 字体={} 膨胀比={:.3} 回退={}",
        s.ok,
        s.pages,
        s.paragraphs,
        s.elapsed_ms,
        s.stats.fonts,
        s.stats.expansion_ratio,
        s.stats.fallbacks
    );
}

/// 供测试断言用的协议版本导出。
pub const CLI_PROTOCOL_VERSION: u32 = PROTOCOL_VERSION;
