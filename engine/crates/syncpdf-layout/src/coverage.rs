//! 覆盖率门禁：非白字形中心不在任何区域内 = 未覆盖。
//!
//! 门禁阈值（≤ 0.5%）由上游（pipeline）判定，本模块只产出数据。

use syncpdf_core::Rect;

/// 覆盖率报告。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CoverageReport {
    /// 字形总数（含白字形）。
    pub total_glyphs: u32,
    /// 未被任何区域覆盖的非白字形数。
    pub uncovered: u32,
    /// 白字形（空白）数，不计入覆盖统计。
    pub white_excluded: u32,
    /// `uncovered / total_non_white`；无非白字形时为 0。
    pub ratio: f32,
}

/// `is_white` 的判定由上游（pdf 解析阶段）给出：纯空格、不可见（Tr 3/7）、
/// 或填充色接近页面底色。
///
/// `glyph_boxes[i].0` 为字形外接框（PDF 用户空间），`regions` 为布局区域框。
/// 中心点判定：字形框中心落在任一区域框内即视为覆盖。
pub fn coverage(glyph_boxes: &[(Rect, bool /* is_white */)], regions: &[Rect]) -> CoverageReport {
    let mut total = 0u32;
    let mut white = 0u32;
    let mut uncovered = 0u32;
    for (bbox, is_white) in glyph_boxes {
        total += 1;
        if *is_white {
            white += 1;
            continue;
        }
        let c = bbox.center();
        let covered = regions
            .iter()
            .any(|r| c.x >= r.x0 && c.x <= r.x1 && c.y >= r.y0 && c.y <= r.y1);
        if !covered {
            uncovered += 1;
        }
    }
    let non_white = total - white;
    let ratio = if non_white > 0 {
        uncovered as f32 / non_white as f32
    } else {
        0.0
    };
    CoverageReport {
        total_glyphs: total,
        uncovered,
        white_excluded: white,
        ratio,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn g(x0: f32, y0: f32, x1: f32, y1: f32, white: bool) -> (Rect, bool) {
        (Rect::new(x0, y0, x1, y1), white)
    }

    #[test]
    fn empty_inputs() {
        let r = coverage(&[], &[]);
        assert_eq!(r.total_glyphs, 0);
        assert_eq!(r.uncovered, 0);
        assert_eq!(r.white_excluded, 0);
        assert_eq!(r.ratio, 0.0);
    }

    #[test]
    fn all_covered() {
        let regions = [Rect::new(0.0, 0.0, 100.0, 100.0)];
        let glyphs = [
            g(10.0, 10.0, 20.0, 20.0, false),
            g(80.0, 80.0, 90.0, 90.0, false),
        ];
        let r = coverage(&glyphs, &regions);
        assert_eq!(r.uncovered, 0);
        assert_eq!(r.ratio, 0.0);
        assert_eq!(r.total_glyphs, 2);
    }

    #[test]
    fn white_glyphs_excluded_from_denominator() {
        let regions = [Rect::new(0.0, 0.0, 100.0, 100.0)];
        let glyphs = [
            g(10.0, 10.0, 20.0, 20.0, false),    // 覆盖
            g(200.0, 200.0, 210.0, 210.0, true), // 白、区域外：不计
        ];
        let r = coverage(&glyphs, &regions);
        assert_eq!(r.total_glyphs, 2);
        assert_eq!(r.white_excluded, 1);
        assert_eq!(r.uncovered, 0);
        assert_eq!(r.ratio, 0.0);
    }

    #[test]
    fn uncovered_counted() {
        let regions = [Rect::new(0.0, 0.0, 100.0, 100.0)];
        let glyphs = [
            g(10.0, 10.0, 20.0, 20.0, false),   // 覆盖
            g(150.0, 10.0, 160.0, 20.0, false), // 未覆盖
            g(10.0, 150.0, 20.0, 160.0, false), // 未覆盖
        ];
        let r = coverage(&glyphs, &regions);
        assert_eq!(r.uncovered, 2);
        assert!((r.ratio - 2.0 / 3.0).abs() < 1e-6);
    }

    #[test]
    fn center_point_semantics() {
        // 字形框很大但中心在区域内 → 覆盖
        let regions = [Rect::new(50.0, 50.0, 60.0, 60.0)];
        let glyphs = [g(0.0, 0.0, 110.0, 110.0, false)]; // 中心 (55,55) 在区域内
        let r = coverage(&glyphs, &regions);
        assert_eq!(r.uncovered, 0);
        // 反过来：框小但中心在区域外 → 未覆盖
        let regions2 = [Rect::new(0.0, 0.0, 10.0, 10.0)];
        let glyphs2 = [g(20.0, 20.0, 30.0, 30.0, false)];
        let r2 = coverage(&glyphs2, &regions2);
        assert_eq!(r2.uncovered, 1);
    }

    #[test]
    fn boundary_is_inclusive() {
        let regions = [Rect::new(0.0, 0.0, 10.0, 10.0)];
        // 中心 (10,10) 恰在边界 → 覆盖（闭区间）
        let glyphs = [g(5.0, 5.0, 15.0, 15.0, false)];
        assert_eq!(coverage(&glyphs, &regions).uncovered, 0);
    }

    #[test]
    fn only_white_glyphs_means_zero_ratio() {
        let glyphs = [
            g(0.0, 0.0, 5.0, 5.0, true),
            g(100.0, 100.0, 105.0, 105.0, true),
        ];
        let r = coverage(&glyphs, &[]);
        assert_eq!(r.total_glyphs, 2);
        assert_eq!(r.white_excluded, 2);
        assert_eq!(r.uncovered, 0);
        assert_eq!(r.ratio, 0.0);
    }

    #[test]
    fn multiple_regions_union() {
        let regions = [
            Rect::new(0.0, 0.0, 50.0, 50.0),
            Rect::new(100.0, 100.0, 150.0, 150.0),
        ];
        let glyphs = [
            g(10.0, 10.0, 20.0, 20.0, false),     // 区域 1
            g(120.0, 120.0, 130.0, 130.0, false), // 区域 2
            g(60.0, 60.0, 70.0, 70.0, false),     // 都不在
        ];
        let r = coverage(&glyphs, &regions);
        assert_eq!(r.uncovered, 1);
        assert!((r.ratio - 1.0 / 3.0).abs() < 1e-6);
    }

    #[test]
    fn gate_threshold_semantics() {
        // 上游门禁 0.005：10 个非白字形允许最多 0 个未覆盖（0/10=0 < 0.005），
        // 1 个未覆盖时 0.1 > 0.005 触发门禁。这里只验证数值。
        let regions = [Rect::new(0.0, 0.0, 100.0, 100.0)];
        let mut glyphs: Vec<(Rect, bool)> = (0..10)
            .map(|i| g(i as f32, i as f32, i as f32 + 5.0, i as f32 + 5.0, false))
            .collect();
        let r0 = coverage(&glyphs, &regions);
        assert!(r0.ratio <= 0.005, "{}", r0.ratio);
        glyphs.push(g(500.0, 500.0, 510.0, 510.0, false));
        let r1 = coverage(&glyphs, &regions);
        assert!(r1.ratio > 0.005, "{}", r1.ratio);
        assert!((r1.ratio - 1.0 / 11.0).abs() < 1e-6);
    }
}
