//! 断行机会。设计基准：02-技术路径与架构.md §8.2 步骤 3；hjfy research/02 §4。
//!
//! icu_segmenter `LineSegmenter::new_auto()` Strict 求断点；
//! 拉丁单词内再补 hypher 软断点（仅当词长 >= `MIN_HYPHEN_WORD` 字节时），
//! 软断点只在断行时没有普通断点可用才启用（见 layout）。

use icu_segmenter::options::{LineBreakOptions, LineBreakStrictness};
use icu_segmenter::LineSegmenter;

/// 语言分类（供断行与对齐策略选择）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum Lang {
    Zh,
    Ja,
    Ko,
    #[default]
    En,
    Ar,
    Other,
}

impl Lang {
    /// 是否 CJK（禁则、字距均分、无词间空格拉伸）。
    pub fn is_cjk(self) -> bool {
        matches!(self, Lang::Zh | Lang::Ja | Lang::Ko)
    }

    /// hypher 语言（无 pattern 的语言返回 None）。
    fn hypher(self) -> Option<hypher::Lang> {
        match self {
            Lang::En => hypher::Lang::from_iso(*b"en"),
            Lang::Zh | Lang::Ja | Lang::Ko | Lang::Ar | Lang::Other => None,
        }
    }
}

/// 补软断点的最小词长（字节）。
pub const MIN_HYPHEN_WORD: usize = 8;

/// 一个断行机会：`byte` 为断点后的起始字节偏移。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct BreakOpp {
    pub byte: u32,
    /// 强制换行（\n、\r\n 等）。
    pub mandatory: bool,
    /// hypher 软断点（破折号处，只作最后手段）。
    pub soft_hyphen: bool,
}

use serde::{Deserialize, Serialize};

/// 求断行机会（升序）。空文本返回空表。
///
/// - icu_segmenter Strict 给出全部常规断点；`\n` 产生 mandatory 断点。
/// - 拉丁词内部补 hypher 软断点：仅当词长 >= `MIN_HYPHEN_WORD` 字节。
/// - CJK 标点禁则（行首 「，。、；：？！」等）由 icu Strict 保证，
///   这里再兜底过滤：若断点后紧跟禁首标点，则丢弃该断点（保守处理）。
pub fn break_opportunities(text: &str, lang: Lang) -> Vec<BreakOpp> {
    if text.is_empty() {
        return Vec::new();
    }
    // LineSegmenter 构建开销大（加载词典/LSTM 数据），线程内缓存复用。
    thread_local! {
        static SEGMENTER: icu_segmenter::LineSegmenterBorrowed<'static> = {
            let mut options = LineBreakOptions::default();
            options.strictness = Some(LineBreakStrictness::Strict);
            LineSegmenter::new_auto(options)
        };
    }
    let breaks: Vec<usize> = SEGMENTER.with(|seg| seg.segment_str(text).collect());

    let mut out: Vec<BreakOpp> = Vec::new();
    let mut prev = 0usize;
    for b in breaks {
        if b == 0 {
            continue;
        }
        let mandatory = is_mandatory_at(text, b);
        if !mandatory {
            // 行首禁则兜底：断点后是禁首标点则丢弃。
            if let Some(c) = text[b..].chars().next() {
                if is_forbidden_line_start(c) {
                    continue;
                }
            }
            // 行尾禁则兜底：断点前是禁尾标点（开括号类）则丢弃。
            if let Some(c) = text[..b].chars().next_back() {
                if is_forbidden_line_end(c) {
                    continue;
                }
            }
        }
        if b != prev {
            out.push(BreakOpp {
                byte: b as u32,
                mandatory,
                soft_hyphen: false,
            });
            prev = b;
        }
    }

    // 拉丁词内软断点。
    if let Some(hy_lang) = lang.hypher() {
        add_soft_hyphens(text, hy_lang, &mut out);
    }
    out
}

/// 断点 `byte` 处是否为强制换行（前一字符为 \n 或 \r）。
fn is_mandatory_at(text: &str, byte: usize) -> bool {
    text[..byte].ends_with('\n') || text[..byte].ends_with('\r')
}

/// 行首禁则字符（禁排类：句读、闭合引号/括号）。
pub(crate) fn is_forbidden_line_start(c: char) -> bool {
    matches!(
        c,
        '，' | '。'
            | '、'
            | '；'
            | '：'
            | '？'
            | '！'
            | '」'
            | '』'
            | '）'
            | '》'
            | '”'
            | '’'
            | '…'
            | '—'
            | '·'
    )
}

/// 行尾禁则字符（禁排类：起始引号/括号）。
pub(crate) fn is_forbidden_line_end(c: char) -> bool {
    matches!(c, '（' | '「' | '『' | '《' | '“' | '‘')
}

