//! pdfium 访问层：单线程队列封装 + 字形几何提取。
//!
//! 设计基准：docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md。
//!
//! pdfium 本身不是线程安全的，因此所有 FFI 调用都必须发生在同一条专用线程上。
//! [`PdfiumWorker`] 就是这条线程的句柄：外部把闭包通过 crossbeam-channel 发过去，
//! 同步等待结果；pdfium 的对象（`Pdfium` / `PdfDocument` / `PdfPage` …）永远不跨线程。
//! 文档只以 [`DocId`] 形式暴露给调用方，真正的 `PdfDocument` 保存在 worker 线程内部表里。
//!
//! 坐标一律是 PDF 用户空间（左下原点，y 向上，单位 pt）。

use std::collections::HashMap;
use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use crossbeam_channel::{bounded, unbounded, Sender};
use pdfium_render::prelude::{
    PdfDocument, PdfPageObject, PdfPageObjectCommon, PdfPageObjectsCommon, PdfPageText,
    PdfPageTextObject, PdfPageTextRenderMode, PdfRect, PdfRenderConfig, PdfSecurityHandlerRevision,
    Pdfium, PdfiumError as RenderError, Pixels,
};
use serde::{Deserialize, Serialize};
use syncpdf_core::{Color, Point, Rect};

/// 递归进入 Form XObject 的最大深度，防御病态嵌套。
const MAX_FORM_DEPTH: u32 = 16;

/// pdfium 未能识别文本渲染模式时使用的哨兵值（PDF `Tr` 合法值只有 0..=7）。
pub const RENDER_MODE_UNKNOWN: u8 = 255;

// ---------------------------------------------------------------------------
// 错误
// ---------------------------------------------------------------------------

/// pdfium 封装层错误。注意与 `pdfium_render::prelude::PdfiumError` 同名但不是同一类型，
/// 后者在本模块里别名为 `RenderError`。
#[derive(Debug, thiserror::Error)]
pub enum PdfiumError {
    /// 找不到 pdfium 动态库目录。
    #[error(
        "pdfium 动态库目录未找到：设置 PDFIUM_DYNAMIC_LIB_PATH 或准备 engine/vendor/pdfium/lib"
    )]
    LibraryNotFound,
    /// worker 线程初始化失败（加载动态库、建线程等）。
    #[error("pdfium worker 初始化失败：{0}")]
    Init(String),
    /// worker 线程已经退出，队列不可用。
    #[error("pdfium worker 线程已退出")]
    WorkerGone,
    /// `DocId` 不在 worker 的文档表里（未打开或已关闭）。
    #[error("文档句柄无效：{0:?}")]
    UnknownDocument(DocId),
    /// 页号越界。
    #[error("页号越界：{page}（共 {count} 页）")]
    PageOutOfBounds {
        /// 请求的 0 基页号。
        page: u32,
        /// 文档总页数。
        count: u32,
    },
    /// 打开文件失败。
    #[error("打开 PDF 失败 {path}：{source}")]
    Open {
        /// 出错的文件路径。
        path: PathBuf,
        /// pdfium 侧原始错误。
        #[source]
        source: RenderError,
    },
    /// 参数非法。
    #[error("参数非法：{0}")]
    InvalidArgument(String),
    /// 其余 pdfium 调用失败。
    #[error("pdfium 调用失败：{0}")]
    Pdfium(#[from] RenderError),
}

/// 本模块统一的 `Result`。
pub type Result<T, E = PdfiumError> = std::result::Result<T, E>;

// ---------------------------------------------------------------------------
// 公开数据结构
// ---------------------------------------------------------------------------

/// worker 内部文档表的句柄。只能由 [`PdfiumWorker::open`] 产生，关闭后立即失效。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct DocId(u32);

impl DocId {
    /// 原始数值，仅用于日志与跨进程协议。
    pub fn as_u32(self) -> u32 {
        self.0
    }
}

