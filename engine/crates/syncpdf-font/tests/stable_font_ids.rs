use syncpdf_font::FontStore;

#[test]
fn manifest_member_order_does_not_change_font_identity() {
    let fonts = syncpdf_core::fixtures::fonts_dir().expect("builtin fonts required");
    let tmp = tempfile::tempdir().unwrap();
    let package = tmp.path().join("inter");
    std::fs::create_dir(&package).unwrap();
    for name in ["Inter-Regular.otf", "Inter-Bold.otf", "Inter-Italic.otf"] {
        std::fs::copy(fonts.join("inter").join(name), package.join(name)).unwrap();
    }
    let entries = [
        r#""alpha":{"regular":{"path":"Inter-Regular.otf","face_index":0}}"#,
        r#""middle":{"bold":{"path":"Inter-Bold.otf","face_index":0}}"#,
        r#""zulu":{"italic":{"path":"Inter-Italic.otf","face_index":0}}"#,
    ];
    for reverse in [false, true, false, true] {
        let body = if reverse {
            entries.iter().rev().copied().collect::<Vec<_>>().join(",")
        } else {
            entries.join(",")
        };
        std::fs::write(
            package.join("resources.json"),
            format!("{{\"package\":\"test\",\"schema_version\":1,\"fonts\":{{{body}}}}}"),
        )
        .unwrap();
        let store = FontStore::load_builtin(tmp.path()).unwrap();
        for (expected, (alias, weight, italic)) in [
            ("alpha", 400, false),
            ("middle", 700, false),
            ("zulu", 400, true),
        ]
        .into_iter()
        .enumerate()
        {
            let id = store.find_by_family(alias, weight, italic).unwrap();
            assert_eq!(id.0, expected as u32, "stable canonical ID for {alias}");
        }
    }
}
