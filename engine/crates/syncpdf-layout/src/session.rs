//! ort session 封装：加载 PP-DocLayoutV3 ONNX 模型并报告输入尺寸。
//!
//! 模型 I/O（对 `vendor/models/pp_doc_layoutv3.onnx` 实测）：
//! - 输入 `image [N,3,800,800] f32`：RGB、/255 归一化（无 mean/std，oar-ocr 0.9.2 的
//!   `pp_doclayout` 预处理配置：scale=1/255、mean=[0,0,0]、std=[1,1,1]）。
//! - 输入 `im_shape [N,2] f32`：原图 `(H, W)`（像素）。
//! - 输入 `scale_factor [N,2] f32`：`网络输入 / 原图` 的 `(ratio_h, ratio_w)`。
//! - 输出 `fetch_name_0 [300,7] f32`：`(class, score, x1, y1, x2, y2, reading_order)`，
//!   框为**原图像素坐标**（左上原点）。
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

    /// 内部：跑一次推理。`image_chw` 为已按 `input_size` 缩放的 NCHW 数据。
    pub(crate) fn run_raw(
        &mut self,
        image_chw: &[f32],
        orig_h: f32,
        orig_w: f32,
        scale_h: f32,
        scale_w: f32,
    ) -> Result<Vec<[f32; 7]>, SessionError> {
        use ort::value::TensorRef;
        let (h, w) = (self.input_size.1 as usize, self.input_size.0 as usize);
        let im_shape = [orig_h, orig_w];
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
