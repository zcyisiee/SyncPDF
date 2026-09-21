//! 段落分析的行聚类（M1-06 的一小部分）：区域内字形按垂直重叠聚成行。
//!
//! 本模块只做行聚类；样式 run / 原子识别 / `translatable` 判定属后续任务。

use syncpdf_core::{GlyphId, Rect};

/// 把区域内的字形按「基线聚类」分组：同一行的判定为相邻字形的
/// **垂直重叠率 > 50%**（取 bbox 高度较小者的重叠比例）。
///
/// 行内按 x 升序。返回值顺序按行自上而下（PDF 用户空间 y 大在上）。
/// 多栏区域不做列切分（上游应先按布局区域分栏）。
pub fn group_lines(glyphs: &[(GlyphId, Rect)], region: &Rect) -> Vec<Vec<GlyphId>> {
    // 过滤出中心落在区域内的字形，按 y 降序（自上而下）再 x 升序预排。
    let mut in_region: Vec<&(GlyphId, Rect)> = glyphs
        .iter()
        .filter(|(_, b)| {
            let c = b.center();
            c.x >= region.x0 && c.x <= region.x1 && c.y >= region.y0 && c.y <= region.y1
        })
        .collect();
    in_region.sort_by(|a, b| {
        let (ka, kb) = (sort_key(&a.1), sort_key(&b.1));
        ka.total_cmp(&kb).then_with(|| a.1.x0.total_cmp(&b.1.x0))
    });

    let mut lines: Vec<Vec<&(GlyphId, Rect)>> = Vec::new();
    for g in in_region {
        match lines.last_mut() {
            Some(line) => {
                // 与当前行最后一个字形比垂直重叠；比与行内任意比更符合阅读流，
                // 也能容忍轻微倾斜。
                let prev = &line.last().expect("line is never empty").1;
                if vertical_overlap_ratio(prev, &g.1) > 0.5 {
                    line.push(g);
                } else {
                    lines.push(vec![g]);
                }
            }
            None => lines.push(vec![g]),
        }
    }
    lines
        .into_iter()
        .map(|line| {
            let mut line: Vec<(GlyphId, Rect)> =
                line.into_iter().map(|(id, b)| (*id, *b)).collect();
            line.sort_by(|a, b| a.1.x0.total_cmp(&b.1.x0).then(a.0.cmp(&b.0)));
            line.into_iter().map(|(id, _)| id).collect()
        })
        .collect()
}

/// 行排序键：PDF 用户空间 y0 大在上 → 键小排前。
fn sort_key(b: &Rect) -> f32 {
    -(b.y0 + b.y1) * 0.5
}

