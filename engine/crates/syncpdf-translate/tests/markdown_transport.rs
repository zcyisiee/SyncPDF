use syncpdf_core::{AtomId, ParagraphId, StyleId};
use syncpdf_translate::markdown::{parse, serialize, MarkdownStream, TRANSPORT_VERSION};
use syncpdf_translate::{ParsedUnit, Segment};

fn unit(id: &str, segments: Vec<Segment>) -> ParsedUnit {
    ParsedUnit {
        id: id.parse::<ParagraphId>().unwrap(),
        segments,
    }
}

#[test]
fn version_and_all_legal_segment_kinds_roundtrip() {
    assert!(!TRANSPORT_VERSION.is_empty());
    let original = unit(
        "P01-001",
        vec![
            Segment::Text("中文 é []{}\\ {{KEEP_1}} <!-- * _ ` #!.+-()\nnext".into()),
            Segment::Style {
                id: StyleId(2),
                inner: vec![
                    Segment::Text("A\\\nB".into()),
                    Segment::Atom(AtomId(3)),
                    Segment::Br,
                    Segment::Text("end".into()),
                ],
            },
            Segment::Atom(AtomId(1)),
            Segment::Br,
            Segment::Text("tail\n".into()),
        ],
    );
    let markdown = serialize(&original);
    assert!(markdown.contains(r"\{\{KEEP\_1\}\}"));
    assert!(markdown.contains(r"\#\!\.\+\-\(\)"));
    assert_eq!(parse(&markdown).unwrap(), original);
}

#[test]
fn literal_backslash_before_newline_is_not_a_break() {
    let original = unit("P01-001", vec![Segment::Text("a\\\nb".into())]);
    let markdown = serialize(&original);
    assert!(markdown.contains("a\\\\\nb"));
    assert_eq!(parse(&markdown).unwrap(), original);
    assert_eq!(
        parse("<!-- syncpdf:block P01-001 -->\na\\\nb\n<!-- syncpdf:end P01-001 -->")
            .unwrap()
            .segments,
        vec![
            Segment::Text("a".into()),
            Segment::Br,
            Segment::Text("b".into())
        ]
    );
}

#[test]
fn every_utf8_split_and_character_stream_agree() {
    let one = unit("P01-001", vec![Segment::Text("é中 😀".into())]);
    let two = unit("P01-002", vec![Segment::Text("second".into())]);
    let doc = format!("{}\n{}", serialize(&one), serialize(&two));
    let expected = vec![Ok(one.clone()), Ok(two.clone())];
    for i in (0..=doc.len()).filter(|i| doc.is_char_boundary(*i)) {
        let mut stream = MarkdownStream::new();
        let mut got = stream.push(&doc[..i]);
        got.extend(stream.push(&doc[i..]));
        got.extend(stream.finish());
        assert_eq!(got, expected, "split at {i}");
    }
    let mut stream = MarkdownStream::new();
    let mut got = Vec::new();
    for ch in doc.chars() {
        got.extend(stream.push(&ch.to_string()));
    }
    got.extend(stream.finish());
    assert_eq!(got, expected);
}

#[test]
fn first_complete_end_marker_delivers_before_eof_and_duplicates_survive() {
    let original = unit("P01-001", vec![Segment::Text("ok".into())]);
    let block = serialize(&original);
    let mut stream = MarkdownStream::new();
    assert_eq!(stream.push(&block), vec![Ok(original.clone())]);
    assert_eq!(stream.push(&format!("\n{block}")), vec![Ok(original)]);
    assert!(stream.finish().is_empty());
}

#[test]
fn prior_valid_block_survives_tail_errors() {
    let one = unit("P01-001", vec![Segment::Text("ok".into())]);
    let mut stream = MarkdownStream::new();
    assert_eq!(stream.push(&serialize(&one)), vec![Ok(one)]);
    assert!(stream
        .push("\n<!-- syncpdf:block P01-002 -->\nhalf")
        .is_empty());
    let tail = stream.finish();
    assert_eq!(tail.len(), 1);
    assert!(tail[0]
        .as_ref()
        .unwrap_err()
        .to_string()
        .contains("unterminated"));
}