impl fmt::Display for DocId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "doc#{}", self.0)
    }
}

/// 页面几何信息，单位 pt，PDF 用户空间。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct PageInfo {
    /// pdfium 报告的页宽（已考虑 `/Rotate`）。
    pub width: f32,
    /// pdfium 报告的页高（已考虑 `/Rotate`）。
    pub height: f32,
    /// MediaBox。
    pub media_box: Rect,
    /// CropBox；缺失时回退为 MediaBox。
    pub crop_box: Rect,
    /// `/Rotate`，顺时针度数，取值 0/90/180/270。
    pub rotation: i32,
}

/// 渲染结果：RGBA8 像素，左上原点，行优先，`data.len() == width * height * 4`。
#[derive(Clone, PartialEq, Eq)]
pub struct RgbaBitmap {
    /// 像素宽。
    pub width: u32,
    /// 像素高。
    pub height: u32,
    /// RGBA8 字节。
    pub data: Vec<u8>,
}

impl fmt::Debug for RgbaBitmap {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RgbaBitmap")
            .field("width", &self.width)
            .field("height", &self.height)
            .field("data_len", &self.data.len())
            .finish()
    }
}

/// 字体标志位（取自 pdfium 对 FontDescriptor `/Flags` 与字重的解读）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct FontFlags {
    /// 衬线。
    pub serif: bool,
    /// 等宽。
    pub fixed: bool,
    /// 斜体。
    pub italic: bool,
    /// 粗体（字重 >= 600 视为粗体）。
    pub bold: bool,
}

/// 单个字符的几何，坐标为 PDF 用户空间。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TextChar {
    /// 字符的 Unicode 文本；pdfium 无法映射时为 `None`。
    pub unicode: Option<String>,
    /// 字形外接框。优先取 pdfium 的 loose char box（含字体上下沿），
    /// 失败时回退到 tight char box（仅墨迹）。
    pub bbox: Rect,
    /// 字符原点（基线起点）。
    pub origin: Point,
    /// 推进宽度的近似值，等于 `bbox.width()`。
    pub width: f32,
    /// 字符旋转角，度；水平文本为 0。
    pub angle: f32,
    /// 是否为 pdfium 补出来的字符（空格、换行等），原始内容流里并不存在。
    pub is_generated: bool,
}

/// 一个文本绘制对象（内容流里的一段 `BT … ET` 文本），按内容流顺序编号。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TextObject {
    /// 页内文本对象序号，从 0 开始，按内容流顺序（含嵌套 Form XObject 内的）。
    pub index: u32,
    /// 嵌套 Form XObject 的对象序号路径；空表示页级对象。
    pub form_path: Vec<u32>,
    /// pdfium 报告的字体名（通常是 BaseFont，可能带子集前缀）。
    pub font_name: String,
    /// 字号，已乘上文本对象矩阵的缩放（即页面上的视觉字号）。
    pub font_size: f32,
    /// `Tf` 原始字号（不含 Tm/CTM 缩放）。`font_size` 是视觉字号。
    pub unscaled_font_size: f32,
    /// 字体是否内嵌。
    pub is_embedded: bool,
    /// 字体标志位。
    pub font_flags: FontFlags,
    /// 填充色（sRGB 0..1）。
    pub fill: Color,
    /// PDF `Tr` 文本渲染模式 0..=7；pdfium 无法识别时为 [`RENDER_MODE_UNKNOWN`]。
    pub render_mode: u8,
    /// 该对象内的字符。
    pub chars: Vec<TextChar>,
}

// ---------------------------------------------------------------------------
// worker
// ---------------------------------------------------------------------------

/// 投递给 worker 线程的任务。
type Job = Box<dyn FnOnce(&mut WorkerState) + Send>;

/// worker 线程内部状态。`Pdfium` 本身放在进程级 `OnceLock` 里，
/// 这样 `PdfDocument` 才能拿到 `'static` 生命周期存进表中。
struct WorkerState {
    pdfium: &'static Pdfium,
    docs: HashMap<u32, PdfDocument<'static>>,
    next_id: u32,
}

