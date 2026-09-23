//! 集成测试（MonoShaper）。覆盖 typeset.md 列出的全部测试点。

use syncpdf_core::ir::Align;
use syncpdf_core::{AtomId, Color, ParagraphId, Rect, StyleId};
use syncpdf_typeset::shaper::{MonoShaper, StyleSpec};
use syncpdf_typeset::{
    break_opportunities, FitOptions, Inline, Lang, Obstacles, ParagraphSpec, Typeset,
};

fn pid() -> ParagraphId {
    "P01-001".parse().unwrap()
}

fn spec(bbox: Rect, font_size: f32, line_height: f32, align: Align) -> ParagraphSpec {
    ParagraphSpec {
        bbox,
        font_size,
        line_height,
        align,
        first_indent: 0.0,
        is_rtl: false,
        color: Color::BLACK,
        styles: vec![(StyleId(1), StyleSpec::default())],
        lang: Lang::En,
        first_baseline: None,
    }
}

fn text_inline(s: &str) -> Inline {
    Inline::Text {
        text: s.to_string(),
        style: StyleId(1),
    }
}

/// 全局 MonoShaper（ZST），供 'static 借用。
static MONO: MonoShaper = MonoShaper;

fn typeset_default() -> Typeset<'static> {
    Typeset::new(&MONO, FitOptions::default())
}

#[test]
fn english_exact_fit_scale_1() {
    // 框宽 70pt：MonoShaper 拉丁 0.5em，字号 10 → 每字符 5pt、空格 2.5pt。
    // "aaaa bbbb" = 45pt 可断（空格后），" cccc" 超宽 → 换行 → 2 行。
    let bbox = Rect::new(0.0, 0.0, 70.0, 30.0);
    let s = spec(bbox, 10.0, 1.2, Align::Left);
    let t = typeset_default();
    let r = t.layout(
        pid(),
        &s,
        &[text_inline("aaaa bbbb cccc dddd")],
        &Obstacles::default(),
    );
    assert!((r.scale - 1.0).abs() < 1e-6, "scale={}", r.scale);
    assert!(r.issues.is_empty(), "{:?}", r.issues);
    assert_eq!(
        r.paragraph.lines.len(),
        2,
        "lines={}",
        r.paragraph.lines.len()
    );
    // 首行基线：y1 - ascent(0.8)·size = 30 - 8 = 22。
    let b0 = r.paragraph.lines[0].baseline_y;
    assert!((b0 - 22.0).abs() < 1e-4, "baseline={b0}");
    // 第二行基线再降 12（10×1.2）。
    let b1 = r.paragraph.lines[1].baseline_y;
    assert!((b1 - 10.0).abs() < 1e-4, "baseline={b1}");
}

#[test]
fn text_growth_preserves_requested_size_and_reports_overflow() {
    let bbox = Rect::new(0.0, 0.0, 100.0, 12.0);
    let s = spec(bbox, 10.0, 1.0, Align::Left);
    let t = typeset_default();

    let base = "aaaa bbbb cccc dddd";
    let r_base = t.layout(pid(), &s, &[text_inline(base)], &Obstacles::default());
    let grown = format!("{base} {} {}", base, base);
    let r_grown = t.layout(pid(), &s, &[text_inline(&grown)], &Obstacles::default());

    assert_eq!(r_base.scale, 1.0);
    assert_eq!(r_grown.scale, 1.0);
    assert!(r_grown.paragraph.overflow);
    assert!(r_grown
        .paragraph
        .lines
        .iter()
        .flat_map(|l| &l.glyphs)
        .all(|g| g.size == 10.0));
    assert!(r_grown
        .issues
        .iter()
        .any(|i| matches!(i, syncpdf_typeset::TypesetIssue::Overflow { .. })));
    assert!(!r_grown
        .issues
        .contains(&syncpdf_typeset::TypesetIssue::MinScaleHit));
    assert!(
        r_grown.paragraph.lines.len() > r_base.paragraph.lines.len(),
        "base={} grown={}",
        r_base.paragraph.lines.len(),
        r_grown.paragraph.lines.len()
    );
}

