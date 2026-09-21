//! 检测后处理：NMS。阈值过滤在 `detect` 内完成。

use crate::detect::Detection;

/// PaddleX 布局 NMS：同类 IoU ≥ `same_class_iou` 抑制，跨类 IoU ≥ 0.98 抑制
/// （跨类几乎重合的框视为同一目标的重复检出）。
///
/// 返回保留候选的下标（按分数降序的贪心序）。
pub fn nms(cands: &[Detection], same_class_iou: f32) -> Vec<usize> {
    let mut order: Vec<usize> = (0..cands.len()).collect();
    order.sort_by(|&a, &b| cands[b].score.total_cmp(&cands[a].score).then(a.cmp(&b)));
    let mut suppressed = vec![false; cands.len()];
    let mut keep = Vec::new();
    for pos in 0..order.len() {
        if suppressed[pos] {
            continue;
        }
        let i = order[pos];
        keep.push(i);
        for q in pos + 1..order.len() {
            let j = order[q];
            if suppressed[q] {
                continue;
            }
            let threshold = if cands[i].raw_label == cands[j].raw_label {
                same_class_iou
            } else {
                0.98
            };
            if iou(&cands[i].bbox_px, &cands[j].bbox_px) >= threshold {
                suppressed[q] = true;
            }
        }
    }
    keep
}

/// 轴对齐框 IoU（连续坐标，无 +1 像素补偿）。
fn iou(a: &syncpdf_core::Rect, b: &syncpdf_core::Rect) -> f32 {
    let inter = a.intersection(b).map(|r| r.area()).unwrap_or(0.0);
    if inter <= 0.0 {
        return 0.0;
    }
    let union = a.area() + b.area() - inter;
    if union > 0.0 {
        inter / union
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::RegionKind;
    use syncpdf_core::Rect;

    fn d(label: u32, score: f32, x0: f32, y0: f32, x1: f32, y1: f32) -> Detection {
        Detection {
            kind: match label {
                22 => RegionKind::Text,
                14 => RegionKind::Figure,
                _ => RegionKind::Other,
            },
            raw_label: label,
            score,
            bbox_px: Rect::new(x0, y0, x1, y1),
            order: None,
        }
    }

    #[test]
    fn nms_suppresses_same_class_overlap() {
        let cands = vec![
            d(22, 0.9, 0.0, 0.0, 100.0, 50.0),
            d(22, 0.8, 5.0, 5.0, 105.0, 55.0), // 与 #0 IoU≈0.82，被抑制
            d(22, 0.7, 200.0, 0.0, 300.0, 50.0), // 不重叠，保留
        ];
        let keep = nms(&cands, 0.5);
        assert_eq!(keep, vec![0, 2]);
    }

    #[test]
    fn nms_keeps_disjoint_same_class() {
        let cands = vec![
            d(22, 0.9, 0.0, 0.0, 100.0, 50.0),
            d(22, 0.8, 0.0, 60.0, 100.0, 110.0),
        ];
        let keep = nms(&cands, 0.5);
        assert_eq!(keep.len(), 2);
    }

    #[test]
    fn nms_cross_class_near_identical_suppressed() {
        let cands = vec![
            d(22, 0.9, 0.0, 0.0, 100.0, 50.0),
            d(14, 0.85, 1.0, 1.0, 101.0, 51.0), // 跨类 IoU≈0.96 ≥ 0.98？≈0.96 → 保留
        ];
        // IoU = (99*49)/(100*50 + 100*50 - 99*49) = 4851/5149 ≈ 0.942 → 不抑制
        let keep = nms(&cands, 0.5);
        assert_eq!(keep.len(), 2);
        // 完全重合的跨类框必须抑制
        let cands2 = vec![
            d(22, 0.9, 0.0, 0.0, 100.0, 50.0),
            d(14, 0.85, 0.0, 0.0, 100.0, 50.0),
        ];
        let keep2 = nms(&cands2, 0.5);
        assert_eq!(keep2, vec![0]);
    }

    #[test]
    fn nms_score_order_deterministic() {
        // 同分时按下标稳定排序
        let cands = vec![
            d(22, 0.5, 0.0, 0.0, 10.0, 10.0),
            d(22, 0.5, 20.0, 0.0, 30.0, 10.0),
        ];
        let keep = nms(&cands, 0.5);
        assert_eq!(keep, vec![0, 1]);
    }

    #[test]
    fn iou_edge_cases() {
        let a = Rect::new(0.0, 0.0, 10.0, 10.0);
        assert_eq!(iou(&a, &Rect::new(0.0, 0.0, 10.0, 10.0)), 1.0);
        assert_eq!(iou(&a, &Rect::new(20.0, 20.0, 30.0, 30.0)), 0.0);
        assert_eq!(iou(&a, &Rect::new(5.0, 5.0, 15.0, 15.0)), 25.0 / 175.0);
        // 零面积框
        assert_eq!(iou(&Rect::new(0.0, 0.0, 0.0, 10.0), &a), 0.0);
    }
}