/// 进程内唯一的 `Pdfium` 实例。pdfium-render 的库绑定本身就是进程级 `OnceCell`，
/// 重复 `bind_to_library` 会报 `PdfiumLibraryBindingsAlreadyInitialized`。
static PDFIUM: OnceLock<Pdfium> = OnceLock::new();

/// 进程内唯一的 worker 线程。`Err` 表示初始化失败，后续 `spawn()` 直接复现同一错误。
static WORKER: OnceLock<std::result::Result<Sender<Job>, String>> = OnceLock::new();

fn init_pdfium() -> Result<&'static Pdfium> {
    if let Some(p) = PDFIUM.get() {
        return Ok(p);
    }
    let dir = syncpdf_core::fixtures::pdfium_lib_dir().ok_or(PdfiumError::LibraryNotFound)?;
    let lib = Pdfium::pdfium_platform_library_name_at_path(&dir);
    let bindings = Pdfium::bind_to_library(&lib)
        .map_err(|e| PdfiumError::Init(format!("加载 {} 失败：{}", lib.display(), e)))?;
    // set() 失败说明有别的线程抢先初始化了，直接用它的实例即可。
    let _ = PDFIUM.set(Pdfium::new(bindings));
    PDFIUM
        .get()
        .ok_or_else(|| PdfiumError::Init("Pdfium 实例初始化失败".to_string()))
}

/// pdfium 单线程队列句柄。克隆代价极低，所有克隆共享同一条 worker 线程。
///
/// 注意：pdfium 的库绑定是进程级单例，因此本进程内只会有一条 worker 线程；
/// 多次调用 [`PdfiumWorker::spawn`] 返回的是同一条线程的句柄。
#[derive(Debug, Clone)]
pub struct PdfiumWorker {
    jobs: Sender<Job>,
}

impl PdfiumWorker {
    /// 启动（或复用）pdfium worker 线程。
    pub fn spawn() -> Result<Self> {
        let slot = WORKER.get_or_init(|| {
            let (job_tx, job_rx) = unbounded::<Job>();
            let (ready_tx, ready_rx) = bounded::<std::result::Result<(), String>>(1);
            std::thread::Builder::new()
                .name("syncpdf-pdfium".to_string())
                .spawn(move || {
                    let pdfium = match init_pdfium() {
                        Ok(p) => {
                            let _ = ready_tx.send(Ok(()));
                            p
                        }
                        Err(e) => {
                            let _ = ready_tx.send(Err(e.to_string()));
                            return;
                        }
                    };
                    let mut state = WorkerState {
                        pdfium,
                        docs: HashMap::new(),
                        next_id: 1,
                    };
                    while let Ok(job) = job_rx.recv() {
                        job(&mut state);
                    }
                })
                .map_err(|e| format!("无法创建 pdfium worker 线程：{e}"))?;
            match ready_rx.recv() {
                Ok(Ok(())) => Ok(job_tx),
                Ok(Err(e)) => Err(e),
                Err(_) => Err("pdfium worker 线程启动时退出".to_string()),
            }
        });
        match slot {
            Ok(jobs) => Ok(Self { jobs: jobs.clone() }),
            Err(e) => Err(PdfiumError::Init(e.clone())),
        }
    }

    /// 在 worker 线程上执行闭包并同步等待结果。这是给上层做「pdfium 逃生舱」用的，
    /// 常规操作请用本类型的具名方法。
    pub fn call<R, F>(&self, f: F) -> Result<R>
    where
        F: FnOnce(&Pdfium) -> R + Send + 'static,
        R: Send + 'static,
    {
        self.exec(move |state| f(state.pdfium))
    }

