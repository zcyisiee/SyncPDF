//! ort session 封装：加载 PP-DocLayoutV3 ONNX 模型并报告输入尺寸。
//!
//! 模型 I/O（对 `vendor/models/pp_doc_layoutv3.onnx` 实测，并用旧 Python 后端
//! `babeldoc/docvision/paddle_runtime.py`（PaddleX 官方预处理）作 oracle 验证）：
//! - 输入 `image [N,3,800,800] f32`：**BGR**、/255 归一化（无 mean/std；官方
//!   PaddleX `ReadImage` 给 BGR，旧后端直接喂 BGR，见 paddle_runtime.py 的
//!   "Input is BGR from PaddleX ReadImage"）。
//! - 输入 `im_shape [N,2] f32`：**网络输入尺寸 (800, 800)**，不是原图 (H, W)。
//!   两者会让输出落在不同坐标空间（原图 vs 原图×原图/800，已实测）。
//! - 输入 `scale_factor [N,2] f32`：`(ratio_h, ratio_w) = 网络输入/原图`。
//! - 输出 `fetch_name_0 [300,7] f32`：`(class, score, x1, y1, x2, y2, reading_order)`，
//!   框已由图内后处理换算回**原图像素坐标**（左上原点）；PaddleX 的
//!   `LayoutAnalysisProcess` 对它只做阈值/NMS/过滤，不再缩放。
//! - 输出 `fetch_name_1 [1] i32`：框数（对导出图恒为 300，低分框靠 score 过滤）。
//! - 输出 `fetch_name_2 [300,200,200] i32`：阅读顺序掩码图（本 crate 不使用）。

use std::path::Path;

use thiserror::Error;

/// ort / 模型相关错误。
#[derive(Debug, Error)]
pub enum SessionError {
    #[error("ort: {0}")]
    Ort(#[from] ort::Error),
    #[error("model file missing: {0}")]
    ModelMissing(std::path::PathBuf),
    #[error("unexpected model i/o: {0}")]
    BadModelIo(String),
}

/// 输入张量固定尺寸（宽, 高）。
pub const INPUT_SIZE: (u32, u32) = (800, 800);

/// PP-DocLayoutV3 检测模型句柄。线程安全：ort 的 `Session::run` 需要 `&mut`，
/// 上层并发时每线程一个实例或加锁。
pub struct LayoutModel {
    session: ort::session::Session,
    input_size: (u32, u32),
}

impl std::fmt::Debug for LayoutModel {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LayoutModel")
            .field("input_size", &self.input_size)
            .finish_non_exhaustive()
    }
}

impl LayoutModel {
    /// 从 ONNX 文件加载。`threads` 为 intra-op 线程数（0 = ort 默认）。
    ///
    /// 动态库策略：crate 依赖启用 `download-binaries`（pyke 预编译静态库，macOS arm64
    /// 为 `+coreml` 构建但只经 CPU EP 执行）；也可用 `ORT_LIB_PATH` 指向本地构建。
    pub fn load(path: &Path, threads: usize) -> Result<Self, SessionError> {
        if !path.is_file() {
            return Err(SessionError::ModelMissing(path.to_path_buf()));
        }
        let mut builder = ort::session::Session::builder()?;
        if threads > 0 {
            builder = builder
                .with_intra_threads(threads)
                .map_err(ort::Error::from)?;
        }
        let session = builder.commit_from_file(path)?;
        // 校验 I/O 形状与预期一致，防止误加载其他模型。
        let has_image = session.inputs().iter().any(|i| i.name() == "image");
        if !has_image {
            return Err(SessionError::BadModelIo(format!(
                "no `image` input; got {:?}",
                session
                    .inputs()
                    .iter()
                    .map(|i| i.name())
                    .collect::<Vec<_>>()
            )));
        }
        Ok(Self {
            session,
            input_size: INPUT_SIZE,
        })
    }

    /// 网络输入位图尺寸 `(width, height)`。
    pub fn input_size(&self) -> (u32, u32) {
        self.input_size
    }

    /// 内部：跑一次推理。`image_chw` 为已按 `input_size` 缩放的 NCHW 数据（BGR）。
    ///
    /// `im_shape` 传**网络输入尺寸**（不是原图尺寸）：该导出图的内部后处理按
    /// `im_shape`/`scale_factor` 组合决定输出坐标空间，只有官方组合
    /// （im_shape=网络尺寸、scale_factor=网络/原图）才把框换算回原图像素，
    /// 传原图尺寸会把框额外放大 `原图/800` 倍（实测，见模块注释）。
    pub(crate) fn run_raw(
        &mut self,
        image_chw: &[f32],
        scale_h: f32,
        scale_w: f32,
    ) -> Result<Vec<[f32; 7]>, SessionError> {
        use ort::value::TensorRef;
        let (h, w) = (self.input_size.1 as usize, self.input_size.0 as usize);
        let im_shape = [h as f32, w as f32];
        let scale = [scale_h, scale_w];
        let outputs = self.session.run(ort::inputs! {
            "image" => TensorRef::from_array_view(([1usize, 3, h, w], image_chw))?,
            "im_shape" => TensorRef::from_array_view(([1usize, 2], &im_shape[..]))?,
            "scale_factor" => TensorRef::from_array_view(([1usize, 2], &scale[..]))?,
        })?;
        let Some(out) = outputs
            .get("fetch_name_0")
            .map(|v| v.try_extract_tensor::<f32>())
        else {
            return Err(SessionError::BadModelIo(
                "no `fetch_name_0` output".to_string(),
            ));
        };
        let (shape, data) = out?;
        if shape.len() != 2 || shape[1] as usize != 7 {
            return Err(SessionError::BadModelIo(format!(
                "fetch_name_0 shape {shape:?}, expect [N,7]"
            )));
        }
        Ok(data
            .chunks_exact(7)
            .map(|r| [r[0], r[1], r[2], r[3], r[4], r[5], r[6]])
            .collect())
    }
}