#[test]
fn cjk_no_forbidden_punct_at_line_start() {
    let bbox = Rect::new(0.0, 0.0, 50.0, 200.0); // 窄框 → 多行
    let s = ParagraphSpec {
        bbox,
        font_size: 10.0,
        line_height: 1.2,
        align: Align::Left,
        first_indent: 0.0,
        is_rtl: false,
        color: Color::BLACK,
        styles: vec![(StyleId(1), StyleSpec::default())],
        lang: Lang::Zh,
        first_baseline: None,
    };
    let text = "这是一个测试段落，用来验证禁则处理是否正确。（括号内容）「引号内容」结束。";
    let t = typeset_default();
    let r = t.layout(pid(), &s, &[text_inline(text)], &Obstacles::default());
    assert!(r.paragraph.lines.len() >= 2);
    for line in &r.paragraph.lines {
        let first = line.glyphs.first().and_then(|g| g.text.chars().next());
        if let Some(c) = first {
            assert!(
                !matches!(c, '，' | '。' | '」' | '）' | '？' | '！'),
                "行首出现禁则标点 {c:?}"
            );
        }
        let last = line.glyphs.last().and_then(|g| g.text.chars().next());
        if let Some(c) = last {
            assert!(
                !matches!(c, '（' | '「' | '『' | '《'),
                "行尾出现禁则标点 {c:?}"
            );
        }
    }
}

#[test]
fn hard_break_creates_lines() {
    let bbox = Rect::new(0.0, 0.0, 500.0, 100.0);
    let s = spec(bbox, 10.0, 1.2, Align::Left);
    let t = typeset_default();
    let inlines = vec![
        text_inline("first line"),
        Inline::Br,
        text_inline("second line"),
    ];
    let r = t.layout(pid(), &s, &inlines, &Obstacles::default());
    assert_eq!(
        r.paragraph.lines.len(),
        2,
        "lines={}",
        r.paragraph.lines.len()
    );
    let texts: Vec<String> = r
        .paragraph
        .lines
        .iter()
        .map(|l| l.glyphs.iter().map(|g| g.text.as_str()).collect())
        .collect();
    assert_eq!(texts[0], "first line");
    assert_eq!(texts[1], "second line");
}

#[test]
fn atom_is_unbreakable_and_keeps_width() {
    let bbox = Rect::new(0.0, 0.0, 60.0, 40.0);
    let s = spec(bbox, 10.0, 1.2, Align::Left);
    let t = typeset_default();
    // 原子宽 30pt：一行放 "aa"(10pt) + 原子(30pt) + "bb"(10pt) = 50 < 60。
    let inlines = vec![
        text_inline("aa"),
        Inline::Atom {
            id: AtomId(1),
            width: 30.0,
            height: 10.0,
        },
        text_inline("bb"),
    ];
    let r = t.layout(pid(), &s, &inlines, &Obstacles::default());
    assert_eq!(r.paragraph.lines.len(), 1);
    assert_eq!(r.paragraph.lines[0].kept_atoms, vec![AtomId(1)]);
    // 行宽 = 10 + 30 + 10 = 50。
    let w = r.paragraph.lines[0].bbox.width();
    assert!((w - 50.0).abs() < 1.0, "width={w}");

    // 原子比行宽还宽 → 原子独占一行（不可断）。
    let inlines2 = vec![
        text_inline("aaaa"),
        Inline::Atom {
            id: AtomId(1),
            width: 55.0,
            height: 10.0,
        },
        text_inline("bbbb"),
    ];
    let r2 = t.layout(pid(), &s, &inlines2, &Obstacles::default());
    // 断行必须保证原子完整出现在某一行的 kept_atoms。
    let all_atoms: Vec<AtomId> = r2
        .paragraph
        .lines
        .iter()
        .flat_map(|l| l.kept_atoms.clone())
        .collect();
    assert_eq!(all_atoms, vec![AtomId(1)]);
}

#[test]
fn justify_first_line_increasing_x_and_last_glyph_near_right() {
    let bbox = Rect::new(0.0, 0.0, 100.0, 40.0);
    let s = spec(bbox, 10.0, 1.2, Align::Justify);
    let t = typeset_default();
    // 两个完整行 + 末行。
    let r = t.layout(
        pid(),
        &s,
        &[text_inline("aaaa bbbb aaaa bbbb aaaa bbbb aaaa bbbb aaaa")],
        &Obstacles::default(),
    );
    assert!(r.paragraph.lines.len() >= 2, "{}", r.paragraph.lines.len());
    let l0 = &r.paragraph.lines[0];
    // 字形 x 严格递增（存在空格拉开）。
    let mut sorted = true;
    for w in l0.glyphs.windows(2) {
        if w[1].x <= w[0].x {
            sorted = false;
        }
    }
    assert!(sorted, "first line x not increasing");
    // 首行（非末行）末字形右边 ≈ bbox.x1。
    let last = l0.glyphs.last().unwrap();
    let right = last.x + 5.0; // 最后一字形 advance 5pt（拉丁）
    assert!(
        (right - bbox.x1).abs() < 1.0,
        "right={right} vs x1={}",
        bbox.x1
    );
}