    fn exec<R, F>(&self, f: F) -> Result<R>
    where
        F: FnOnce(&mut WorkerState) -> R + Send + 'static,
        R: Send + 'static,
    {
        let (tx, rx) = bounded::<R>(1);
        let job: Job = Box::new(move |state| {
            let _ = tx.send(f(state));
        });
        self.jobs.send(job).map_err(|_| PdfiumError::WorkerGone)?;
        rx.recv().map_err(|_| PdfiumError::WorkerGone)
    }

    /// 打开文档，返回句柄。不支持加密口令（本阶段不需要）。
    pub fn open(&self, path: &Path) -> Result<DocId> {
        let path = path.to_path_buf();
        self.exec(move |state| {
            let pdfium = state.pdfium;
            let doc = pdfium
                .load_pdf_from_file(path.as_path(), None)
                .map_err(|source| PdfiumError::Open { path, source })?;
            let id = state.next_id;
            state.next_id = state.next_id.wrapping_add(1);
            state.docs.insert(id, doc);
            Ok(DocId(id))
        })?
    }

    /// 关闭文档并释放 pdfium 侧资源。句柄不存在时静默忽略。
    pub fn close(&self, doc: DocId) {
        let _ = self.exec(move |state| {
            state.docs.remove(&doc.0);
        });
    }

    /// 页数。
    pub fn page_count(&self, doc: DocId) -> Result<u32> {
        self.exec(move |state| {
            let d = lookup(state, doc)?;
            Ok(d.pages().len().max(0) as u32)
        })?
    }

    /// 页面几何。
    pub fn page_info(&self, doc: DocId, page: u32) -> Result<PageInfo> {
        self.exec(move |state| {
            let d = lookup(state, doc)?;
            let pages = d.pages();
            let count = pages.len().max(0) as u32;
            if page >= count {
                return Err(PdfiumError::PageOutOfBounds { page, count });
            }
            let p = pages.get(page as i32)?;
            let width = p.width().value;
            let height = p.height().value;
            let boundaries = p.boundaries();
            let media = boundaries
                .media()
                .map(|b| to_rect(b.bounds))
                .unwrap_or_else(|_| Rect::new(0.0, 0.0, width, height));
            let crop = boundaries
                .crop()
                .map(|b| to_rect(b.bounds))
                .unwrap_or(media);
            let rotation = p.rotation().map(|r| r.as_degrees() as i32).unwrap_or(0);
            Ok(PageInfo {
                width,
                height,
                media_box: media,
                crop_box: crop,
                rotation,
            })
        })?
    }

    /// 按 dpi 渲染整页为 RGBA8 位图（不做 png 编码）。
    ///
    /// 像素宽 = `round(页宽pt / 72 * dpi)`，高按页面宽高比由 pdfium 推出。
    pub fn render_page(&self, doc: DocId, page: u32, dpi: f32) -> Result<RgbaBitmap> {
        if !(dpi.is_finite() && dpi > 0.0) {
            return Err(PdfiumError::InvalidArgument(format!("dpi 非法：{dpi}")));
        }
        self.exec(move |state| {
            let d = lookup(state, doc)?;
            let pages = d.pages();
            let count = pages.len().max(0) as u32;
            if page >= count {
                return Err(PdfiumError::PageOutOfBounds { page, count });
            }
            let p = pages.get(page as i32)?;
            let scale = dpi / 72.0;
            let target_width = (p.width().value * scale).round().max(1.0) as Pixels;
            let config = PdfRenderConfig::new().set_target_width(target_width);
            let bitmap = p.render_with_config(&config)?;
            Ok(RgbaBitmap {
                width: bitmap.width().max(0) as u32,
                height: bitmap.height().max(0) as u32,
                data: bitmap.as_rgba_bytes(),
            })
        })?
    }

