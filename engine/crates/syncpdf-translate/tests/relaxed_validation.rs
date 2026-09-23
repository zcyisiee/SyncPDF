//! Policy regressions: typography/technical English are not translation failures.
use syncpdf_translate::{
    parse_unit_html, validate, Cache, ContextMap, Engine, FakeTranslator, PromptSpec, Unit,
    ValidateCtx, Violation,
};

fn unit(html: &str) -> Unit {
    let p = parse_unit_html(html).unwrap();
    Unit {
        id: p.id.clone(),
        html: html.into(),
        styles: p.style_ids().len() as u32,
        atoms: p.atom_ids(),
        breaks: p.break_count(),
    }
}

#[test]
fn technical_english_and_short_mixed_language_labels_have_no_ratio_gate() {
    let u = unit(r#"<p id="P19-018">G Evading Neural Cleanse</p>"#);
    for body in [
        "G 规避 Neural Cleanse",
        "G Evading Neural Cleanse",
        "G Neural Cleanse",
    ] {
        let html = format!("<p id=\"P19-018\">{body}</p>");
        assert!(
            validate(&ValidateCtx::new(&u, "zh-CN"), &html).is_ok(),
            "{body}"
        );
    }
    let model = unit(r#"<p id="P18-011">InceptionResNetV1</p>"#);
    assert!(validate(&ValidateCtx::new(&model, "zh-CN"), &model.html).is_ok());
}

#[test]
fn known_styles_can_be_split_merged_omitted_empty_or_atom_only() {
    let u = unit(
        r#"<p id="P01-001"><span data-style="1">first {{KEEP_1}}</span> <span data-style="2">second {{KEEP_2}}</span></p>"#,
    );
    for body in [
        r#"{{KEEP_2}} second then {{KEEP_1}} first"#,
        r#"<span data-style="2">{{KEEP_2}} second {{KEEP_1}} first</span>"#,
        r#"<span data-style="2">second</span> {{KEEP_2}} <span data-style="2">first {{KEEP_1}}</span>"#,
        r#"<span data-style="1"> </span> second {{KEEP_2}} then first {{KEEP_1}}"#,
        r#"<span data-style="2">{{KEEP_1}}</span> first then second {{KEEP_2}}"#,
    ] {
        let html = format!("<p id=\"P01-001\">{body}</p>");
        assert!(
            validate(&ValidateCtx::new(&u, "zh-CN"), &html).is_ok(),
            "{body}"
        );
    }
}

#[test]
fn relaxing_style_and_language_does_not_relax_content_identity() {
    let u = unit(
        r#"<p id="P01-001"><span data-style="1">Neural Cleanse {{KEEP_1}} at 20 epochs</span></p>"#,
    );
    for (html, violation) in [
        (
            r#"<p id="P01-001">Neural Cleanse at 20 epochs</p>"#,
            Violation::PlaceholderCount,
        ),
        (
            r#"<p id="P01-001">Neural Cleanse {{KEEP_1}} {{KEEP_1}} at 20 epochs</p>"#,
            Violation::PlaceholderCount,
        ),
        (
            r#"<p id="P01-001">Neural Cleanse {{KEEP_9}} at 20 epochs</p>"#,
            Violation::UnknownPlaceholder,
        ),
        (
            r#"<p id="P01-001">Neural Cleanse {{KEEP_1}} at 21 epochs</p>"#,
            Violation::ProtectedLiteralCount,
        ),
        (
            r#"<p id="P01-002">Neural Cleanse {{KEEP_1}} at 20 epochs</p>"#,
            Violation::UnknownParagraph,
        ),
        (
            r#"<p id="P01-001"><span data-style="9">Neural Cleanse {{KEEP_1}} at 20 epochs</span></p>"#,
            Violation::UnknownStyle,
        ),
    ] {
        assert!(validate(&ValidateCtx::new(&u, "zh-CN"), html)
            .unwrap_err()
            .contains(&violation));
    }
}

#[tokio::test]
async fn relaxed_cached_blocks_are_revalidated_and_delivered_without_requests() {
    let u = unit(
        r#"<p id="P19-018">G <span data-style="1">Evading</span> <span data-style="2">Neural Cleanse</span></p>"#,
    );
    let target = r#"<p id="P19-018">G 规避 <span data-style="2">Neural</span> <span data-style="2">Cleanse</span></p>"#;
    let cache = Cache::open_in_memory().unwrap();
    cache.put_manual("en", "zh-CN", &u.html, target).unwrap();
    let units = vec![u];
    let ctx = ContextMap::from_units(&units);
    let mut delivered = Vec::new();
    let result = Engine::new(FakeTranslator::Echo)
        .with_cache_only(true)
        .translate_document(
            &PromptSpec::new("en", "zh-CN"),
            units,
            ctx,
            Some(&cache),
            |b| delivered.push(b),
        )
        .await
        .unwrap();
    assert_eq!(result.stats.cache_hits, 1);
    assert_eq!(result.stats.primary_prompts, 0);
    assert_eq!(result.stats.retry_prompts, 0);
    assert!(result.fallback_ids.is_empty());
    assert_eq!(delivered.len(), 1);
    assert!(delivered[0].status.is_ok() && delivered[0].from_cache);
    assert_eq!(delivered[0].html, target);
}