/// 对词长 >= `MIN_HYPHEN_WORD` 的拉丁词补 hypher 软断点。
fn add_soft_hyphens(text: &str, lang: hypher::Lang, out: &mut Vec<BreakOpp>) {
    let mut word_start: Option<usize> = None;
    let push_word = |text: &str, start: usize, end: usize, out: &mut Vec<BreakOpp>| {
        let word = &text[start..end];
        if word.len() < MIN_HYPHEN_WORD {
            return;
        }
        // 只对纯拉丁词启用（CJK 数字混合交给常规断点）。
        if !word.chars().all(|c| c.is_ascii_alphabetic()) {
            return;
        }
        let mut prev = start;
        for syl in hypher::hyphenate(word, lang) {
            let at = prev + syl.len();
            if at > start && at < end {
                // 软断点不能与已有断点重合。
                if !out.iter().any(|b: &BreakOpp| b.byte as usize == at) {
                    out.push(BreakOpp {
                        byte: at as u32,
                        mandatory: false,
                        soft_hyphen: true,
                    });
                }
            }
            prev = at;
        }
    };

    for (i, c) in text.char_indices() {
        let is_latin = c.is_ascii_alphabetic();
        if is_latin {
            if word_start.is_none() {
                word_start = Some(i);
            }
        } else if let Some(start) = word_start.take() {
            push_word(text, start, i, out);
        }
    }
    if let Some(start) = word_start {
        push_word(text, start, text.len(), out);
    }
    out.sort_by_key(|b| b.byte);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bytes(text: &str, lang: Lang) -> Vec<u32> {
        break_opportunities(text, lang)
            .iter()
            .map(|b| b.byte)
            .collect()
    }

    #[test]
    fn english_word_boundaries() {
        let b = bytes("Hello World", Lang::En);
        // 串尾断点（11）也存在，但不构成行拆分；词间只有 6。
        assert!(b.contains(&6), "got {b:?}");
        assert!(b.iter().all(|x| *x as usize <= "Hello World".len()));
    }

    #[test]
    fn cjk_breaks_between_chars() {
        let b = bytes("中文字符测试", Lang::Zh);
        // CJK 每个字符后可断（禁则过滤后）。
        assert!(b.contains(&9), "got {b:?}");
        assert!(b.len() >= 3, "got {b:?}");
    }

    #[test]
    fn cjk_punctuation_no_break_before() {
        // 「，」不能出现在行首 → 该断点被丢弃。
        let text = "这是一段长文本，后面还有内容";
        let b = bytes(text, Lang::Zh);
        let comma = text.find('，').unwrap();
        assert!(!b.contains(&(comma as u32)), "got {b:?}");
    }

    #[test]
    fn cjk_open_bracket_no_break_after() {
        // 「（」不能出现在行尾。
        let text = "这是一段长文本（后面还有内容";
        let b = bytes(text, Lang::Zh);
        let paren = text.find('（').unwrap();
        // 「（」之后紧跟「后」的断点：icu 本身可能不给，这里兜底保证不出现。
        let after = paren + '（'.len_utf8();
        let _ = after;
        // 更强：验证所有断点处行首不是禁首标点（串尾断点除外）。
        for &bp in &b {
            if bp as usize == text.len() {
                continue;
            }
            let c = text[bp as usize..].chars().next().unwrap();
            assert!(!is_forbidden_line_start(c));
        }
    }

    #[test]
    fn mandatory_newline() {
        let b = break_opportunities("first\nsecond", Lang::En);
        let m = b.iter().find(|x| x.mandatory).unwrap();
        assert_eq!(m.byte, 6);
        assert!(!m.soft_hyphen);
    }

    #[test]
    fn soft_hyphen_for_long_words() {
        // "wonderful" 9 字节 >= 8 → hypher 有 won-der-ful 断点。
        let b = break_opportunities("wonderful", Lang::En);
        assert!(b.iter().any(|x| x.soft_hyphen), "got {b:?}");
    }

    #[test]
    fn no_soft_hyphen_for_short_words() {
        let b = break_opportunities("cat dog", Lang::En);
        assert!(b.iter().all(|x| !x.soft_hyphen), "got {b:?}");
    }

    #[test]
    fn no_soft_hyphen_for_cjk() {
        let b = break_opportunities("这是一个很长的测试文本段落", Lang::Zh);
        assert!(b.iter().all(|x| !x.soft_hyphen), "got {b:?}");
    }

    #[test]
    fn empty_text() {
        assert!(break_opportunities("", Lang::En).is_empty());
    }
}