    /// 按内容流顺序枚举页面上的文本对象与字符几何，递归进入 Form XObject。
    pub fn page_text_objects(&self, doc: DocId, page: u32) -> Result<Vec<TextObject>> {
        self.exec(move |state| {
            let d = lookup(state, doc)?;
            let pages = d.pages();
            let count = pages.len().max(0) as u32;
            if page >= count {
                return Err(PdfiumError::PageOutOfBounds { page, count });
            }
            let p = pages.get(page as i32)?;
            let text = p.text()?;
            let objects = p.objects();
            let mut level = Vec::with_capacity(objects.len());
            for i in 0..objects.len() {
                level.push(objects.get(i)?);
            }
            let mut out = Vec::new();
            let mut form_path = Vec::new();
            let mut next_index = 0u32;
            visit_objects(level, &mut form_path, &text, &mut out, &mut next_index, 0)?;
            Ok(out)
        })?
    }

    /// 文档是否含数字签名。
    pub fn has_signatures(&self, doc: DocId) -> Result<bool> {
        self.exec(move |state| {
            let d = lookup(state, doc)?;
            Ok(!d.signatures().is_empty())
        })?
    }

    /// 文档是否加密。
    ///
    /// pdfium 的 `FPDF_GetSecurityHandlerRevision` 对未加密文档返回 -1
    /// （映射为 [`PdfSecurityHandlerRevision::Unprotected`]）；
    /// AES-256（revision 5/6）不在 pdfium-render 的枚举里，会返回 `Err`，此时按「已加密」处理。
    pub fn is_encrypted(&self, doc: DocId) -> Result<bool> {
        self.exec(move |state| {
            let d = lookup(state, doc)?;
            Ok(match d.permissions().security_handler_revision() {
                Ok(PdfSecurityHandlerRevision::Unprotected) => false,
                Ok(_) => true,
                Err(_) => true,
            })
        })?
    }
}

// ---------------------------------------------------------------------------
// worker 线程内部辅助
// ---------------------------------------------------------------------------

fn lookup(state: &WorkerState, doc: DocId) -> Result<&PdfDocument<'static>> {
    state
        .docs
        .get(&doc.0)
        .ok_or(PdfiumError::UnknownDocument(doc))
}

fn to_rect(r: PdfRect) -> Rect {
    Rect::new(
        r.left().value,
        r.bottom().value,
        r.right().value,
        r.top().value,
    )
}

/// 递归遍历一层页面对象，收集文本对象。`level` 必须按内容流顺序排列。
fn visit_objects<'a>(
    level: Vec<PdfPageObject<'a>>,
    form_path: &mut Vec<u32>,
    text: &PdfPageText<'_>,
    out: &mut Vec<TextObject>,
    next_index: &mut u32,
    depth: u32,
) -> Result<()> {
    for (i, object) in level.into_iter().enumerate() {
        match object {
            PdfPageObject::Text(text_object) => {
                let built = build_text_object(&text_object, text, *next_index, form_path)?;
                *next_index += 1;
                out.push(built);
            }
            PdfPageObject::XObjectForm(form) => {
                if depth >= MAX_FORM_DEPTH {
                    tracing::warn!(depth, "Form XObject 嵌套过深，停止递归");
                    continue;
                }
                let mut children = Vec::with_capacity(form.len());
                for j in 0..form.len() {
                    children.push(form.get(j)?);
                }
                form_path.push(i as u32);
                let result = visit_objects(children, form_path, text, out, next_index, depth + 1);
                form_path.pop();
                result?;
            }
            _ => {}
        }
    }
    Ok(())
}

