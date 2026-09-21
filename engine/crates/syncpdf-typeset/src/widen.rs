//! 邻接间隙加宽。设计基准：02-技术路径与架构.md §8.2 步骤 5；
//! hjfy research/02 §4（`infer_widening_limit` / `clip_widening_to_obstacles`）。
//!
//! 同列上下相邻文本区域（水平重叠 + 垂直中心差 <= `MAX_CENTER_DELTA`）给出去向；
//! 加宽上限 = 与邻居的间隙；再裁剪到障碍物（图像 / 路径 bbox）。

use syncpdf_core::Rect;

/// 垂直中心差上限（pt），与 hjfy `infer_widening_limit` 的 6.0 一致。
pub const MAX_CENTER_DELTA: f32 = 6.0;

/// 求加宽后的框：优先向下（行向下延伸），其次向上。
///
/// `bbox` 为原段落框，`neighbors` 为候选邻居框（同列相邻文本区域），
/// `obstacles` 为障碍（图像 / 路径等）。
///
/// 返回 `Some(widened)` 当且仅当存在可用间隙；`None` 表示无加宽余地。
/// 结果保证至少包含原框。
pub fn widen(bbox: &Rect, neighbors: &[Rect], obstacles: &[Rect]) -> Option<Rect> {
    let mut widened = *bbox;

    // 尝试向下：找垂直中心差在阈值内、且在 bbox 下方有实际间隙的同列邻居。
    if let Some(gap) = gap_below(bbox, neighbors) {
        let candidate = Rect::new(bbox.x0, bbox.y0 - gap, bbox.x1, bbox.y1);
        widened = clip_to_obstacles(candidate, obstacles, bbox);
    }
    if widened.height() > bbox.height() + 1e-3 {
        return Some(widened);
    }

    // 向上。
    if let Some(gap) = gap_above(bbox, neighbors) {
        let candidate = Rect::new(bbox.x0, bbox.y0, bbox.x1, bbox.y1 + gap);
        widened = clip_to_obstacles(candidate, obstacles, bbox);
    }
    if widened.height() > bbox.height() + 1e-3 {
        return Some(widened);
    }

    None
}

/// 向下可用间隙：同列（水平重叠）且在 bbox 下方的邻居，取最近者与 bbox 的间隙。
///
/// 中心差上限只约束「水平相邻」的同列判定（hjfy `infer_widening_limit`）；
/// 垂直堆叠的邻居天然满足同列，按实际间隙取值。
fn gap_below(bbox: &Rect, neighbors: &[Rect]) -> Option<f32> {
    let mut gap: Option<f32> = None;
    for n in neighbors {
        if !same_column(bbox, n) {
            continue;
        }
        let g = bbox.y0 - n.y1;
        if g > 1e-3 {
            gap = Some(match gap {
                Some(cur) => cur.min(g),
                None => g,
            });
        }
    }
    gap
}

/// 向上可用间隙。
fn gap_above(bbox: &Rect, neighbors: &[Rect]) -> Option<f32> {
    let mut gap: Option<f32> = None;
    for n in neighbors {
        if !same_column(bbox, n) {
            continue;
        }
        let g = n.y0 - bbox.y1;
        if g > 1e-3 {
            gap = Some(match gap {
                Some(cur) => cur.min(g),
                None => g,
            });
        }
    }
    gap
}

/// 同列判定：水平重叠（垂直堆叠邻居），或水平相邻且垂直中心差 <= 上限。
fn same_column(bbox: &Rect, n: &Rect) -> bool {
    let x_overlap = n.x1 > bbox.x0 + 1e-3 && n.x0 < bbox.x1 - 1e-3;
    if x_overlap {
        return true;
    }
    // 水平相邻：垂直中心差约束（hjfy 6pt 规则）。
    let dc = (n.center().y - bbox.center().y).abs();
    dc <= MAX_CENTER_DELTA
}

