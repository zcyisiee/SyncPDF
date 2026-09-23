//! 塑形抽象。设计基准：02-技术路径与架构.md §8；typeset.md。
//!
//! 本 crate 不依赖 `syncpdf-font`：字体 crate 与本 crate 并行开发，
//! 这里只定义最小塑形接口，pipeline 阶段再用真实字体适配器实现。
//! 测试与基准使用 [`MonoShaper`]（等宽假字形）。

use serde::{Deserialize, Serialize};

/// 塑形后的单个字形。坐标与步长均以 pt 计（相对给定字号），
/// `cluster` 为该字形所属 cluster 在被塑形文本中的起始字节偏移。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ShapedGlyph {
    pub gid: u16,
    pub cluster: u32,
    pub x_advance: f32,
    pub x_offset: f32,
    pub y_offset: f32,
}

/// 字体度量，以 1pt 字号为单位的比例（ascent 向上为正，descent 向下为正）。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct FontMetrics {
    pub ascent: f32,
    pub descent: f32,
}

/// 样式描述：从段落 `styles` 表查得，用于挑选字体。
#[derive(Debug, Clone, Copy, PartialEq, Default, Serialize, Deserialize)]
pub struct StyleSpec {
    pub bold: bool,
    pub italic: bool,
    pub mono: bool,
    pub script: bool,
    #[serde(default)]
    pub serif: bool,
    /// Exact requested run size; None inherits the paragraph size.
    #[serde(default)]
    pub size: Option<f32>,
    #[serde(default)]
    pub color: Option<syncpdf_core::Color>,
    /// Explicit font override, used by backend edits after validation.
    #[serde(default)]
    pub font: Option<u32>,
}

/// 塑形器抽象：真实实现由字体 crate 适配（harfrust）。
pub trait Shaper {
    /// 塑形一段纯文本。`size` 为字号（pt）；`rtl` 提示段落方向。
    /// 返回按视觉顺序排列的字形（cluster 值为文本内字节偏移）。
    fn shape(&self, font: u32, text: &str, size: f32, rtl: bool) -> Vec<ShapedGlyph>;

    /// 字体度量（1pt 字号下的比例）。
    fn metrics(&self, font: u32) -> FontMetrics;

    /// 为样式挑选字体句柄。
    fn font_for(&self, style: &StyleSpec) -> u32;
}

/// 判断字符是否属于 CJK 表意文字/注音/全角区段（用于宽度与禁则判断）。
pub(crate) fn is_cjk_char(c: char) -> bool {
    matches!(
        c as u32,
        0x1100..=0x11FF
            | 0x2E80..=0x9FFF
            | 0xA960..=0xA97F
            | 0xAC00..=0xD7FF
            | 0xF900..=0xFAFF
            | 0xFE30..=0xFE4F
            | 0xFF00..=0xFF60
            | 0xFFE0..=0xFFE6
            | 0x20000..=0x2FFFD
            | 0x30000..=0x3FFFD
    )
}

/// 测试用等宽塑形器：拉丁 0.5em、CJK 1.0em、空格 0.25em。
///
/// gid 取 Unicode 码位低 16 位，保证可复现；度量 ascent 0.8 / descent 0.2。
#[derive(Debug, Clone, Copy, Default)]
pub struct MonoShaper;

impl MonoShaper {
    /// 单字符宽度（em 倍数）。
    fn em(c: char) -> f32 {
        if c == ' ' {
            0.25
        } else if is_cjk_char(c) {
            1.0
        } else {
            0.5
        }
    }

    /// MonoShaper 的字体句柄表：0=正文，1=粗，2=斜，3=粗斜，4=等宽，5=脚本。
    pub fn font_of(style: &StyleSpec) -> u32 {
        if style.mono {
            4
        } else if style.script {
            5
        } else {
            match (style.bold, style.italic) {
                (false, false) => 0,
                (true, false) => 1,
                (false, true) => 2,
                (true, true) => 3,
            }
        }
    }
}

impl Shaper for MonoShaper {
    fn shape(&self, _font: u32, text: &str, size: f32, _rtl: bool) -> Vec<ShapedGlyph> {
        let mut out = Vec::with_capacity(text.len());
        for (byte, c) in text.char_indices() {
            out.push(ShapedGlyph {
                gid: (c as u32 & 0xFFFF) as u16,
                cluster: byte as u32,
                x_advance: Self::em(c) * size,
                x_offset: 0.0,
                y_offset: 0.0,
            });
        }
        out
    }

    fn metrics(&self, _font: u32) -> FontMetrics {
        FontMetrics {
            ascent: 0.8,
            descent: 0.2,
        }
    }

    fn font_for(&self, style: &StyleSpec) -> u32 {
        Self::font_of(style)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mono_widths() {
        let s = MonoShaper;
        let g = s.shape(0, "A 中", 10.0, false);
        assert_eq!(g.len(), 3);
        assert!((g[0].x_advance - 5.0).abs() < 1e-6);
        assert!((g[1].x_advance - 2.5).abs() < 1e-6);
        assert!((g[2].x_advance - 10.0).abs() < 1e-6);
        assert_eq!(g[2].cluster, 2);
    }

    #[test]
    fn mono_font_for_styles() {
        let s = MonoShaper;
        assert_eq!(s.font_for(&StyleSpec::default()), 0);
        assert_eq!(
            s.font_for(&StyleSpec {
                bold: true,
                ..Default::default()
            }),
            1
        );
        assert_eq!(
            s.font_for(&StyleSpec {
                italic: true,
                ..Default::default()
            }),
            2
        );
        assert_eq!(
            s.font_for(&StyleSpec {
                bold: true,
                italic: true,
                ..Default::default()
            }),
            3
        );
        assert_eq!(
            s.font_for(&StyleSpec {
                mono: true,
                ..Default::default()
            }),
            4
        );
        assert_eq!(
            s.font_for(&StyleSpec {
                script: true,
                ..Default::default()
            }),
            5
        );
        assert_eq!(s.metrics(0).ascent, 0.8);
        assert_eq!(s.metrics(0).descent, 0.2);
    }

    #[test]
    fn cjk_detection() {
        assert!(is_cjk_char('中'));
        assert!(is_cjk_char('，'));
        assert!(is_cjk_char('あ'));
        assert!(is_cjk_char('한'));
        assert!(!is_cjk_char('A'));
        assert!(!is_cjk_char(' '));
        assert!(!is_cjk_char('\u{FFFC}'));
    }
}
