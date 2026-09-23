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
//! - 某些导出图另有 `fetch_name_2 [300,200,200] i32` 阅读顺序掩码图；
//!   bbox 专用图只有前两个输出，本 crate 均不消费这些附加输出。

use std::path::{Path, PathBuf};

use ort::value::{TensorElementType, ValueType};

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
    #[error("filesystem: {0}")]
    Io(#[from] std::io::Error),
}

/// 输入张量固定尺寸（宽, 高）。
pub const INPUT_SIZE: (u32, u32) = (800, 800);

/// Requested provider. `Auto` selects CoreML on macOS and CPU elsewhere.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LayoutDevice {
    Cpu,
    CoreMl,
    Auto,
}

/// Session construction options. Profiling is finalized with `end_profiling`.
#[derive(Debug, Clone)]
pub struct LayoutOptions {
    pub threads: usize,
    pub device: LayoutDevice,
    pub cache_dir: Option<PathBuf>,
    pub profile_path: Option<PathBuf>,
}

impl Default for LayoutOptions {
    fn default() -> Self {
        Self {
            threads: 2,
            device: LayoutDevice::Auto,
            cache_dir: None,
            profile_path: None,
        }
    }
}

/// PP-DocLayoutV3 检测模型句柄。线程安全：ort 的 `Session::run` 需要 `&mut`，
/// 上层并发时每线程一个实例或加锁。
pub struct LayoutModel {
    session: ort::session::Session,
    input_size: (u32, u32),
    path: PathBuf,
    options: LayoutOptions,
    configured_device: LayoutDevice,
    fallback_reason: Option<String>,
    completed_profile: Option<PathBuf>,
    profiling_active: bool,
}

impl std::fmt::Debug for LayoutModel {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LayoutModel")
            .field("input_size", &self.input_size)
            .field("configured_device", &self.configured_device)
            .field("fallback_reason", &self.fallback_reason)
            .finish_non_exhaustive()
    }
}

impl LayoutModel {
    /// 从 ONNX 文件加载。`threads` 为 intra-op 线程数（0 = ort 默认）。
    ///
    /// Compatibility entry point: always uses CPU.
    pub fn load(path: &Path, threads: usize) -> Result<Self, SessionError> {
        Self::load_with_options(
            path,
            LayoutOptions {
                threads,
                device: LayoutDevice::Cpu,
                ..LayoutOptions::default()
            },
        )
    }

    pub fn load_with_options(path: &Path, options: LayoutOptions) -> Result<Self, SessionError> {
        if !path.is_file() {
            return Err(SessionError::ModelMissing(path.to_path_buf()));
        }
        let try_coreml = match options.device {
            LayoutDevice::Cpu => false,
            LayoutDevice::CoreMl => true,
            LayoutDevice::Auto => cfg!(target_os = "macos"),
        };
        let (session, configured_device, fallback_reason) = if try_coreml {
            match Self::build_session(path, &options, LayoutDevice::CoreMl) {
                Ok(session) => (session, LayoutDevice::CoreMl, None),
                Err(err) if options.device == LayoutDevice::Auto => {
                    tracing::warn!(error = %err, "CoreML layout session failed; using CPU");
                    (
                        Self::build_session(path, &options, LayoutDevice::Cpu)?,
                        LayoutDevice::Cpu,
                        Some(err.to_string()),
                    )
                }
                Err(err) => return Err(err),
            }
        } else {
            (
                Self::build_session(path, &options, LayoutDevice::Cpu)?,
                LayoutDevice::Cpu,
                None,
            )
        };
        Ok(Self {
            session,
            input_size: INPUT_SIZE,
            path: path.to_path_buf(),
            profiling_active: options.profile_path.is_some(),
            options,
            configured_device,
            fallback_reason,
            completed_profile: None,
        })
    }

    fn build_session(
        path: &Path,
        options: &LayoutOptions,
        device: LayoutDevice,
    ) -> Result<ort::session::Session, SessionError> {
        let mut builder = ort::session::Session::builder()?;
        if options.threads > 0 {
            builder = builder
                .with_intra_threads(options.threads)
                .map_err(ort::Error::from)?;
        }
        if let Some(profile_path) = &options.profile_path {
            builder = builder
                .with_profiling(profile_path)
                .map_err(ort::Error::from)?;
        }
        if device == LayoutDevice::CoreMl {
            use ort::ep::{coreml, CoreML};
            let mut ep = CoreML::default()
                .with_model_format(coreml::ModelFormat::MLProgram)
                .with_compute_units(coreml::ComputeUnits::CPUAndGPU)
                .with_profile_compute_plan(options.profile_path.is_some());
            if let Some(cache_dir) = &options.cache_dir {
                std::fs::create_dir_all(cache_dir)?;
                ep = ep.with_model_cache_dir(cache_dir.to_string_lossy());
            }
            builder = builder
                .with_dimension_override("DynamicDimension.0", 1)
                .map_err(ort::Error::from)?
                .with_dimension_override("DynamicDimension.1", 1)
                .map_err(ort::Error::from)?
                .with_dimension_override("DynamicDimension.2", 1)
                .map_err(ort::Error::from)?
                .with_execution_providers([ep.build().error_on_failure()])
                .map_err(ort::Error::from)?;
        }
        let session = builder.commit_from_file(path)?;
        Self::validate_io(&session)?;
        Ok(session)
    }

