//! 链接与注释保留校验（M1-12）。
//!
//! 我们不删除页面对象、不动 `/Annots`，因此 `/Link` 注释天然保留；
//! 本模块只做**输出对照**：比较输入与输出文档每页 `/Annots` 数量，
//! 并确认每个 `/Link` 的 `/Dest` 或 `/A` 仍能解析。

use lopdf::{Dictionary, Document, Object, ObjectId};

/// 比较输入/输出文档的链接保留情况；返回问题描述列表（空表示无问题）。
pub fn links_check(doc_in: &Document, doc_out: &Document) -> Vec<String> {
    let mut problems = Vec::new();
    let pages_in = doc_in.get_pages();
    let pages_out = doc_out.get_pages();
    if pages_in.len() != pages_out.len() {
        problems.push(format!(
            "page count changed: {} -> {}",
            pages_in.len(),
            pages_out.len()
        ));
    }
    for (num, id_in) in &pages_in {
        let Some(id_out) = pages_out.get(num) else {
            problems.push(format!("page {num} missing in output"));
            continue;
        };
        let annots_in = annot_ids(doc_in, *id_in);
        let annots_out = annot_ids(doc_out, *id_out);
        if annots_in.len() != annots_out.len() {
            problems.push(format!(
                "page {num}: /Annots count {} -> {}",
                annots_in.len(),
                annots_out.len()
            ));
        }
        // 逐个检查输出侧 Link 的可解析性。
        for aid in &annots_out {
            let Ok(d) = dict_of(doc_out, *aid) else {
                problems.push(format!("page {num}: annot {} unreadable", aid.0));
                continue;
            };
            let subtype = d.get(b"Subtype").ok().and_then(|o| o.as_name().ok());
            if subtype != Some(b"Link") {
                continue;
            }
            let dest_ok = d
                .get(b"Dest")
                .ok()
                .map(|o| dest_resolvable(doc_out, o))
                .unwrap_or(false);
            let action_ok = d
                .get(b"A")
                .ok()
                .and_then(|o| resolve(doc_out, o))
                .and_then(|o| o.as_dict().ok())
                .map(|a| a.get(b"S").is_ok())
                .unwrap_or(false);
            if !dest_ok && !action_ok {
                problems.push(format!(
                    "page {num}: link {} has neither resolvable /Dest nor /A",
                    aid.0
                ));
            }
        }
    }
    problems
}

/// 页的 `/Annots` 里的注释对象 id 列表。
fn annot_ids(doc: &Document, page_id: ObjectId) -> Vec<ObjectId> {
    let Ok(d) = dict_of(doc, page_id) else {
        return Vec::new();
    };
    let Some(annots) = d.get(b"Annots").ok().and_then(|o| resolve(doc, o)) else {
        return Vec::new();
    };
    match annots {
        Object::Array(a) => a
            .iter()
            .filter_map(|o| o.as_reference().ok())
            .collect::<Vec<_>>(),
        _ => Vec::new(),
    }
}

/// 解引用一层。
fn resolve<'a>(doc: &'a Document, o: &'a Object) -> Option<&'a Object> {
    match o {
        Object::Reference(id) => doc.get_object(*id).ok(),
        other => Some(other),
    }
}

fn dict_of(doc: &Document, id: ObjectId) -> Result<&Dictionary, ()> {
    match doc.get_object(id) {
        Ok(Object::Dictionary(d)) => Ok(d),
        Ok(Object::Stream(s)) => Ok(&s.dict),
        _ => Err(()),
    }
}

/// `/Dest` 是否可解析：名字（需在 /Dests 或 /Names 里）、数组、或命名目标。
fn dest_resolvable(doc: &Document, dest: &Object) -> bool {
    match dest {
        Object::Array(a) => {
            // 首个元素应是页引用或页号。
            !a.is_empty()
        }
        Object::String(..) | Object::Name(..) => named_dest_exists(doc, dest),
        Object::Reference(id) => doc.get_object(*id).is_ok(),
        _ => false,
    }
}

/// 命名目标是否在文档里存在：catalog `/Names /Dests` 名字树（PDF 1.2+），
/// 或 catalog 的 `/Dests` 字典（PDF 1.1）。
fn named_dest_exists(doc: &Document, dest: &Object) -> bool {
    let key: &[u8] = match dest {
        Object::String(s, _) => s,
        Object::Name(n) => n,
        _ => return false,
    };
    let Some(catalog) = doc.catalog().ok() else {
        return false;
    };
    let tree = catalog
        .get(b"Names")
        .ok()
        .and_then(|o| resolve(doc, o))
        .and_then(|o| o.as_dict().ok())
        .and_then(|d| d.get(b"Dests").ok())
        .and_then(|o| resolve(doc, o))
        .and_then(|o| o.as_dict().ok());
    if tree.is_some_and(|t| name_tree_contains(doc, t, key, 0)) {
        return true;
    }
    catalog
        .get(b"Dests")
        .ok()
        .and_then(|o| resolve(doc, o))
        .and_then(|o| o.as_dict().ok())
        .is_some_and(|d| d.has(key))
}