#[test]
fn widen_uses_neighbor_gap() {
    // 原框高20，下方邻居留20pt空隙；整个过程保持指定10pt字号。
    let bbox = Rect::new(0.0, 100.0, 100.0, 120.0);
    let neighbor = Rect::new(0.0, 60.0, 100.0, 80.0); // 原框底 100 与邻居顶 80 之间 20pt
    let mut s = spec(bbox, 10.0, 1.2, Align::Left);
    s.lang = Lang::Zh;
    let t = typeset_default();
    let obstacles = Obstacles {
        rects: vec![],
        neighbors: vec![neighbor],
    };
    // 25个CJK字符需要3行，原框放不下，加高至40pt后可容纳。
    let text = "一二三四五六七八九十".repeat(2) + "一二三四五";
    let r = t.layout(pid(), &s, &[text_inline(&text)], &obstacles);
    assert!(r.widened.is_some(), "应加宽，issues={:?}", r.issues);
    let w = r.widened.unwrap();
    // 不超上限：加宽后底不低于邻居顶。
    assert!(
        w.y0 >= neighbor.y1 - 1e-3,
        "y0={} neighbor.y1={}",
        w.y0,
        neighbor.y1
    );
    assert_eq!(r.paragraph.lines.len(), 3);
    assert_eq!(r.scale, 1.0);
    assert!(r
        .issues
        .iter()
        .any(|i| matches!(i, syncpdf_typeset::TypesetIssue::Widened { .. })));
}

#[test]
fn overflow_long_text_reports_issue_at_requested_size() {
    let bbox = Rect::new(0.0, 0.0, 100.0, 20.0);
    let s = spec(bbox, 10.0, 1.2, Align::Left);
    let t = typeset_default();
    let text = "word ".repeat(200);
    let r = t.layout(pid(), &s, &[text_inline(&text)], &Obstacles::default());
    assert!(
        r.issues
            .iter()
            .any(|i| matches!(i, syncpdf_typeset::TypesetIssue::Overflow { .. })),
        "issues={:?}",
        r.issues
    );
    assert_eq!(r.scale, 1.0);
    assert!(r
        .paragraph
        .lines
        .iter()
        .flat_map(|l| &l.glyphs)
        .all(|g| g.size == s.font_size));
    assert!(!r
        .issues
        .contains(&syncpdf_typeset::TypesetIssue::MinScaleHit));
    assert!(r.paragraph.overflow);
}

#[test]
fn first_indent_applies_to_first_line_only() {
    let bbox = Rect::new(0.0, 0.0, 60.0, 60.0);
    let mut s = spec(bbox, 10.0, 1.2, Align::Left);
    s.first_indent = 20.0;
    let t = typeset_default();
    let r = t.layout(
        pid(),
        &s,
        &[text_inline("aaaa bbbb cccc dddd")],
        &Obstacles::default(),
    );
    assert!(r.paragraph.lines.len() >= 2);
    let l0 = &r.paragraph.lines[0];
    let l1 = &r.paragraph.lines[1];
    assert!(
        (l0.bbox.x0 - bbox.x0 - 20.0).abs() < 1e-3,
        "l0.x0={}",
        l0.bbox.x0
    );
    assert!((l1.bbox.x0 - bbox.x0).abs() < 1e-3, "l1.x0={}", l1.bbox.x0);
}

#[test]
fn center_and_right_align() {
    let bbox = Rect::new(0.0, 0.0, 100.0, 20.0);
    let t = typeset_default();
    let inlines = [text_inline("abc")]; // 宽 15pt

    let s = spec(bbox, 10.0, 1.2, Align::Center);
    let r = t.layout(pid(), &s, &inlines, &Obstacles::default());
    let g = &r.paragraph.lines[0].glyphs[0];
    assert!((g.x - 42.5).abs() < 0.5, "center x={}", g.x); // (100-15)/2

    let s = spec(bbox, 10.0, 1.2, Align::Right);
    let r = t.layout(pid(), &s, &inlines, &Obstacles::default());
    let g = &r.paragraph.lines[0].glyphs[0];
    assert!((g.x - 85.0).abs() < 0.5, "right x={}", g.x);
}

