//! XY-cut 阅读顺序兜底：模型无阅读顺序头（或序值可疑）时使用。
//!
//! 递归投影切分：先按 x 间隙切列，列内再按 y 间隙切块，如此往复。
//! 列优先（同一水平带上的多列按 x 排）、每列内从上到下。

use syncpdf_core::Rect;

/// 计算 `regions` 的阅读序号，`result[i]` 是 `regions[i]` 的序号（0 基，`regions.len()` 个）。
///
/// `page_h` 为页高（PDF 用户空间），用于把 y 上下文换成自上而下的顺序；
/// 若调用方传入的是图像坐标（左上原点），传 `0.0` 即可（此时 y 越大越靠后）。
pub fn xy_cut_order(regions: &[Rect], page_h: f32) -> Vec<u32> {
    let n = regions.len();
    let mut order = vec![u32::MAX; n];
    if n == 0 {
        return order;
    }
    // 顶边距页顶的距离（小 = 靠上）。PDF 用户空间（page_h > 0）y 大在上，顶边是
    // y1 → page_h - y1；图像坐标（page_h = 0）y 小在上，顶边是 y0 → y0。
    let top_key = |r: &Rect| -> f32 {
        if page_h > 0.0 {
            page_h - r.y1
        } else {
            r.y0
        }
    };
    // 底边距页顶的距离（小 = 靠上），与 top_key 同一坐标系：PDF → page_h - y0，
    // 图像 → y1。
    let bot_key = |r: &Rect| -> f32 {
        if page_h > 0.0 {
            page_h - r.y0
        } else {
            r.y1
        }
    };
    let mut idx: Vec<usize> = (0..n).collect();
    let mut next = 0u32;
    cut(&mut idx, regions, &top_key, &bot_key, &mut order, &mut next);
    order
}

/// 在当前区域集合上递归切分并分配序号。
///
/// 返回前 `order[i]` 已填好。序号分配规则：x 切分成列后按列 x 升序逐列处理；
/// y 切分成带后按带自上而下处理；两种切分都不可行时（互相重叠或无间隙）
/// 按 y（自上而下）再 x 排序线性输出。
fn cut(
    idx: &mut [usize],
    regions: &[Rect],
    top_key: &dyn Fn(&Rect) -> f32,
    bot_key: &dyn Fn(&Rect) -> f32,
    order: &mut [u32],
    next: &mut u32,
) {
    if idx.is_empty() {
        return;
    }
    if idx.len() == 1 {
        order[idx[0]] = *next;
        *next += 1;
        return;
    }
    // 尝试 x 切分：按 x 排序后找相邻间隙。
    if let Some(cols) = split_x(idx, regions) {
        // 间隙切成 ≥2 组：按列 x 升序处理。
        for mut col in cols {
            cut(&mut col, regions, top_key, bot_key, order, next);
        }
        return;
    }
    // 尝试 y 切分：按 y 排序后找间隙。
    if let Some(rows) = split_y(idx, regions, top_key, bot_key) {
        for mut band in rows {
            cut(&mut band, regions, top_key, bot_key, order, next);
        }
        return;
    }
    // 都切不开：按 y 再 x 排序输出。
    let mut sorted: Vec<usize> = idx.to_vec();
    sorted.sort_by(|&a, &b| {
        top_key(&regions[a])
            .total_cmp(&top_key(&regions[b]))
            .then_with(|| regions[a].x0.total_cmp(&regions[b].x0))
            .then_with(|| a.cmp(&b))
    });
    for i in sorted {
        order[i] = *next;
        *next += 1;
    }
}