#[test]
fn rejects_mismatch_unknown_markers_nested_styles_and_attributes() {
    let bad = [
        "<!-- syncpdf:block P01-001 -->\nx\n<!-- syncpdf:end P01-002 -->",
        "<!-- syncpdf:other P01-001 -->",
        "<!-- syncpdf:block bad -->\nx\n<!-- syncpdf:end bad -->",
        "<!-- syncpdf:block P01-001 -->\n[x]{link=1}\n<!-- syncpdf:end P01-001 -->",
        "<!-- syncpdf:block P01-001 -->\n[[x]{style=2}]{style=1}\n<!-- syncpdf:end P01-001 -->",
        "<!-- syncpdf:block P01-001 -->\n{{KEEP_01}}\n<!-- syncpdf:end P01-001 -->",
        "<!-- syncpdf:block P01-001 -->\nraw <!-- marker -->\n<!-- syncpdf:end P01-001 -->",
        "junk\n<!-- syncpdf:block P01-001 -->\nx\n<!-- syncpdf:end P01-001 -->",
    ];
    for input in bad {
        let err = parse(input).unwrap_err();
        assert!(err.to_string().contains("byte"), "{input:?}: {err}");
    }
}

#[test]
fn ordinary_physical_newline_does_not_merge_words() {
    let parsed =
        parse("<!-- syncpdf:block P01-001 -->\none\ntwo\n<!-- syncpdf:end P01-001 -->").unwrap();
    assert_eq!(parsed.segments, vec![Segment::Text("one\ntwo".into())]);
}

#[test]
fn empty_body_empty_style_and_carriage_return_roundtrip() {
    for original in [
        unit("P01-001", vec![]),
        unit(
            "P01-001",
            vec![Segment::Style {
                id: StyleId(1),
                inner: vec![],
            }],
        ),
        unit("P01-001", vec![Segment::Text("trailing\r".into())]),
    ] {
        assert_eq!(parse(&serialize(&original)).unwrap(), original);
    }
}

#[test]
fn adjacent_marker_lines_require_a_newline() {
    let one = unit("P01-001", vec![Segment::Text("a".into())]);
    let two = unit("P01-002", vec![Segment::Text("b".into())]);
    let input = format!("{}{}", serialize(&one), serialize(&two));
    assert!(parse(&input).is_err());
    let mut stream = MarkdownStream::new();
    assert_eq!(stream.push(&serialize(&one)), vec![Ok(one)]);
    let mut tail = stream.push(&serialize(&two));
    tail.extend(stream.finish());
    assert!(tail.iter().any(Result::is_err));
}

#[test]
fn malformed_stream_results_are_split_independent() {
    for input in [
        "<!-- syncpdf:block P01-001 -->\nok\n<!-- syncpdf:end P01-001 -->junk\n",
        "<!-- syncpdf:block P01-001 -->\nok\n<!-- syncpdf:end P01-002 -->\n",
        "<!-- syncpdf:block P01-001 -->\n[x]{style=0}\n<!-- syncpdf:end P01-001 -->\n",
        "<!-- syncpdf:block P01-001 -->\nok\n<!-- syncpdf:end P01-001 -->\n<!-- syncpdf:block P01-002 -->\n半块",
    ] {
        let mut whole = MarkdownStream::new();
        let mut expected = whole.push(input);
        expected.extend(whole.finish());
        for i in (0..=input.len()).filter(|i| input.is_char_boundary(*i)) {
            let mut split = MarkdownStream::new();
            let mut got = split.push(&input[..i]);
            got.extend(split.push(&input[i..]));
            got.extend(split.finish());
            assert_eq!(got, expected, "split at {i} of {input:?}");
        }
    }
}