/// 名字树深度上限：防御 `/Kids` 环；真实文档的树只有几层。
const NAME_TREE_MAX_DEPTH: usize = 32;

/// 在名字树节点中查找 key：叶节点查 `/Names`（key value 交替），
/// 中间节点递归 `/Kids`（大文档会把名字拆到多个子节点）。
fn name_tree_contains(doc: &Document, node: &Dictionary, key: &[u8], depth: usize) -> bool {
    if depth > NAME_TREE_MAX_DEPTH {
        return false;
    }
    let array = |name: &[u8]| {
        node.get(name)
            .ok()
            .and_then(|o| resolve(doc, o))
            .and_then(|o| o.as_array().ok())
    };
    if array(b"Names").is_some_and(|names| {
        names
            .chunks(2)
            .any(|pair| pair[0].as_str().is_ok_and(|s| s == key))
    }) {
        return true;
    }
    array(b"Kids").is_some_and(|kids| {
        kids.iter()
            .filter_map(|k| resolve(doc, k))
            .filter_map(|k| k.as_dict().ok())
            .any(|k| name_tree_contains(doc, k, key, depth + 1))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use lopdf::Stream;

    /// 造一个带 `/Annots` 的文档。
    fn doc_with_links(n_links: usize, dest_style: u8) -> Document {
        let mut doc = Document::with_version("1.7");
        let pages_id = doc.new_object_id();
        let mut annots: Vec<Object> = Vec::new();
        for i in 0..n_links {
            let mut a = Dictionary::new();
            a.set("Type", Object::Name(b"Annot".to_vec()));
            a.set("Subtype", Object::Name(b"Link".to_vec()));
            a.set("Rect", vec![0.into(), 0.into(), 10.into(), 10.into()]);
            match dest_style {
                0 => a.set(
                    "Dest",
                    vec![Object::Reference(pages_id), Object::Name(b"XYZ".to_vec())],
                ),
                _ => {
                    let mut act = Dictionary::new();
                    act.set("S", Object::Name(b"URI".to_vec()));
                    act.set(
                        "URI",
                        Object::String(
                            format!("https://e/{i}").into_bytes(),
                            lopdf::StringFormat::Literal,
                        ),
                    );
                    a.set("A", act);
                }
            }
            annots.push(Object::Reference(doc.add_object(a)));
        }
        let content = doc.add_object(Stream::new(Dictionary::new(), b"BT ET".to_vec()));
        let mut page = Dictionary::new();
        page.set("Type", Object::Name(b"Page".to_vec()));
        page.set("Parent", Object::Reference(pages_id));
        page.set("MediaBox", vec![0.into(), 0.into(), 595.into(), 842.into()]);
        page.set("Contents", Object::Reference(content));
        page.set("Annots", Object::Array(annots));
        let page_id = doc.add_object(page);
        let mut pages = Dictionary::new();
        pages.set("Type", Object::Name(b"Pages".to_vec()));
        pages.set("Kids", vec![Object::Reference(page_id)]);
        pages.set("Count", 1);
        doc.objects.insert(pages_id, Object::Dictionary(pages));
        let cat = doc.add_object(Dictionary::new());
        let cd = doc.get_object_mut(cat).unwrap().as_dict_mut().unwrap();
        cd.set("Type", Object::Name(b"Catalog".to_vec()));
        cd.set("Pages", Object::Reference(pages_id));
        doc.trailer.set("Root", Object::Reference(cat));
        doc
    }

    #[test]
    fn identical_docs_have_no_problems() {
        let a = doc_with_links(3, 0);
        let b = doc_with_links(3, 0);
        assert!(links_check(&a, &b).is_empty());
    }

    #[test]
    fn missing_link_is_reported() {
        let a = doc_with_links(3, 0);
        let b = doc_with_links(2, 0);
        let p = links_check(&a, &b);
        assert!(!p.is_empty());
        assert!(p.iter().any(|s| s.contains("/Annots count")), "{p:?}");
    }

    #[test]
    fn action_links_ok() {
        let a = doc_with_links(2, 1);
        let b = doc_with_links(2, 1);
        assert!(links_check(&a, &b).is_empty());
    }

    #[test]
    fn page_count_change_reported() {
        let a = doc_with_links(1, 0);
        let b = doc_with_links(1, 0);
        // 删掉输出的一页 → 数量不一致。
        let mut c = b.clone();
        let pid = c.get_pages()[&1];
        c.objects.remove(&pid);
        if let Ok(cat) = c.catalog() {
            if let Ok(pages_id) = cat.get(b"Pages").and_then(|o| o.as_reference()) {
                if let Ok(pages) = c.get_dictionary_mut(pages_id) {
                    pages.set("Kids", Object::Array(vec![]));
                    pages.set("Count", 0);
                }
            }
        }
        let p = links_check(&a, &c);
        assert!(p.iter().any(|s| s.contains("page count")), "{p:?}");
    }

    #[test]
    fn broken_link_is_reported() {
        let a = doc_with_links(1, 0);
        let mut b = doc_with_links(1, 0);
        // 把 Link 的 /Dest 与 /A 都去掉。
        let pid = b.get_pages()[&1];
        let annots = b
            .get_object(pid)
            .unwrap()
            .as_dict()
            .unwrap()
            .get(b"Annots")
            .unwrap()
            .as_array()
            .unwrap()
            .to_vec();
        let aid = annots[0].as_reference().unwrap();
        let d = b.get_object_mut(aid).unwrap().as_dict_mut().unwrap();
        d.remove(b"Dest");
        let p = links_check(&a, &b);
        assert!(p.iter().any(|s| s.contains("neither")), "{p:?}");
    }

    /// 把唯一 Link 的目标换成命名目标 `name`。
    fn set_named_dest(doc: &mut Document, name: &str) {
        let pid = doc.get_pages()[&1];
        let aid = doc
            .get_dictionary(pid)
            .unwrap()
            .get(b"Annots")
            .unwrap()
            .as_array()
            .unwrap()[0]
            .as_reference()
            .unwrap();
        let d = doc.get_dictionary_mut(aid).unwrap();
        d.set("Dest", Object::string_literal(name));
    }

    fn set_catalog(doc: &mut Document, key: &str, value: Object) {
        let cat = doc.trailer.get(b"Root").unwrap().as_reference().unwrap();
        doc.get_dictionary_mut(cat).unwrap().set(key, value);
    }

    fn names_leaf(doc: &mut Document, names: &[&str]) -> Object {
        let pairs: Vec<Object> = names
            .iter()
            .flat_map(|n| [Object::string_literal(*n), vec![Object::Integer(0)].into()])
            .collect();
        Object::Reference(doc.add_object(lopdf::dictionary! { "Names" => pairs }))
    }

    fn named_dest_problems(doc: &Document) -> Vec<String> {
        links_check(doc, doc)
    }

    #[test]
    fn named_dest_in_flat_name_tree_resolves() {
        let mut doc = doc_with_links(1, 0);
        set_named_dest(&mut doc, "sec.1");
        let leaf = names_leaf(&mut doc, &["fig.1", "sec.1"]);
        set_catalog(
            &mut doc,
            "Names",
            lopdf::dictionary! { "Dests" => leaf }.into(),
        );
        assert!(named_dest_problems(&doc).is_empty());
    }

    #[test]
    fn named_dest_in_name_tree_kids_resolves() {
        // 大文档把名字树拆成 /Kids 子节点（例：Elsevier 的 af005/cor1）。
        let mut doc = doc_with_links(1, 0);
        set_named_dest(&mut doc, "cor1");
        let a = names_leaf(&mut doc, &["af005", "af010"]);
        let b = names_leaf(&mut doc, &["bib1", "cor1"]);
        let mid = Object::Reference(doc.add_object(lopdf::dictionary! { "Kids" => vec![b] }));
        let root = doc.add_object(lopdf::dictionary! { "Kids" => vec![a, mid] });
        set_catalog(
            &mut doc,
            "Names",
            lopdf::dictionary! { "Dests" => Object::Reference(root) }.into(),
        );
        assert!(named_dest_problems(&doc).is_empty());
    }

    #[test]
    fn named_dest_in_legacy_catalog_dests_resolves() {
        // PDF 1.1：catalog 的 /Dests 字典按名字索引。
        let mut doc = doc_with_links(1, 0);
        set_named_dest(&mut doc, "intro");
        set_catalog(
            &mut doc,
            "Dests",
            lopdf::dictionary! { "intro" => vec![Object::Integer(0)] }.into(),
        );
        assert!(named_dest_problems(&doc).is_empty());
    }

    #[test]
    fn unknown_named_dest_is_reported() {
        let mut doc = doc_with_links(1, 0);
        set_named_dest(&mut doc, "missing");
        let a = names_leaf(&mut doc, &["af005"]);
        let root = doc.add_object(lopdf::dictionary! { "Kids" => vec![a] });
        set_catalog(
            &mut doc,
            "Names",
            lopdf::dictionary! { "Dests" => Object::Reference(root) }.into(),
        );
        let p = named_dest_problems(&doc);
        assert!(p.iter().any(|s| s.contains("neither")), "{p:?}");
    }
}