/// x 方向切分：返回 `None` 表示无有效间隙（或切出的组与整体相同）。
fn split_x(idx: &[usize], regions: &[Rect]) -> Option<Vec<Vec<usize>>> {
    let mut by_x: Vec<usize> = idx.to_vec();
    by_x.sort_by(|&a, &b| {
        regions[a]
            .x0
            .total_cmp(&regions[b].x0)
            .then_with(|| a.cmp(&b))
    });
    let mut groups: Vec<Vec<usize>> = Vec::new();
    let mut group: Vec<usize> = vec![by_x[0]];
    let mut max_x1 = regions[by_x[0]].x1;
    for &i in &by_x[1..] {
        if regions[i].x0 >= max_x1 {
            // 全间隙：与当前组在 x 上无任何重叠。当前组封口，i 开新组。
            groups.push(std::mem::take(&mut group));
            group.push(i);
            max_x1 = regions[i].x1;
        } else {
            max_x1 = max_x1.max(regions[i].x1);
            group.push(i);
        }
    }
    groups.push(group);
    if groups.len() < 2 {
        return None;
    }
    Some(groups)
}

/// y 方向切分：按顶边自上而下扫描，块的顶边落在已聚组**垂直并集底边**之下时切组
/// （并集底边 = 组内 `bot_key` 最大值，垂直方向传递重叠的块留在同组）。
fn split_y(
    idx: &[usize],
    regions: &[Rect],
    top_key: &dyn Fn(&Rect) -> f32,
    bot_key: &dyn Fn(&Rect) -> f32,
) -> Option<Vec<Vec<usize>>> {
    let mut by_y: Vec<usize> = idx.to_vec();
    // top_key 把 PDF / 图像两种坐标系统一成「小 = 上」。
    by_y.sort_by(|&a, &b| {
        top_key(&regions[a])
            .total_cmp(&top_key(&regions[b]))
            .then_with(|| a.cmp(&b))
    });
    let mut groups: Vec<Vec<usize>> = Vec::new();
    let mut group: Vec<usize> = vec![by_y[0]];
    let mut max_bottom = bot_key(&regions[by_y[0]]);
    for &i in &by_y[1..] {
        if top_key(&regions[i]) >= max_bottom {
            // 全间隙：i 的顶边在当前组并集底边之下 → 切组，i 开新组。
            groups.push(std::mem::take(&mut group));
            group.push(i);
            max_bottom = bot_key(&regions[i]);
        } else {
            max_bottom = max_bottom.max(bot_key(&regions[i]));
            group.push(i);
        }
    }
    groups.push(group);
    if groups.len() < 2 {
        return None;
    }
    Some(groups)
}

#[cfg(test)]
mod tests {
    use super::*;

    const PH: f32 = 100.0; // PDF 用户空间页高

    /// 便捷：用 (x0, y0_pdf, x1, y1_pdf) 构造
    fn r(x0: f32, y0: f32, x1: f32, y1: f32) -> Rect {
        Rect::new(x0, y0, x1, y1)
    }

    #[test]
    fn empty_and_single() {
        assert!(xy_cut_order(&[], PH).is_empty());
        assert_eq!(xy_cut_order(&[r(0.0, 0.0, 10.0, 10.0)], PH), vec![0]);
    }

    #[test]
    fn single_column_top_to_bottom() {
        // 一列三个块，自上而下（PDF y 大在上）
        let regions = [
            r(0.0, 80.0, 50.0, 100.0), // 顶
            r(0.0, 40.0, 50.0, 60.0),  // 中
            r(0.0, 0.0, 50.0, 20.0),   // 底
        ];
        let o = xy_cut_order(&regions, PH);
        assert_eq!(o, vec![0, 1, 2]);
    }

    #[test]
    fn two_columns_left_to_right() {
        // 两列不重叠，每列两个块
        let regions = [
            r(60.0, 40.0, 100.0, 60.0),  // 右列下
            r(0.0, 80.0, 40.0, 100.0),   // 左列上
            r(60.0, 80.0, 100.0, 100.0), // 右列上
            r(0.0, 40.0, 40.0, 60.0),    // 左列下
        ];
        let o = xy_cut_order(&regions, PH);
        // 期望顺序：左上(1)=0、左下(3)=1、右上(2)=2、右下(0)=3
        assert_eq!(o, vec![3, 0, 2, 1]);
    }

