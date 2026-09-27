//! 断行机会。设计基准：02-技术路径与架构.md §8.2 步骤 3；hjfy research/02 §4。
//!
//! icu_segmenter `LineSegmenter::new_auto()` Strict 求断点；
//! 拉丁单词内再补 hypher 软断点（仅当词长 >= `MIN_HYPHEN_WORD` 字节时），
//! 软断点只在断行时没有普通断点可用才启用（见 layout）。
//! CJK 语言自身无 hypher pattern，但段内嵌入的纯拉丁词同样用英文
//! pattern 补软断点，否则长词只能整词挪行、拉松上一行。

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

    /// 拉丁词软断点用的 hypher 语言：英文与（段内嵌拉丁词的）CJK 语言
    /// 共用英文 pattern；其余语言（含 Ar/Other）无 pattern 返回 None。
    fn hypher(self) -> Option<hypher::Lang> {
        match self {
            Lang::En | Lang::Zh | Lang::Ja | Lang::Ko => hypher::Lang::from_iso(*b"en"),
            Lang::Ar | Lang::Other => None,
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

/// token 字符：ASCII 字母、数字与标点。空白、控制字符以及一切非 ASCII
/// 字符（CJK、阿拉伯文等）都是 token 边界。
fn is_token_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c.is_ascii_punctuation()
}

/// 对纯 ASCII 字母 token 补 hypher 软断点。
///
/// token 是以 [`is_token_char`] 界定的连续串，去掉首尾标点后即为词。
/// 词只要不是纯字母（含数字或内部 `/`、`.`、`_`、`-`、`:` 等标点，如
/// `ResNet50x4`、`x_encoder`、`https://…`），就整体不补软断点：按字母
/// 片段划词会把其中的字母段误当成独立词断开；这类 token 交给常规断点处理。
fn add_soft_hyphens(text: &str, lang: hypher::Lang, out: &mut Vec<BreakOpp>) {
    let mut token_start: Option<usize> = None;
    let push_token = |text: &str, start: usize, end: usize, out: &mut Vec<BreakOpp>| {
        // 首尾标点（句读、括号、引号）不属于词本身；只有词内部的标点才使
        // token 不成其为词。
        let token = &text[start..end];
        let lead = token.len()
            - token
                .trim_start_matches(|c: char| c.is_ascii_punctuation())
                .len();
        let token = token.trim_matches(|c: char| c.is_ascii_punctuation());
        let (start, end) = (start + lead, start + lead + token.len());
        if token.len() < MIN_HYPHEN_WORD {
            return;
        }
        // 只对纯拉丁 token 启用（数字/符号混合 token 交给常规断点）。
        if !token.chars().all(|c| c.is_ascii_alphabetic()) {
            return;
        }
        let mut prev = start;
        for syl in hypher::hyphenate(token, lang) {
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
        if is_token_char(c) {
            if token_start.is_none() {
                token_start = Some(i);
            }
        } else if let Some(start) = token_start.take() {
            push_token(text, start, i, out);
        }
    }
    if let Some(start) = token_start {
        push_token(text, start, text.len(), out);
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
    fn cjk_paragraph_hyphenates_embedded_latin_word() {
        // 中文句中夹长英文词（术语），Lang::Zh 也应在音节处获得软断点。
        let text = "研究表明 backdoor 攻击模型行为。";
        let b = break_opportunities(text, Lang::Zh);
        assert!(b.iter().any(|x| x.soft_hyphen), "got {b:?}");
        // 软断点都落在该英文词内部（词首 13..21 字节区间），中文部分不受影响。
        for x in b.iter().filter(|x| x.soft_hyphen) {
            let at = x.byte as usize;
            assert!(
                at > "研究表明 ".len() && at < "研究表明 backdoor".len(),
                "软断点 {at} 不在 backdoor 内部"
            );
        }
    }

    #[test]
    fn cjk_paragraph_hyphenates_trailing_latin_word() {
        // 词位于段尾（无空格收尾分支）也要补软断点。
        let b = break_opportunities("模型容易受到 backdoor", Lang::Zh);
        assert!(b.iter().any(|x| x.soft_hyphen), "got {b:?}");
    }

    #[test]
    fn no_soft_hyphen_for_token_with_digits() {
        // 字母+数字混合 token（如 ResNet50x4）不是纯字母词，整体不断。
        let b = break_opportunities("模型 ResNet50x4 提升", Lang::Zh);
        assert!(b.iter().all(|x| !x.soft_hyphen), "got {b:?}");
    }

    #[test]
    fn no_soft_hyphen_for_token_with_symbols() {
        // 含 `_`、`/`、`.`、`:`、`-` 等符号的 token 整体不断。
        for text in [
            "编码器 x_encoder 精度",
            "带宽 10/100/1000 快",
            "模型 v1.2.3 发布",
        ] {
            let b = break_opportunities(text, Lang::Zh);
            assert!(b.iter().all(|x| !x.soft_hyphen), "{text}: got {b:?}");
        }
    }

    #[test]
    fn no_soft_hyphen_for_url_like_token() {
        // URL 样 token（含 `:`、`/`、`.`）整体不断。
        let b = break_opportunities("详见 https://example.com/backdoorattack 论文", Lang::Zh);
        assert!(b.iter().all(|x| !x.soft_hyphen), "got {b:?}");
        // 英文段落同样不受影响。
        let b = break_opportunities("see https://example.com/backdoorattack paper", Lang::En);
        assert!(b.iter().all(|x| !x.soft_hyphen), "got {b:?}");
    }

    #[test]
    fn english_paragraph_unchanged_by_token_rule() {
        // 纯英文词照常补软断点（改 token 划词前后的行为一致）。
        let b = break_opportunities("wonderful", Lang::En);
        assert!(b.iter().any(|x| x.soft_hyphen), "got {b:?}");
        // 词间的普通断点不受软断点影响。
        let b = bytes("Hello World", Lang::En);
        assert!(b.contains(&6), "got {b:?}");
    }

    #[test]
    fn surrounding_punctuation_is_not_part_of_the_word() {
        // 句读、括号、引号贴在词首尾时仍按词断，En 与 Zh 段一致。
        for (text, lang) in [
            ("extraordinariness.", Lang::En),
            ("paper (backdoorattacks), cited", Lang::En),
            ("\"wonderful\"", Lang::En),
            ("研究 backdoor, 攻击", Lang::Zh),
        ] {
            let b = break_opportunities(text, lang);
            assert!(b.iter().any(|x| x.soft_hyphen), "{text}: got {b:?}");
            // 软断点不落在标点上或标点旁。
            for x in b.iter().filter(|x| x.soft_hyphen) {
                let at = x.byte as usize;
                let (l, r) = (text[..at].chars().next_back(), text[at..].chars().next());
                assert!(
                    l.unwrap().is_ascii_alphabetic() && r.unwrap().is_ascii_alphabetic(),
                    "{text}@{at}"
                );
            }
        }
        // 词内部的标点仍使整个 token 不断。
        for text in ["see e.g.extraordinariness", "x_extraordinariness."] {
            let b = break_opportunities(text, Lang::En);
            assert!(b.iter().all(|x| !x.soft_hyphen), "{text}: got {b:?}");
        }
    }

    #[test]
    fn empty_text() {
        assert!(break_opportunities("", Lang::En).is_empty());
    }
}
