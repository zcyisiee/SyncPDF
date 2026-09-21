//! 颜色。引擎内部统一为 sRGB 0..1，回写时输出 `rg`/`RG`。

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Color {
    pub r: f32,
    pub g: f32,
    pub b: f32,
}

impl Color {
    pub const BLACK: Color = Color {
        r: 0.0,
        g: 0.0,
        b: 0.0,
    };
    pub const WHITE: Color = Color {
        r: 1.0,
        g: 1.0,
        b: 1.0,
    };

    pub fn rgb(r: f32, g: f32, b: f32) -> Self {
        Self {
            r: r.clamp(0.0, 1.0),
            g: g.clamp(0.0, 1.0),
            b: b.clamp(0.0, 1.0),
        }
    }

    pub fn gray(v: f32) -> Self {
        Self::rgb(v, v, v)
    }

    /// PDF DeviceCMYK → RGB 的简单换算（与 pdfium 默认一致）。
    pub fn cmyk(c: f32, m: f32, y: f32, k: f32) -> Self {
        Self::rgb(
            (1.0 - c) * (1.0 - k),
            (1.0 - m) * (1.0 - k),
            (1.0 - y) * (1.0 - k),
        )
    }

    pub fn from_rgb8(r: u8, g: u8, b: u8) -> Self {
        Self::rgb(r as f32 / 255.0, g as f32 / 255.0, b as f32 / 255.0)
    }

    pub fn to_rgb8(&self) -> [u8; 3] {
        [
            (self.r * 255.0).round() as u8,
            (self.g * 255.0).round() as u8,
            (self.b * 255.0).round() as u8,
        ]
    }

    /// 感知亮度 0..1；用于判断「纯白」字形与遮挡矩形底色。
    pub fn luminance(&self) -> f32 {
        0.2126 * self.r + 0.7152 * self.g + 0.0722 * self.b
    }

    pub fn is_near_white(&self) -> bool {
        self.luminance() > 0.98
    }
}

impl Default for Color {
    fn default() -> Self {
        Self::BLACK
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn conversions() {
        assert_eq!(Color::from_rgb8(255, 0, 128).to_rgb8(), [255, 0, 128]);
        assert_eq!(Color::cmyk(0.0, 0.0, 0.0, 1.0), Color::BLACK);
        assert!(Color::WHITE.is_near_white());
        assert!(!Color::gray(0.9).is_near_white());
        assert_eq!(
            Color::rgb(2.0, -1.0, 0.5),
            Color {
                r: 1.0,
                g: 0.0,
                b: 0.5
            }
        );
    }
}
