//! 几何与坐标系。引擎内部一律使用 PDF 用户空间（左下原点，y 向上，单位 pt）。

use serde::{Deserialize, Serialize};

/// 坐标系标注。引擎输出只标注，不转换；前端是唯一换算点（规约 #6）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum CoordSystem {
    /// PDF 用户空间：左下原点，y 向上，pt。
    #[default]
    PdfUser,
    /// 位图像素：左上原点，y 向下，未旋转。用于布局模型输入输出。
    ImageTopLeft,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, Default)]
pub struct Point {
    pub x: f32,
    pub y: f32,
}

impl Point {
    pub const fn new(x: f32, y: f32) -> Self {
        Self { x, y }
    }
}

/// 轴对齐矩形，`x0 <= x1`，`y0 <= y1`。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, Default)]
pub struct Rect {
    pub x0: f32,
    pub y0: f32,
    pub x1: f32,
    pub y1: f32,
}

impl Rect {
    /// 构造并归一化角点顺序。
    pub fn new(x0: f32, y0: f32, x1: f32, y1: f32) -> Self {
        Self {
            x0: x0.min(x1),
            y0: y0.min(y1),
            x1: x0.max(x1),
            y1: y0.max(y1),
        }
    }

    pub fn from_xywh(x: f32, y: f32, w: f32, h: f32) -> Self {
        Self::new(x, y, x + w, y + h)
    }

    pub fn width(&self) -> f32 {
        self.x1 - self.x0
    }

    pub fn height(&self) -> f32 {
        self.y1 - self.y0
    }

    pub fn area(&self) -> f32 {
        self.width() * self.height()
    }

    pub fn is_empty(&self) -> bool {
        self.width() <= 0.0 || self.height() <= 0.0
    }

    pub fn center(&self) -> Point {
        Point::new((self.x0 + self.x1) * 0.5, (self.y0 + self.y1) * 0.5)
    }

    pub fn contains(&self, p: Point) -> bool {
        p.x >= self.x0 && p.x <= self.x1 && p.y >= self.y0 && p.y <= self.y1
    }

    pub fn intersects(&self, o: &Rect) -> bool {
        self.x0 < o.x1 && o.x0 < self.x1 && self.y0 < o.y1 && o.y0 < self.y1
    }

    /// 交集；不相交返回 `None`。
    pub fn intersection(&self, o: &Rect) -> Option<Rect> {
        let r = Rect {
            x0: self.x0.max(o.x0),
            y0: self.y0.max(o.y0),
            x1: self.x1.min(o.x1),
            y1: self.y1.min(o.y1),
        };
        (!r.is_empty()).then_some(r)
    }

    pub fn union(&self, o: &Rect) -> Rect {
        Rect {
            x0: self.x0.min(o.x0),
            y0: self.y0.min(o.y0),
            x1: self.x1.max(o.x1),
            y1: self.y1.max(o.y1),
        }
    }

    /// 向外扩 `d`（负值为收缩）。
    pub fn inflate(&self, d: f32) -> Rect {
        Rect::new(self.x0 - d, self.y0 - d, self.x1 + d, self.y1 + d)
    }

    /// 变换后的外接矩形。
    pub fn transform(&self, m: &Matrix) -> Rect {
        let pts = [
            m.apply(Point::new(self.x0, self.y0)),
            m.apply(Point::new(self.x1, self.y0)),
            m.apply(Point::new(self.x0, self.y1)),
            m.apply(Point::new(self.x1, self.y1)),
        ];
        let mut r = Rect {
            x0: pts[0].x,
            y0: pts[0].y,
            x1: pts[0].x,
            y1: pts[0].y,
        };
        for p in &pts[1..] {
            r.x0 = r.x0.min(p.x);
            r.y0 = r.y0.min(p.y);
            r.x1 = r.x1.max(p.x);
            r.y1 = r.y1.max(p.y);
        }
        r
    }
}

/// PDF 仿射矩阵 `[a b c d e f]`：`x' = a·x + c·y + e`，`y' = b·x + d·y + f`。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Matrix {
    pub a: f32,
    pub b: f32,
    pub c: f32,
    pub d: f32,
    pub e: f32,
    pub f: f32,
}

impl Default for Matrix {
    fn default() -> Self {
        Self::IDENTITY
    }
}

impl Matrix {
    pub const IDENTITY: Matrix = Matrix {
        a: 1.0,
        b: 0.0,
        c: 0.0,
        d: 1.0,
        e: 0.0,
        f: 0.0,
    };

    pub const fn new(a: f32, b: f32, c: f32, d: f32, e: f32, f: f32) -> Self {
        Self { a, b, c, d, e, f }
    }

    pub const fn translate(tx: f32, ty: f32) -> Self {
        Self::new(1.0, 0.0, 0.0, 1.0, tx, ty)
    }

    pub const fn scale(sx: f32, sy: f32) -> Self {
        Self::new(sx, 0.0, 0.0, sy, 0.0, 0.0)
    }

    pub fn as_array(&self) -> [f32; 6] {
        [self.a, self.b, self.c, self.d, self.e, self.f]
    }

    pub fn is_identity(&self) -> bool {
        *self == Self::IDENTITY
    }

