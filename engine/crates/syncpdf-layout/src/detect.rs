//! 推理入口：RGBA 位图 → 预处理（缩放 + /255）→ ort 推理 → 阈值/NMS 后处理 → `Detection`。

use syncpdf_core::ir::RegionKind;
use syncpdf_core::Rect;

use crate::labels;
use crate::postprocess;
use crate::session::{LayoutModel, SessionError};

/// 待检测位图（左上原点，RGBA8，行优先，`rgba.len() == w*h*4`）。
pub struct RawImage<'a> {
    pub width: u32,
    pub height: u32,
    pub rgba: &'a [u8],
}

impl std::fmt::Debug for RawImage<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RawImage")
            .field("width", &self.width)
            .field("height", &self.height)
            .field("rgba_len", &self.rgba.len())
            .finish()
    }
}

impl RawImage<'_> {
    fn validate(&self) -> Result<(), DetectError> {
        if self.width == 0 || self.height == 0 {
            return Err(DetectError::BadImage("empty image".into()));
        }
        if self.rgba.len() != self.width as usize * self.height as usize * 4 {
            return Err(DetectError::BadImage(format!(
                "rgba len {} != {}x{}x4",
                self.rgba.len(),
                self.width,
                self.height
            )));
        }
        Ok(())
    }
}

/// 单个检测结果（图像像素坐标，左上原点）。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Detection {
    pub kind: RegionKind,
    /// 模型原始类别 id（0..24）。
    pub raw_label: u32,
    pub score: f32,
    /// 图像像素框。
    pub bbox_px: Rect,
    /// 模型阅读顺序值（越小越先读）；无序头时为 `None`。
    pub order: Option<u32>,
}

/// 检测参数。
#[derive(Debug, Clone, Copy)]
pub struct DetectOpts {
    /// 分数阈值。
    pub score_threshold: f32,
    /// 同类 NMS IoU 阈值（跨类用 PaddleX 的 0.98）。
    pub nms_iou: f32,
}

impl Default for DetectOpts {
    fn default() -> Self {
        Self {
            score_threshold: 0.4,
            nms_iou: 0.5,
        }
    }
}

