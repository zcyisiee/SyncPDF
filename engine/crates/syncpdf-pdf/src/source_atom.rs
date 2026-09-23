//! Capture immutable page drawing for exact inline formula replay.
use crate::content::{parse_content, write_content, Operand};
use crate::{BoundPage, PatchSet};
use lopdf::{dictionary, Dictionary, Document, Object, ObjectId, Stream};
use std::collections::{BTreeMap, BTreeSet};
use syncpdf_core::ir::Paragraph;
use syncpdf_core::{AtomId, ParagraphId};

pub fn resource(para: &ParagraphId, atom: AtomId) -> String {
    format!("SPAtom{}_{}_{}", para.page, para.seq, atom.0)
}

fn resources(doc: &Document, mut id: ObjectId) -> lopdf::Result<Dictionary> {
    loop {
        let d = doc.get_dictionary(id)?;
        if let Ok(r) = d.get(b"Resources") {
            let mut r = match r {
                Object::Reference(id) => doc.get_dictionary(*id)?.clone(),
                _ => r.as_dict()?.clone(),
            };
            for key in [b"Font".as_slice(), b"XObject".as_slice()] {
                if let Ok(Object::Reference(id)) = r.get(key) {
                    r.set(key.to_vec(), doc.get_dictionary(*id)?.clone());
                }
            }
            return Ok(r);
        }
        id = d.get(b"Parent")?.as_reference()?;
    }
}

/// Called on a private candidate after glyph deletion. `source` is unmodified on
/// this page. Original resources remain referenced; their dictionaries are private.
pub fn install(
    doc: &mut Document,
    source: &Document,
    bound: &BoundPage,
    paras: &[&Paragraph],
) -> crate::patch::Result<()> {
    let atoms: Vec<_> = paras
        .iter()
        .flat_map(|p| {
            p.atoms
                .iter()
                .filter_map(move |a| a.source.map(|s| (*p, a, s)))
        })
        .collect();
    if atoms.is_empty() {
        return Ok(());
    }
    let page = bound.ir.page.number();
    let page_box = bound.ir.media_box;
    let page_id = source.get_pages()[&page];
    let mut target_resources = resources(doc, page_id)?;
    let mut xobjects = target_resources
        .get(b"XObject")
        .ok()
        .and_then(|v| v.as_dict().ok())
        .cloned()
        .unwrap_or_default();
    let mut clips = Vec::new();
    for (para, atom, geometry) in atoms {
        let name = resource(&para.id, atom.id);
        if xobjects.has(name.as_bytes()) {
            return Err(crate::PatchError::UnsupportedPath(
                "source atom resource already exists",
            ));
        }
        let keep: std::collections::BTreeSet<_> = para.glyphs
            [atom.glyph_range.0 as usize..atom.glyph_range.1 as usize]
            .iter()
            .copied()
            .collect();
        let keep_streams: BTreeSet<_> = keep.iter().map(|g| g.op.stream).collect();
        let unrelated_forms: BTreeSet<_> = bound
            .form_dos
            .iter()
            .map(|f| f.target)
            .filter(|s| !keep_streams.contains(s))
            .collect();
        let remove: Vec<_> = bound
            .ir
            .glyphs()
            .filter(|g| !keep.contains(&g.id) && !unrelated_forms.contains(&g.id.op.stream))
            .map(|g| g.id)
            .collect();
        // Strip unrelated source text, rather than relying on clipping to hide it
        // from text extraction / search / accessibility.
        let mut isolated = source.clone();
        let start = doc.max_id;
        isolated.max_id = start;
        let mut patch = PatchSet::new();
        patch.delete_glyphs(bound, &remove)?;
        patch.apply(&mut isolated, page)?;
        strip_unrelated_form_text(&mut isolated, page_id, &unrelated_forms)?;
        let form = Stream::new(
            dictionary! {
                "Type" => "XObject", "Subtype" => "Form", "FormType" => 1,
                "BBox" => vec![geometry.bbox.x0.into(),geometry.bbox.y0.into(),geometry.bbox.x1.into(),geometry.bbox.y1.into()],
                "Resources" => resources(&isolated,page_id)?,
            },
            isolated.get_page_content(page_id),
        );
        doc.max_id = isolated.max_id;
        doc.objects
            .extend(isolated.objects.into_iter().filter(|(id, _)| id.0 > start));
        let form_id = doc.add_object(form);
        xobjects.set(name, Object::Reference(form_id));
        clips.push(geometry.bbox);
    }
    target_resources.set("XObject", xobjects);
    doc.get_dictionary_mut(page_id)?
        .set("Resources", target_resources);
    // Sequential complement clips remove the original vector ink. Unlike a single
    // even-odd compound path, overlapping rectangles cannot expose an overlap again.
    let mut prefix = String::from("q\n");
    for r in &clips {
        prefix.push_str(&format!(
            "{} {} {} {} re {} {} {} {} re W* n\n",
            page_box.x0,
            page_box.y0,
            page_box.width(),
            page_box.height(),
            r.x0,
            r.y0,
            r.width(),
            r.height()
        ));
    }
    let before = doc.add_object(Stream::new(Dictionary::new(), prefix.into_bytes()));
    let after = doc.add_object(Stream::new(Dictionary::new(), b"Q\n".to_vec()));
    let mut contents = vec![Object::Reference(before)];
    contents.extend(
        doc.get_page_contents(page_id)
            .into_iter()
            .map(Object::Reference),
    );
    contents.push(Object::Reference(after));
    doc.get_dictionary_mut(page_id)?.set("Contents", contents);
    Ok(())
}