#[test]
fn cjk_justify_distributes_tracking() {
    // 框宽 65：首行 6 字（60pt），slack 5pt 分到 5 个 gap → 字距 10 + 1 = 11。
    let bbox = Rect::new(0.0, 0.0, 65.0, 40.0);
    let mut s = spec(bbox, 10.0, 1.2, Align::Justify);
    s.lang = Lang::Zh;
    let t = typeset_default();
    let text = "一二三四五六七八九十";
    let r = t.layout(pid(), &s, &[text_inline(text)], &Obstacles::default());
    assert!(
        r.paragraph.lines.len() >= 2,
        "lines={}",
        r.paragraph.lines.len()
    );
    let l0 = &r.paragraph.lines[0];
    assert_eq!(l0.glyphs.len(), 6);
    // 首行（非末行）末字形右边 ≈ x1（justify 拉满）。
    let last = l0.glyphs.last().unwrap();
    let right = last.x + 10.0;
    assert!(
        (right - bbox.x1).abs() < 1.5,
        "right={right} x1={}",
        bbox.x1
    );
    // 字距均匀：相邻差 = 10 + 5/5 = 11。
    let d = l0.glyphs[1].x - l0.glyphs[0].x;
    assert!((d - 11.0).abs() < 0.6, "delta={d}");
    // 相邻 gap 一致。
    let d2 = l0.glyphs[2].x - l0.glyphs[1].x;
    assert!((d2 - d).abs() < 0.3, "d={d} d2={d2}");
}

#[test]
fn rtl_arabic_reorders() {
    let bbox = Rect::new(0.0, 0.0, 300.0, 20.0);
    let mut s = spec(bbox, 10.0, 1.2, Align::Left);
    s.lang = Lang::Ar;
    s.is_rtl = true;
    let t = typeset_default();
    // 纯阿拉伯字符串：bidi 重排后视觉序应反转。
    let text = "\u{0645}\u{0631}\u{062D}\u{0628}\u{0627}"; // مرحبا
    let r = t.layout(pid(), &s, &[text_inline(text)], &Obstacles::default());
    let line = &r.paragraph.lines[0];
    // 视觉序首字形应为原文最后一个字符（RTL 反转）。
    let logical_first = text.chars().next().unwrap();
    let visual_first = line.glyphs.first().unwrap().text.chars().next().unwrap();
    let logical_last = text.chars().next_back().unwrap();
    assert_ne!(visual_first, logical_first);
    assert_eq!(visual_first, logical_last);
}

#[test]
fn soft_hyphen_breaks_long_word() {
    // 单词 20 字符（100pt）超过 60pt 行宽，且无空格 → 必须用软断点。
    let bbox = Rect::new(0.0, 0.0, 60.0, 40.0);
    let s = spec(bbox, 10.0, 1.2, Align::Left);
    let t = typeset_default();
    let r = t.layout(
        pid(),
        &s,
        &[text_inline("extraordinariness")],
        &Obstacles::default(),
    );
    // 没有 Overflow（fit 阶梯缩字号后能放下）。
    assert!(
        !r.issues
            .iter()
            .any(|i| matches!(i, syncpdf_typeset::TypesetIssue::Overflow { .. })),
        "issues={:?}",
        r.issues
    );
    // 确认断行机会里确实有软断点。
    let opps = break_opportunities("extraordinariness", Lang::En);
    assert!(opps.iter().any(|o| o.soft_hyphen), "{opps:?}");
    assert!(
        r.paragraph
            .lines
            .iter()
            .take(r.paragraph.lines.len().saturating_sub(1))
            .any(|line| line.glyphs.last().is_some_and(|g| g.text == "-")),
        "selected soft break must draw its hyphen"
    );
}

#[test]
fn performance_100_paragraphs_under_500ms_debug() {
    let t0 = std::time::Instant::now();
    let t = typeset_default();
    let bbox = Rect::new(0.0, 0.0, 300.0, 60.0);
    let mut s = spec(bbox, 10.0, 1.2, Align::Justify);
    s.lang = Lang::Zh;
    // ≥300 字段落。
    let text: String = "这是一个用于性能测试的中文段落，内容会被重复多遍以形成足够长度的规模，从而覆盖断行与对齐的完整路径。".repeat(7);
    assert!(text.chars().count() >= 300, "n={}", text.chars().count());
    let obstacles = Obstacles::default();
    for i in 0..100u32 {
        let id = ParagraphId {
            page: 1,
            seq: i + 1,
        };
        let unique = format!("{i}{text}");
        let r = t.layout(id, &s, &[text_inline(&unique)], &obstacles);
        assert!(!r.paragraph.lines.is_empty());
    }
    let elapsed = t0.elapsed();
    println!("100×300字段落耗时: {elapsed:?}");
    assert!(elapsed.as_millis() < 500, "耗时 {elapsed:?} 超过 500ms");
}

#[test]
fn empty_paragraph_produces_no_lines() {
    let bbox = Rect::new(0.0, 0.0, 100.0, 20.0);
    let s = spec(bbox, 10.0, 1.2, Align::Left);
    let t = typeset_default();
    let r = t.layout(pid(), &s, &[], &Obstacles::default());
    assert!(r.paragraph.lines.is_empty());
    assert!((r.scale - 1.0).abs() < 1e-6);
}