/// 检测阶段错误。
#[derive(Debug, thiserror::Error)]
pub enum DetectError {
    #[error("bad image: {0}")]
    BadImage(String),
    #[error(transparent)]
    Session(#[from] SessionError),
    #[error("model output label {0} out of range")]
    BadLabel(u32),
}

impl LayoutModel {
    /// 对一页位图推理。返回结果按阅读顺序值升序（无序值时按 `y` 再 `x`）。
    pub fn detect(
        &mut self,
        img: &RawImage<'_>,
        opts: &DetectOpts,
    ) -> Result<Vec<Detection>, DetectError> {
        img.validate()?;
        let (iw, ih) = (img.width, img.height);
        let (nw, nh) = self.input_size();
        let tensor = preprocess(img, nw, nh);
        let scale_h = nh as f32 / ih as f32;
        let scale_w = nw as f32 / iw as f32;
        let rows = self.run_raw(&tensor, ih as f32, iw as f32, scale_h, scale_w)?;

        let mut cands: Vec<Detection> = Vec::new();
        for r in rows {
            let (label, score, x1, y1, x2, y2, order) = (r[0], r[1], r[2], r[3], r[4], r[5], r[6]);
            if !(score.is_finite()) || score < opts.score_threshold {
                continue;
            }
            let label = label as u32;
            let name = labels::LABELS
                .get(label as usize)
                .ok_or(DetectError::BadLabel(label))?;
            // 输出框在原图坐标（模型已按 im_shape/scale_factor 换算），夹回图内。
            let bbox_px = Rect::new(x1, y1, x2, y2);
            if bbox_px.is_empty() {
                continue;
            }
            let bbox_px = Rect::new(
                bbox_px.x0.clamp(0.0, iw as f32),
                bbox_px.y0.clamp(0.0, ih as f32),
                bbox_px.x1.clamp(0.0, iw as f32),
                bbox_px.y1.clamp(0.0, ih as f32),
            );
            if bbox_px.is_empty() {
                continue;
            }
            cands.push(Detection {
                kind: labels::to_region_kind(name),
                raw_label: label,
                score,
                bbox_px,
                order: (order.is_finite() && order >= 0.0).then_some(order as u32),
            });
        }

        let keep = postprocess::nms(&cands, opts.nms_iou);
        let mut out: Vec<Detection> = keep.into_iter().map(|i| cands[i]).collect();
        out.sort_by(|a, b| {
            a.order
                .cmp(&b.order)
                .then_with(|| a.bbox_px.y0.total_cmp(&b.bbox_px.y0))
                .then_with(|| a.bbox_px.x0.total_cmp(&b.bbox_px.x0))
        });
        Ok(out)
    }
}

/// 像素→网络输入：双线性缩放到 `(nw, nh)`，RGB（丢 alpha），/255。
/// 实测该模型无 mean/std（仅 /255），见 session.rs 模块注释。
fn preprocess(img: &RawImage<'_>, nw: u32, nh: u32) -> Vec<f32> {
    let (iw, ih) = (img.width as usize, img.height as usize);
    let (nw, nh) = (nw as usize, nh as usize);
    // 源坐标中心对齐（与 PaddleX resize 行为一致的近似）。
    let sx = iw as f32 / nw as f32;
    let sy = ih as f32 / nh as f32;
    let mut out = vec![0.0f32; nw * nh * 3];
    for y in 0..nh {
        // 双线性：上下两行源采样
        let fy = (y as f32 + 0.5) * sy - 0.5;
        let y0 = fy.floor().clamp(0.0, (ih - 1) as f32) as usize;
        let y1 = (y0 + 1).min(ih - 1);
        let wy = (fy - y0 as f32).clamp(0.0, 1.0);
        for x in 0..nw {
            let fx = (x as f32 + 0.5) * sx - 0.5;
            let x0 = fx.floor().clamp(0.0, (iw - 1) as f32) as usize;
            let x1 = (x0 + 1).min(iw - 1);
            let wx = (fx - x0 as f32).clamp(0.0, 1.0);
            for c in 0..3 {
                let p00 = img.rgba[(y0 * iw + x0) * 4 + c] as f32;
                let p01 = img.rgba[(y0 * iw + x1) * 4 + c] as f32;
                let p10 = img.rgba[(y1 * iw + x0) * 4 + c] as f32;
                let p11 = img.rgba[(y1 * iw + x1) * 4 + c] as f32;
                let top = p00 + (p01 - p00) * wx;
                let bottom = p10 + (p11 - p10) * wx;
                out[(c * nh + y) * nw + x] = (top + (bottom - top) * wy) / 255.0;
            }
        }
    }
    out
}

/// 像素框 → PDF 用户空间（左下原点，pt）：等比缩放 + y 翻转。
///
/// `img_w/img_h` 为位图尺寸，`page_w_pt/page_h_pt` 为页面尺寸（pt）。
/// 返回的框可直接填 `Region::bbox`。
pub fn to_pdf_space(d: &Detection, img_w: u32, img_h: u32, page_w_pt: f32, page_h_pt: f32) -> Rect {
    let kx = page_w_pt / img_w as f32;
    let ky = page_h_pt / img_h as f32;
    let b = d.bbox_px;
    // 左上原点 → 左下原点：x 不变，y' = page_h - y。
    Rect::new(
        b.x0 * kx,
        page_h_pt - b.y1 * ky,
        b.x1 * kx,
        page_h_pt - b.y0 * ky,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn det(x0: f32, y0: f32, x1: f32, y1: f32) -> Detection {
        Detection {
            kind: RegionKind::Text,
            raw_label: 22,
            score: 0.9,
            bbox_px: Rect::new(x0, y0, x1, y1),
            order: None,
        }
    }

    #[test]
    fn to_pdf_space_flips_y_and_scales() {
        // 位图 100x200，页面 50x100pt（等比）。像素框 (10,20)-(30,80) →
        // pt: x 5..15；y 翻转：像素顶 80px → 距顶 40pt → PDF y=60（框底），
        // 像素底 20px → 距顶 10pt → PDF y=90（框顶），即 PDF y 60..90。
        let d = det(10.0, 20.0, 30.0, 80.0);
        let r = to_pdf_space(&d, 100, 200, 50.0, 100.0);
        assert!((r.x0 - 5.0).abs() < 1e-4, "{r:?}");
        assert!((r.x1 - 15.0).abs() < 1e-4, "{r:?}");
        assert!((r.y0 - 60.0).abs() < 1e-4, "{r:?}");
        assert!((r.y1 - 90.0).abs() < 1e-4, "{r:?}");
    }

    #[test]
    fn to_pdf_space_full_page_roundtrip() {
        let d = det(0.0, 0.0, 100.0, 200.0);
        let r = to_pdf_space(&d, 100, 200, 595.0, 842.0);
        assert!((r.x0 - 0.0).abs() < 1e-4 && (r.y0 - 0.0).abs() < 1e-4);
        assert!((r.x1 - 595.0).abs() < 1e-4 && (r.y1 - 842.0).abs() < 1e-4);
    }

    #[test]
    fn raw_image_validate() {
        let good = RawImage {
            width: 2,
            height: 2,
            rgba: &[0; 16],
        };
        assert!(good.validate().is_ok());
        let bad = RawImage {
            width: 2,
            height: 2,
            rgba: &[0; 8],
        };
        assert!(bad.validate().is_err());
        let empty = RawImage {
            width: 0,
            height: 2,
            rgba: &[],
        };
        assert!(empty.validate().is_err());
    }

    #[test]
    fn preprocess_identity_and_scale() {
        // 2x2 图放大到 2x2：数据不变
        let img = RawImage {
            width: 2,
            height: 2,
            rgba: &[
                0, 0, 0, 255, 255, 0, 0, 255, 0, 255, 0, 255, 255, 255, 255, 255,
            ],
        };
        let t = preprocess(&img, 2, 2);
        // 左上像素黑、右上红（R=255）、左下绿、右下白
        assert_eq!(t[0], 0.0); // R of (0,0)
        let r_of = |x: usize, y: usize| t[y * 2 + x];
        assert!((r_of(1, 0) - 1.0).abs() < 1e-6);
        assert!((r_of(0, 1) - 0.0).abs() < 1e-6);
        assert!((r_of(1, 1) - 1.0).abs() < 1e-6);
        // G of (0,1) = 255
        let g_of = |x: usize, y: usize| t[4 + y * 2 + x];
        assert!((g_of(0, 1) - 1.0).abs() < 1e-6);
        assert!((g_of(1, 0) - 0.0).abs() < 1e-6);
    }
}