    #[test]
    fn column_priority_over_rows_when_clear_gap() {
        // 两个明显的列（x 有全间隙）→ 列优先，即使右列整体更靠上
        let regions = [
            r(100.0, 90.0, 150.0, 100.0), // 右列（更靠上）
            r(0.0, 0.0, 50.0, 100.0),     // 左列（占满整高）
        ];
        let o = xy_cut_order(&regions, PH);
        // 左列先：regions[1] → 0
        assert_eq!(o, vec![1, 0]);
    }

    #[test]
    fn y_cut_within_column() {
        // x 无间隙（左右交错）→ y 切分
        let regions = [
            r(30.0, 80.0, 80.0, 100.0), // 上（右偏）
            r(0.0, 60.0, 50.0, 70.0),   // 中（左偏，与上块 y 相邻但 y 不重叠）
            r(10.0, 0.0, 60.0, 20.0),   // 下
        ];
        let o = xy_cut_order(&regions, PH);
        assert_eq!(o, vec![0, 1, 2]);
    }

    #[test]
    fn nested_overlap_falls_back_to_sort() {
        // 互相包含，切不开 → 线性排序（y 自上而下）
        let regions = [
            r(0.0, 0.0, 100.0, 100.0), // 大框
            r(10.0, 10.0, 90.0, 90.0), // 中框
            r(20.0, 20.0, 80.0, 80.0), // 小框
        ];
        let o = xy_cut_order(&regions, PH);
        assert_eq!(o, vec![0, 1, 2]); // 自上而下（y0 大优先）
    }

    #[test]
    fn three_columns_then_rows() {
        // 三列，每列上下两块；x 切 3 列，列内 y 切 2
        let regions = vec![
            // 列 0
            r(0.0, 80.0, 30.0, 100.0),
            r(0.0, 0.0, 30.0, 20.0),
            // 列 1
            r(40.0, 80.0, 70.0, 100.0),
            r(40.0, 0.0, 70.0, 20.0),
            // 列 2
            r(80.0, 80.0, 110.0, 100.0),
            r(80.0, 0.0, 110.0, 20.0),
        ];
        let o = xy_cut_order(&regions, PH);
        // 列 0 上(0)=0 下(1)=1；列 1 上(2)=2 下(3)=3；列 2 上(4)=4 下(5)=5
        assert_eq!(o, vec![0, 1, 2, 3, 4, 5]);
    }

    #[test]
    fn image_coordinates_top_left_origin() {
        // 图像坐标（左上原点）：page_h=0，y0 小在上
        let regions = [
            r(0.0, 100.0, 50.0, 150.0), // 下（图像 y 大）
            r(0.0, 0.0, 50.0, 50.0),    // 上（图像 y 小）
        ];
        let o = xy_cut_order(&regions, 0.0);
        assert_eq!(o, vec![1, 0]);
    }

    #[test]
    fn order_is_permutation() {
        let regions = [
            r(0.0, 50.0, 40.0, 90.0),
            r(60.0, 10.0, 100.0, 60.0),
            r(0.0, 0.0, 40.0, 40.0),
            r(60.0, 70.0, 100.0, 95.0),
        ];
        let o = xy_cut_order(&regions, PH);
        let mut sorted = o.clone();
        sorted.sort_unstable();
        assert_eq!(sorted, vec![0, 1, 2, 3]);
    }

    #[test]
    fn touching_boxes_stay_one_group() {
        // 边相邻（x1 == x0'）不算全间隙？按 `x0 >= max_x1` 判定，相等即切。
        // 但 y 上相邻（y1 == y0'）也切。边界盒相邻时切分是允许的。
        let regions = [
            r(0.0, 50.0, 50.0, 100.0),
            r(0.0, 0.0, 50.0, 50.0), // y 相接
        ];
        let o = xy_cut_order(&regions, PH);
        assert_eq!(o, vec![0, 1]);
    }
}