    /// `self` 先作用，再 `other`（PDF `cm` 语义：`new = self × other`）。
    pub fn then(&self, other: &Matrix) -> Matrix {
        Matrix {
            a: self.a * other.a + self.b * other.c,
            b: self.a * other.b + self.b * other.d,
            c: self.c * other.a + self.d * other.c,
            d: self.c * other.b + self.d * other.d,
            e: self.e * other.a + self.f * other.c + other.e,
            f: self.e * other.b + self.f * other.d + other.f,
        }
    }

    pub fn apply(&self, p: Point) -> Point {
        Point::new(
            self.a * p.x + self.c * p.y + self.e,
            self.b * p.x + self.d * p.y + self.f,
        )
    }

    /// 只变换向量（忽略平移）。
    pub fn apply_vector(&self, p: Point) -> Point {
        Point::new(self.a * p.x + self.c * p.y, self.b * p.x + self.d * p.y)
    }

    pub fn determinant(&self) -> f32 {
        self.a * self.d - self.b * self.c
    }

    pub fn invert(&self) -> Option<Matrix> {
        let det = self.determinant();
        if det.abs() < 1e-12 {
            return None;
        }
        let inv = 1.0 / det;
        Some(Matrix {
            a: self.d * inv,
            b: -self.b * inv,
            c: -self.c * inv,
            d: self.a * inv,
            e: (self.c * self.f - self.d * self.e) * inv,
            f: (self.b * self.e - self.a * self.f) * inv,
        })
    }

    /// x 方向缩放量（用于估算字号）。
    pub fn x_scale(&self) -> f32 {
        (self.a * self.a + self.b * self.b).sqrt()
    }

    pub fn y_scale(&self) -> f32 {
        (self.c * self.c + self.d * self.d).sqrt()
    }
}

impl From<[f32; 6]> for Matrix {
    fn from(m: [f32; 6]) -> Self {
        Self::new(m[0], m[1], m[2], m[3], m[4], m[5])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn approx(a: f32, b: f32) -> bool {
        (a - b).abs() < 1e-4
    }

    #[test]
    fn rect_normalizes_and_measures() {
        let r = Rect::new(10.0, 20.0, 0.0, 5.0);
        assert_eq!(
            r,
            Rect {
                x0: 0.0,
                y0: 5.0,
                x1: 10.0,
                y1: 20.0
            }
        );
        assert!(approx(r.width(), 10.0) && approx(r.height(), 15.0));
        assert!(r.contains(Point::new(5.0, 10.0)));
        assert!(!r.contains(Point::new(11.0, 10.0)));
    }

    #[test]
    fn rect_intersection_union() {
        let a = Rect::new(0.0, 0.0, 10.0, 10.0);
        let b = Rect::new(5.0, 5.0, 15.0, 15.0);
        assert_eq!(a.intersection(&b), Some(Rect::new(5.0, 5.0, 10.0, 10.0)));
        assert_eq!(a.union(&b), Rect::new(0.0, 0.0, 15.0, 15.0));
        let c = Rect::new(20.0, 20.0, 30.0, 30.0);
        assert_eq!(a.intersection(&c), None);
        assert!(!a.intersects(&c));
    }

    #[test]
    fn matrix_then_matches_pdf_cm_order() {
        // 先缩放 2，再平移 (10, 0)：点 (1,1) → (2,2) → (12,2)
        let m = Matrix::scale(2.0, 2.0).then(&Matrix::translate(10.0, 0.0));
        let p = m.apply(Point::new(1.0, 1.0));
        assert!(approx(p.x, 12.0) && approx(p.y, 2.0));
        // 反过来：先平移再缩放：(1,1) → (11,1) → (22,2)
        let m2 = Matrix::translate(10.0, 0.0).then(&Matrix::scale(2.0, 2.0));
        let p2 = m2.apply(Point::new(1.0, 1.0));
        assert!(approx(p2.x, 22.0) && approx(p2.y, 2.0));
    }

    #[test]
    fn matrix_invert_roundtrip() {
        let m = Matrix::new(2.0, 0.5, -0.3, 1.5, 7.0, -3.0);
        let inv = m.invert().unwrap();
        let id = m.then(&inv);
        for (x, y) in [
            (id.a, 1.0),
            (id.b, 0.0),
            (id.c, 0.0),
            (id.d, 1.0),
            (id.e, 0.0),
            (id.f, 0.0),
        ] {
            assert!(approx(x, y), "{id:?}");
        }
        assert!(Matrix::scale(0.0, 1.0).invert().is_none());
    }

    #[test]
    fn rect_transform_is_bounding_box() {
        let r = Rect::new(0.0, 0.0, 2.0, 1.0);
        // 旋转 90°：(x,y) → (-y, x)
        let rot = Matrix::new(0.0, 1.0, -1.0, 0.0, 0.0, 0.0);
        let t = r.transform(&rot);
        assert!(approx(t.x0, -1.0) && approx(t.x1, 0.0) && approx(t.y0, 0.0) && approx(t.y1, 2.0));
    }

    #[test]
    fn coord_system_serde_snake_case() {
        assert_eq!(
            serde_json::to_string(&CoordSystem::PdfUser).unwrap(),
            "\"pdf_user\""
        );
        assert_eq!(
            serde_json::to_string(&CoordSystem::ImageTopLeft).unwrap(),
            "\"image_top_left\""
        );
    }
}
