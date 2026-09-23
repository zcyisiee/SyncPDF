//! 端到端：`Paragraph` → `Unit` → one-shot 提示词 → 假 pi CLI 流式回包 →
//! 逐块校验 → 边译边交付。**不调用真实 pi**：用写到 tempdir 的 `#!/bin/sh`
//! 脚本读走 stdin 并打印预录 JSONL。

use std::collections::HashMap;

use syncpdf_core::ir::{Align, Atom, AtomKind, Paragraph, RegionKind, StyleRun, Translatable};
use syncpdf_core::{AtomId, GlyphId, ObjRef, OpKey, PageId, ParagraphId, Rect, StyleId};
use syncpdf_translate::{
    build_unit, BlockStatus, Cache, ContextMap, Engine, PiTranslator, PromptSpec, Unit,
};

fn glyph(i: u32) -> GlyphId {
    GlyphId {
        page: PageId(0),
        op: OpKey::new(ObjRef::new(1, 0), 0),
        ordinal: i as u16,
    }
}

/// 造一个「一字形一字符」的段落。
fn paragraph(id: &str, text: &str) -> Paragraph {
    Paragraph {
        id: id.parse().unwrap(),
        page: PageId(0),
        region: 0,
        kind: RegionKind::Text,
        bbox: Rect::new(0.0, 0.0, 100.0, 20.0),
        lines: vec![],
        glyphs: (0..text.chars().count() as u32).map(glyph).collect(),
        text_spans: Vec::new(),
        style_runs: vec![],
        atoms: vec![],
        text: text.into(),
        align: Align::Left,
        first_indent: 0.0,
        line_height: 12.0,
        is_rtl: false,
        translatable: Translatable::Yes,
    }
}

fn texter(text: &str) -> impl Fn(GlyphId) -> Option<String> {
    let chars: Vec<String> = text.chars().map(|c| c.to_string()).collect();
    move |g: GlyphId| chars.get(g.ordinal as usize).cloned()
}

/// 带一个样式 run 与一个原子的段落。
fn rich_paragraph(id: &str) -> Paragraph {
    let text = "Deep learning models";
    let mut p = paragraph(id, text);
    // 字形 5..13 = "learning" 是样式 1；字形 14..20 = "models" 折叠成原子 1。
    p.style_runs = vec![StyleRun {
        id: StyleId(1),
        glyph_range: (5, 13),
        font: 0,
        size: 10.0,
        color: Default::default(),
        bold: true,
        italic: false,
    }];
    p.atoms = vec![Atom {
        id: AtomId(1),
        glyph_range: (14, 20),
        kind: AtomKind::Formula,
        text: "\\mathcal{M}".into(),
    }];
    p
}

/// 写一个假 pi：读走 stdin，按行打印预录的 JSONL。
#[cfg(unix)]
fn fake_pi(dir: &std::path::Path, deltas: &[&str]) -> std::path::PathBuf {
    use std::io::Write;
    use std::os::unix::fs::PermissionsExt;

    let mut printf = String::from("printf '%s\\n' ");
    for d in deltas {
        let ev = serde_json::json!({
            "assistantMessageEvent": { "type": "text_delta", "delta": d }
        });
        printf.push_str(&format!("'{}' ", serde_json::to_string(&ev).unwrap()));
    }
    printf.push_str(&format!(
        "'{}'",
        serde_json::json!({
            "type": "message_end",
            "message": { "role": "assistant", "stopReason": "stop", "content": [] }
        })
    ));

    let p = dir.join("pi");
    let mut f = std::fs::File::create(&p).unwrap();
    write!(f, "#!/bin/sh\ncat > /dev/null\n{printf}\n").unwrap();
    drop(f);
    std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
    p
}