/// 把候选框裁剪到不与任何障碍相交；`orig` 保证裁剪不切进原框。
fn clip_to_obstacles(candidate: Rect, obstacles: &[Rect], orig: &Rect) -> Rect {
    let mut rect = candidate;
    for ob in obstacles {
        if !rect.intersects(ob) {
            continue;
        }
        // 障碍重叠时从远离 orig 中心的一侧收缩。
        // 向下扩的框：障碍在上半部 → 收 y0 到 ob.y1；在下半部 → 收 y0。
        if ob.y0 < orig.y0 {
            // 障碍横跨/位于原框上方 → 只能收 y0（下方扩展边）。
            rect.y0 = rect.y0.max(ob.y1);
        } else if ob.y1 > orig.y1 {
            // 障碍位于原框下方 → 收 y1（上方扩展边）。
            rect.y1 = rect.y1.min(ob.y0);
        }
        // 障碍完全在原框内部：无法裁剪，忽略（保持原框完整）。
    }
    Rect::new(rect.x0, rect.y0, rect.x1, rect.y1.max(rect.y0))
}

#[cfg(test)]
mod tests {
    use super::*;

    const EPS: f32 = 1e-3;

    fn close(a: f32, b: f32) -> bool {
        (a - b).abs() < EPS
    }

    #[test]
    fn no_neighbors_no_widen() {
        let bbox = Rect::new(0.0, 100.0, 200.0, 120.0);
        assert!(widen(&bbox, &[], &[]).is_none());
    }

    #[test]
    fn widen_down_by_gap() {
        // 原框底 100，邻居顶 80 → 下方 20pt 空隙。
        let bbox = Rect::new(0.0, 100.0, 200.0, 120.0);
        let neighbor = Rect::new(10.0, 60.0, 190.0, 80.0);
        let w = widen(&bbox, &[neighbor], &[]).unwrap();
        assert!(close(w.y1, 120.0));
        assert!(close(w.y0, 80.0), "y0={}", w.y0);
        assert!(close(w.height(), 40.0));
    }

    #[test]
    fn widen_respects_gap_limit() {
        // 紧贴的邻居（间隙 0）→ 无加宽。
        let bbox = Rect::new(0.0, 100.0, 200.0, 120.0);
        let neighbor = Rect::new(10.0, 100.0, 190.0, 120.0);
        assert!(widen(&bbox, &[neighbor], &[]).is_none());

        // 间隙 15 → 只加宽 15，不越邻居顶。
        let neighbor2 = Rect::new(10.0, 65.0, 190.0, 85.0);
        let w = widen(&bbox, &[neighbor2], &[]).unwrap();
        assert!(close(w.y0, 85.0), "y0={}", w.y0);
        assert!(close(w.height(), 35.0));
    }

    #[test]
    fn different_column_not_used() {
        let bbox = Rect::new(0.0, 100.0, 100.0, 120.0);
        let neighbor = Rect::new(150.0, 80.0, 250.0, 100.0); // 无水平重叠
        assert!(widen(&bbox, &[neighbor], &[]).is_none());
    }

    #[test]
    fn center_delta_limit() {
        let bbox = Rect::new(0.0, 100.0, 200.0, 120.0);
        // 侧向邻居（无水平重叠）：中心差 30 > 6 → 不算同列，不加宽。
        let neighbor = Rect::new(210.0, 40.0, 400.0, 60.0);
        assert!(widen(&bbox, &[neighbor], &[]).is_none());
    }

    #[test]
    fn clip_to_obstacle_below() {
        let bbox = Rect::new(0.0, 100.0, 200.0, 120.0);
        let neighbor = Rect::new(10.0, 40.0, 190.0, 60.0); // 下方 40pt 空隙
        let obstacle = Rect::new(50.0, 75.0, 150.0, 85.0); // 空隙中的图像
        let w = widen(&bbox, &[neighbor], &[obstacle]).unwrap();
        // 只能扩到障碍上沿 85。
        assert!(close(w.y0, 85.0), "y0={}", w.y0);
    }

    #[test]
    fn widen_up_when_only_option() {
        let bbox = Rect::new(0.0, 100.0, 200.0, 120.0);
        // 下方无邻居。
        // 上方有 20pt 空隙的邻居。
        let neighbor = Rect::new(10.0, 140.0, 190.0, 160.0);
        let w = widen(&bbox, &[neighbor], &[]).unwrap();
        assert!(close(w.y1, 140.0), "y1={}", w.y1);
        assert!(close(w.y0, 100.0));
    }
}