fn build_text_object(
    object: &PdfPageTextObject<'_>,
    text: &PdfPageText<'_>,
    index: u32,
    form_path: &[u32],
) -> Result<TextObject> {
    let font = object.font();
    let font_flags = FontFlags {
        serif: font.is_serif(),
        fixed: font.is_fixed_pitch(),
        italic: font.is_italic(),
        bold: is_bold(&font),
    };
    let font_name = font.name();
    let is_embedded = font.is_embedded().unwrap_or(false);
    drop(font);

    let fill = object
        .fill_color()
        .map(|c| Color::from_rgb8(c.red(), c.green(), c.blue()))
        .unwrap_or(Color::BLACK);

    let chars = text.chars_for_object(object)?;
    let mut collected = Vec::with_capacity(chars.len());
    for i in 0..chars.len() {
        let ch = chars.get(i)?;
        let bbox = ch
            .loose_bounds()
            .or_else(|_| ch.tight_bounds())
            .map(to_rect)
            .unwrap_or_default();
        let origin = ch
            .origin()
            .map(|(x, y)| Point::new(x.value, y.value))
            .unwrap_or(Point::new(bbox.x0, bbox.y0));
        collected.push(TextChar {
            unicode: ch.unicode_string(),
            bbox,
            origin,
            width: bbox.width(),
            angle: ch.angle_degrees().unwrap_or(0.0),
            is_generated: ch.is_generated().unwrap_or(false),
        });
    }

    Ok(TextObject {
        index,
        form_path: form_path.to_vec(),
        font_name,
        font_size: object.scaled_font_size().value,
        unscaled_font_size: object.unscaled_font_size().value,
        is_embedded,
        font_flags,
        fill,
        render_mode: render_mode_to_u8(object.render_mode()),
        chars: collected,
    })
}

fn is_bold(font: &pdfium_render::prelude::PdfFont<'_>) -> bool {
    use pdfium_render::prelude::PdfFontWeight::*;
    match font.weight() {
        Ok(Weight600) | Ok(Weight700Bold) | Ok(Weight800) | Ok(Weight900) => true,
        Ok(Custom(w)) => w >= 600,
        _ => font.is_bold_reenforced(),
    }
}

/// 映射到 PDF `Tr` 操作符的标准取值 0..=7。
fn render_mode_to_u8(mode: PdfPageTextRenderMode) -> u8 {
    match mode {
        PdfPageTextRenderMode::FilledUnstroked => 0,
        PdfPageTextRenderMode::StrokedUnfilled => 1,
        PdfPageTextRenderMode::FilledThenStroked => 2,
        PdfPageTextRenderMode::Invisible => 3,
        PdfPageTextRenderMode::FilledUnstrokedClipping => 4,
        PdfPageTextRenderMode::StrokedUnfilledClipping => 5,
        PdfPageTextRenderMode::FilledThenStrokedClipping => 6,
        PdfPageTextRenderMode::InvisibleClipping => 7,
        PdfPageTextRenderMode::Unknown => RENDER_MODE_UNKNOWN,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pdfium_render::prelude::PdfPoints;

    #[test]
    fn render_mode_covers_pdf_tr_values() {
        assert_eq!(render_mode_to_u8(PdfPageTextRenderMode::FilledUnstroked), 0);
        assert_eq!(render_mode_to_u8(PdfPageTextRenderMode::Invisible), 3);
        assert_eq!(
            render_mode_to_u8(PdfPageTextRenderMode::InvisibleClipping),
            7
        );
        assert_eq!(
            render_mode_to_u8(PdfPageTextRenderMode::Unknown),
            RENDER_MODE_UNKNOWN
        );
    }

    #[test]
    fn doc_id_display_and_value() {
        let id = DocId(7);
        assert_eq!(id.as_u32(), 7);
        assert_eq!(id.to_string(), "doc#7");
    }

    #[test]
    fn rect_conversion_is_bottom_left_origin() {
        let r = to_rect(PdfRect::new(
            PdfPoints::new(10.0),
            PdfPoints::new(1.0),
            PdfPoints::new(30.0),
            PdfPoints::new(5.0),
        ));
        assert_eq!(r, Rect::new(1.0, 10.0, 5.0, 30.0));
    }

    #[test]
    fn bitmap_debug_hides_pixels() {
        let b = RgbaBitmap {
            width: 2,
            height: 1,
            data: vec![0; 8],
        };
        let s = format!("{b:?}");
        assert!(s.contains("data_len: 8"), "{s}");
    }
}