#[cfg(unix)]
#[tokio::test]
async fn one_shot_pipeline_streams_blocks_and_fills_the_cache() {
    let paragraphs = [
        rich_paragraph("P01-001"),
        paragraph("P01-002", "The second paragraph of the document"),
        paragraph("P02-001", "A paragraph that the model will silently drop"),
    ];
    let units: Vec<Unit> = paragraphs
        .iter()
        .map(|p| build_unit(p, texter(&p.text)))
        .collect();

    // 源单元确实带上了 span 与原子占位。
    assert_eq!(
        units[0].html,
        "<p id=\"P01-001\">Deep <span data-style=\"1\">learning</span> {{KEEP_1}}</p>"
    );

    // 预录回包：前两段译出（P01-001 保留 span/原子），第三段被模型漏掉。
    // delta 故意切在标签与属性中间。
    let deltas = [
        "Here you go:\n```html\n<p id=\"P0",
        "1-001\">深度<span data-sty",
        "le=\"1\">学习</span> {{KEEP_1",
        "}}</p>\n<p id=\"P01-0",
        "02\">文档的第二段</p>\n```\nDone.",
    ];
    let dir = tempfile::tempdir().unwrap();
    let exe = fake_pi(dir.path(), &deltas);

    let cache = Cache::open_in_memory().unwrap();
    let engine = Engine::new(PiTranslator::new(&exe, "deepseek/deepseek-flash", "low"));
    let spec = PromptSpec::new("en", "zh-CN");
    let ctx = ContextMap::from_units(&units).with_hints(
        "P01-001".parse::<ParagraphId>().unwrap(),
        vec!["\\mathcal{M}".to_string()],
    );

    let mut streamed: Vec<ParagraphId> = Vec::new();
    let r = engine
        .translate_document(&spec, units.clone(), ctx, Some(&cache), |b| {
            streamed.push(b.id.clone())
        })
        .await
        .unwrap();

    // 前两段译好，第三段漏译 → 回退原文。
    assert_eq!(r.blocks.len(), 3);
    assert!(r.blocks[0].status.is_ok(), "{:?}", r.blocks[0]);
    assert_eq!(
        r.blocks[0].html,
        "<p id=\"P01-001\">深度<span data-style=\"1\">学习</span> {{KEEP_1}}</p>"
    );
    assert!(r.blocks[1].status.is_ok());
    assert!(matches!(r.blocks[2].status, BlockStatus::Fallback { .. }));
    assert_eq!(r.blocks[2].html, units[2].html, "漏译必须回退原文");
    assert_eq!(r.fallback_ids, vec![units[2].id.clone()]);
    assert!(r.empty_ids.is_empty());
    assert!(r.extra_ids.is_empty());

    // 边译边交付：前两段在首轮就流出来了。
    assert_eq!(streamed[0], units[0].id);
    assert_eq!(streamed[1], units[1].id);

    // 成功块进了缓存。
    assert_eq!(cache.len().unwrap(), 2);
    assert_eq!(
        cache.get("en", "zh-CN", &units[0].html).unwrap().as_deref(),
        Some(r.blocks[0].html.as_str())
    );
    assert_eq!(
        cache
            .origin_of("en", "zh-CN", &units[0].html)
            .unwrap()
            .as_deref(),
        Some("pi/deepseek-flash")
    );
}

#[cfg(unix)]
#[tokio::test]
async fn cached_units_never_reach_the_harness() {
    let p = paragraph("P01-001", "Only paragraph of this document");
    let units = vec![build_unit(&p, texter(&p.text))];

    let cache = Cache::open_in_memory().unwrap();
    cache
        .put_manual(
            "en",
            "zh-CN",
            &units[0].html,
            "<p id=\"P01-001\">本文唯一的一段</p>",
        )
        .unwrap();

    // 假 pi 一旦被调用就会失败（exit 1），因此「没失败」即证明它没被调用。
    let dir = tempfile::tempdir().unwrap();
    let exe = {
        use std::io::Write;
        use std::os::unix::fs::PermissionsExt;
        let q = dir.path().join("pi");
        let mut f = std::fs::File::create(&q).unwrap();
        write!(f, "#!/bin/sh\ncat > /dev/null\nexit 1\n").unwrap();
        drop(f);
        std::fs::set_permissions(&q, std::fs::Permissions::from_mode(0o755)).unwrap();
        q
    };

    let engine = Engine::new(PiTranslator::new(&exe, "m", "low"));
    let r = engine
        .translate_document(
            &PromptSpec::new("en", "zh-CN"),
            units,
            ContextMap::default(),
            Some(&cache),
            |_| {},
        )
        .await
        .unwrap();

    assert_eq!(r.stats.cache_hits, 1);
    assert_eq!(r.stats.prompts, 0, "没有单元要译就不该发提示词");
    assert!(r.blocks[0].from_cache);
    assert_eq!(r.blocks[0].html, "<p id=\"P01-001\">本文唯一的一段</p>");
}

#[test]
fn document_prompts_are_one_shot_until_the_limit() {
    let units: Vec<Unit> = (1..=30)
        .map(|i| {
            let p = paragraph(
                &format!("P{:02}-001", (i % 9) + 1),
                "A paragraph of moderate length for the packing test",
            );
            build_unit(&p, texter(&p.text))
        })
        .collect();
    let spec = PromptSpec::new("en", "zh-CN");
    let prompts = syncpdf_translate::build_document_prompts(&spec, &units, &HashMap::new());
    assert_eq!(prompts.len(), 1, "60k 上限下整份文档只发一片");
    assert_eq!(prompts[0].unit_ids.len(), 30);
    assert!(prompts[0].system.contains("zh-CN"));
}