    fn validate_io(session: &ort::session::Session) -> Result<(), SessionError> {
        let inputs = session.inputs();
        if inputs.len() < 3 {
            return Err(SessionError::BadModelIo(format!(
                "expected at least 3 inputs; got {:?}",
                inputs.iter().map(|i| i.name()).collect::<Vec<_>>()
            )));
        }
        for (name, expected) in [
            ("image", &[1, 3, 800, 800][..]),
            ("im_shape", &[1, 2][..]),
            ("scale_factor", &[1, 2][..]),
        ] {
            let Some(input) = inputs.iter().find(|i| i.name() == name) else {
                return Err(SessionError::BadModelIo(format!("missing input `{name}`")));
            };
            Self::check_tensor(name, input.dtype(), TensorElementType::Float32, expected)?;
        }
        let Some(bbox) = session
            .outputs()
            .iter()
            .find(|o| o.name() == "fetch_name_0")
        else {
            return Err(SessionError::BadModelIo(
                "missing `fetch_name_0` output".into(),
            ));
        };
        Self::check_tensor(
            "fetch_name_0",
            bbox.dtype(),
            TensorElementType::Float32,
            &[-1, 7],
        )?;
        Ok(())
    }

    fn check_tensor(
        name: &str,
        dtype: &ValueType,
        expected_type: TensorElementType,
        expected_shape: &[i64],
    ) -> Result<(), SessionError> {
        let ValueType::Tensor { ty, shape, .. } = dtype else {
            return Err(SessionError::BadModelIo(format!(
                "{name}: expected tensor, got {dtype:?}"
            )));
        };
        if *ty != expected_type
            || shape.len() != expected_shape.len()
            || shape
                .iter()
                .zip(expected_shape)
                .any(|(&got, &want)| got != -1 && want != -1 && got != want)
        {
            return Err(SessionError::BadModelIo(format!(
                "{name}: expected {expected_type:?} {expected_shape:?}, got {dtype:?}"
            )));
        }
        Ok(())
    }

    /// Provider registered for the current session. This does not prove GPU execution.
    pub fn configured_device(&self) -> LayoutDevice {
        self.configured_device
    }

    pub fn fallback_reason(&self) -> Option<&str> {
        self.fallback_reason.as_deref()
    }

    /// Finalize an optional ONNX Runtime profile and return its actual file path.
    pub fn end_profiling(&mut self) -> Result<Option<PathBuf>, SessionError> {
        if self.profiling_active {
            let path = PathBuf::from(self.session.end_profiling()?);
            self.completed_profile = Some(path);
            self.profiling_active = false;
        }
        Ok(self.completed_profile.clone())
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
        let run = |session: &mut ort::session::Session| -> Result<Vec<[f32; 7]>, SessionError> {
            let image = TensorRef::from_array_view(([1usize, 3, h, w], image_chw))
                .map_err(|e| SessionError::BadModelIo(format!("invalid image tensor: {e}")))?;
            let shape = TensorRef::from_array_view(([1usize, 2], &im_shape[..]))
                .map_err(|e| SessionError::BadModelIo(format!("invalid im_shape tensor: {e}")))?;
            let factor = TensorRef::from_array_view(([1usize, 2], &scale[..])).map_err(|e| {
                SessionError::BadModelIo(format!("invalid scale_factor tensor: {e}"))
            })?;
            let outputs = session.run(ort::inputs! {
                "image" => image,
                "im_shape" => shape,
                "scale_factor" => factor,
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
        };
        match run(&mut self.session) {
            Err(SessionError::Ort(err))
                if self.options.device == LayoutDevice::Auto
                    && self.configured_device == LayoutDevice::CoreMl =>
            {
                tracing::warn!(error = %err, "CoreML layout inference failed; using CPU");
                // Build before replacing the old session so a CPU setup error leaves this
                // instance intact and still observable to callers.
                let mut cpu_options = self.options.clone();
                // Keep the failed CoreML profile as the diagnostic artifact. A new CPU
                // profile would have a different meaning and this API returns one path.
                cpu_options.profile_path = None;
                let cpu = Self::build_session(&self.path, &cpu_options, LayoutDevice::Cpu)?;
                if self.profiling_active {
                    match self.session.end_profiling() {
                        Ok(path) => self.completed_profile = Some(PathBuf::from(path)),
                        Err(profile_err) => {
                            tracing::warn!(error = %profile_err, "failed to finalize CoreML profile")
                        }
                    }
                    self.profiling_active = false;
                }
                self.session = cpu;
                self.configured_device = LayoutDevice::Cpu;
                self.fallback_reason = Some(err.to_string());
                run(&mut self.session)
            }
            result => result,
        }
    }
}