/// The source may contain deeply nested plots. We never patch their original
/// drawing. In the isolated formula copy only, clone those Forms without text;
/// paths/images remain available to the formula clip and source fonts stay shared.
fn strip_unrelated_form_text(
    doc: &mut Document,
    page: ObjectId,
    forms: &BTreeSet<syncpdf_core::ObjRef>,
) -> crate::patch::Result<()> {
    let replacements: BTreeMap<_, _> = forms
        .iter()
        .map(|r| ((r.obj, r.gen), doc.new_object_id()))
        .collect();
    fn remap_resources(
        doc: &Document,
        mut res: Dictionary,
        replacements: &BTreeMap<ObjectId, ObjectId>,
    ) -> lopdf::Result<Dictionary> {
        if let Ok(objects) = res.get(b"XObject") {
            let mut objects = match objects {
                Object::Reference(id) => doc.get_dictionary(*id)?.clone(),
                _ => objects.as_dict()?.clone(),
            };
            for (_, value) in objects.iter_mut() {
                if let Object::Reference(id) = value {
                    if let Some(new) = replacements.get(id) {
                        *id = *new;
                    }
                }
            }
            res.set("XObject", objects);
        }
        Ok(res)
    }
    for (old, new) in &replacements {
        let mut stream = doc.get_object(*old)?.as_stream()?.clone();
        let bytes = stream
            .decompressed_content()
            .unwrap_or_else(|_| stream.content.clone());
        let mut ops = parse_content(&bytes).map_err(|e| crate::PatchError::Parse {
            stream: old.0,
            msg: e.to_string(),
        })?;
        for op in &mut ops {
            match op.operator.as_str() {
                "Tj" | "'" => {
                    op.operands = vec![Operand::Str(Vec::new())];
                    op.mark_dirty();
                }
                "TJ" => {
                    op.operands = vec![Operand::Array(Vec::new())];
                    op.mark_dirty();
                }
                "\"" => {
                    if let Some(text) = op.operands.last_mut() {
                        *text = Operand::Str(Vec::new());
                        op.mark_dirty();
                    }
                }
                _ => {}
            }
        }
        stream.set_plain_content(write_content(&bytes, &ops));
        if let Ok(res) = stream.dict.get(b"Resources") {
            let res = match res {
                Object::Reference(id) => doc.get_dictionary(*id)?.clone(),
                _ => res.as_dict()?.clone(),
            };
            stream
                .dict
                .set("Resources", remap_resources(doc, res, &replacements)?);
        }
        doc.set_object(*new, stream);
    }
    let res = remap_resources(doc, resources(doc, page)?, &replacements)?;
    doc.get_dictionary_mut(page)?.set("Resources", res);
    Ok(())
}