/// 垂直重叠率：交集高度 / 两者中较小的框高。
fn vertical_overlap_ratio(a: &Rect, b: &Rect) -> f32 {
    // 交集高度 = min(y1) - max(y0)，不相交时钳为 0。
    let overlap = (a.y1.min(b.y1) - a.y0.max(b.y0)).max(0.0);
    let min_h = a.height().min(b.height());
    if min_h <= 0.0 {
        0.0
    } else {
        overlap / min_h
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::{GlyphId, ObjRef, OpKey, PageId};

    fn gid(n: u32) -> GlyphId {
        GlyphId {
            page: PageId(0),
            op: OpKey::new(ObjRef::new(1, 0), 0),
            ordinal: n as u16,
        }
    }

    fn g(n: u32, x0: f32, y0: f32, x1: f32, y1: f32) -> (GlyphId, Rect) {
        (gid(n), Rect::new(x0, y0, x1, y1))
    }

    fn region() -> Rect {
        Rect::new(0.0, 0.0, 100.0, 100.0)
    }

    #[test]
    fn empty_and_outside() {
        assert!(group_lines(&[], &region()).is_empty());
        let glyphs = [g(0, 200.0, 200.0, 210.0, 210.0)];
        assert!(group_lines(&glyphs, &region()).is_empty());
    }

    #[test]
    fn single_line_ordered_by_x() {
        let glyphs = [
            g(2, 60.0, 80.0, 70.0, 90.0),
            g(0, 10.0, 80.0, 20.0, 90.0),
            g(1, 35.0, 80.0, 45.0, 90.0),
        ];
        let lines = group_lines(&glyphs, &region());
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0].len(), 3);
        // x 升序
        assert_eq!(lines[0][0], gid(0));
        assert_eq!(lines[0][1], gid(1));
        assert_eq!(lines[0][2], gid(2));
    }

    #[test]
    fn two_lines_split_by_vertical_gap() {
        // 上一行 y 80..90，下一行 y 60..70：垂直重叠 0 → 两行
        let glyphs = [g(0, 10.0, 80.0, 20.0, 90.0), g(1, 10.0, 60.0, 20.0, 70.0)];
        let lines = group_lines(&glyphs, &region());
        assert_eq!(lines.len(), 2);
        // 自上而下：先 y 高的行
        assert_eq!(lines[0][0], gid(0));
        assert_eq!(lines[1][0], gid(1));
    }

    #[test]
    fn overlap_above_half_same_line() {
        // 字形 A y 80..90（高 10），B y 72..90（高 18）：重叠 [80,90] = 10 / min(10,18)=10 → 1.0 同行
        let glyphs = [g(0, 10.0, 80.0, 20.0, 90.0), g(1, 30.0, 72.0, 40.0, 90.0)];
        let lines = group_lines(&glyphs, &region());
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0].len(), 2);
    }

    #[test]
    fn overlap_below_half_new_line() {
        // A y 80..90（高 10），B y 70..76（高 6）：重叠 [80,76]=空？y 区间 [80,90] 与 [70,76] 交
        // [80,76] 无 → 0。构造一个正重叠但 < 50% 的：A y 70..90（高 20），B y 70..79（高 9）：
        // 重叠 [70,79] = 9 / 9 = 1.0 → 同行。换：A y 71..91（高20）, B y 70..80（高10）：重叠[71,80]=9/10=0.9 同行。
        // 要 < 0.5：A y 80..100（高20），B y 71..79（高8）：重叠 [80,79] 空 → 0。
        // 用高度差：A y 75..95（高20），B y 70..82（高12）：重叠 [75,82]=7/12≈0.58 → 同行。
        // A y 75..95（高20），B y 70..80（高10）：重叠 [75,80]=5/10=0.5 → 不大于 0.5 → 新行。
        let glyphs = [g(0, 10.0, 75.0, 20.0, 95.0), g(1, 30.0, 70.0, 40.0, 80.0)];
        let lines = group_lines(&glyphs, &region());
        assert_eq!(lines.len(), 2, "overlap exactly 0.5 must start a new line");
    }

    #[test]
    fn many_lines_top_down() {
        let mut glyphs = Vec::new();
        for row in 0..4 {
            let y = 80.0 - row as f32 * 20.0; // 80, 60, 40, 20
            for col in 0..3 {
                glyphs.push(g(
                    row * 3 + col,
                    10.0 + col as f32 * 30.0,
                    y,
                    20.0 + col as f32 * 30.0,
                    y + 10.0,
                ));
            }
        }
        let lines = group_lines(&glyphs, &region());
        assert_eq!(lines.len(), 4);
        for (i, line) in lines.iter().enumerate() {
            assert_eq!(line.len(), 3, "row {i}");
            // 每行 x 升序：ordinal 即 row*3+col
            assert_eq!(line[0], gid(i as u32 * 3));
            assert_eq!(line[2], gid(i as u32 * 3 + 2));
        }
    }

    #[test]
    fn region_filters_by_center() {
        // 字形中心在区域外（虽然 bbox 与区域相交）→ 排除
        let narrow = Rect::new(50.0, 50.0, 60.0, 60.0);
        let glyphs = [
            g(0, 0.0, 0.0, 100.0, 100.0), // 中心 (50,50) 在 narrow 内 → 保留
            g(1, 55.0, 0.0, 155.0, 5.0),  // 中心 (105,2.5) 区域外 → 排除
        ];
        let lines = group_lines(&glyphs, &narrow);
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0], vec![gid(0)]);
    }

    #[test]
    fn superscript_stays_with_line_when_overlapping() {
        // 上标：主体 y 80..90，上标 y 86..90（高 4）：重叠 [86,90]=4/4=1.0 → 同行
        let glyphs = [g(0, 10.0, 80.0, 20.0, 90.0), g(1, 22.0, 86.0, 26.0, 90.0)];
        let lines = group_lines(&glyphs, &region());
        assert_eq!(lines.len(), 1);
        assert_eq!(lines[0], vec![gid(0), gid(1)]);
    }

    #[test]
    fn vertical_overlap_ratio_math() {
        let a = Rect::new(0.0, 0.0, 10.0, 10.0);
        assert_eq!(vertical_overlap_ratio(&a, &a), 1.0);
        assert_eq!(
            vertical_overlap_ratio(&a, &Rect::new(0.0, 5.0, 10.0, 15.0)),
            0.5
        );
        assert_eq!(
            vertical_overlap_ratio(&a, &Rect::new(0.0, 10.0, 10.0, 20.0)),
            0.0
        );
        // 零高度
        assert_eq!(
            vertical_overlap_ratio(&a, &Rect::new(0.0, 5.0, 10.0, 5.0)),
            0.0
        );
    }
}
