use super::*;

pub(super) fn resolve_named_links(doc: &mut Document) -> Result<()> {
    let mut names = BTreeMap::new();
    if let Ok(dests) = doc.catalog()?.get(b"Dests") {
        for (key, value) in resolve(doc, dests)?.as_dict()?.iter() {
            names.insert(key.clone(), destination_value(doc, value)?);
        }
    }
    if let Ok(tree) = doc.catalog()?.get(b"Names") {
        if let Ok(dests) = resolve(doc, tree)?.as_dict()?.get(b"Dests") {
            collect_names(doc, dests, &mut names, 0)?;
        }
    }
    fn replace(obj: &mut Object, names: &BTreeMap<Vec<u8>, Object>) -> Result<()> {
        match obj {
            Object::Dictionary(d) => {
                let key = if d.get(b"S").ok().and_then(|v| v.as_name().ok()) == Some(b"GoTo") {
                    b"D".as_slice()
                } else {
                    b"Dest".as_slice()
                };
                // 原文自身的悬空名称保持原样：合并后同样查不到，行为与原文一致（死链）。
                if let Ok(Object::Name(n) | Object::String(n, _)) = d.get(key) {
                    if let Some(value) = names.get(n) {
                        d.set(key, value.clone());
                    }
                }
                for (_, value) in d.iter_mut() {
                    replace(value, names)?;
                }
            }
            Object::Array(a) => {
                for value in a {
                    replace(value, names)?;
                }
            }
            Object::Stream(s) => {
                for (_, value) in s.dict.iter_mut() {
                    replace(value, names)?;
                }
            }
            _ => {}
        }
        Ok(())
    }
    for object in doc.objects.values_mut() {
        replace(object, &names)?;
    }
    Ok(())
}

fn destination_value(doc: &Document, value: &Object) -> Result<Object> {
    let value = resolve(doc, value)?;
    let value = if let Object::Dictionary(d) = value {
        resolve(doc, d.get(b"D")?)?
    } else {
        value
    };
    value.as_array()?;
    Ok(value.clone())
}

fn collect_names(
    doc: &Document,
    tree: &Object,
    names: &mut BTreeMap<Vec<u8>, Object>,
    depth: usize,
) -> Result<()> {
    if depth > 32 {
        return Err(DualError::Invalid("destination name tree too deep".into()));
    }
    let tree = resolve(doc, tree)?.as_dict()?;
    if let Ok(kids) = tree.get(b"Kids") {
        for child in resolve(doc, kids)?.as_array()? {
            collect_names(doc, child, names, depth + 1)?;
        }
    }
    if let Ok(entries) = tree.get(b"Names") {
        let entries = resolve(doc, entries)?.as_array()?;
        if entries.len() % 2 != 0 {
            return Err(DualError::Invalid("unpaired destination name".into()));
        }
        for pair in entries.chunks_exact(2) {
            names.insert(
                pair[0].as_str()?.to_vec(),
                destination_value(doc, &pair[1])?,
            );
        }
    }
    Ok(())
}

pub(super) fn transform_destinations(
    doc: &mut Document,
    placements: &BTreeMap<ObjectId, Placement>,
) -> Result<()> {
    fn visit(obj: &mut Object, placements: &BTreeMap<ObjectId, Placement>) -> Result<()> {
        match obj {
            Object::Array(a) => {
                if let Some(p) = a
                    .first()
                    .and_then(|o| o.as_reference().ok())
                    .and_then(|id| placements.get(&id))
                {
                    if a.get(1).is_some_and(|o| o.as_name().is_ok()) {
                        *a = transformed_destination(a, *p)?;
                        return Ok(());
                    }
                }
                for v in a {
                    visit(v, placements)?;
                }
            }
            Object::Dictionary(d) => {
                for (_, v) in d.iter_mut() {
                    visit(v, placements)?;
                }
            }
            Object::Stream(s) => {
                for (_, v) in s.dict.iter_mut() {
                    visit(v, placements)?;
                }
            }
            _ => {}
        }
        Ok(())
    }
    for object in doc.objects.values_mut() {
        visit(object, placements)?;
    }
    Ok(())
}

fn transformed_destination(a: &[Object], p: Placement) -> Result<Vec<Object>> {
    let name = a[1].as_name()?;
    let number = |i: usize| -> Result<Option<f32>> {
        match a.get(i) {
            Some(Object::Null) | None => Ok(None),
            Some(o) => Ok(Some(o.as_float()?)),
        }
    };
    let mut out = vec![Object::Reference(p.page)];
    match name {
        b"XYZ" => {
            let (x, y) = (number(2)?, number(3)?);
            let [a, b, c, d, e, f] = p.matrix;
            let coord = |u: f32, v: f32, w: f32| -> Object {
                let ux = if u == 0. { Some(0.) } else { x.map(|x| u * x) };
                let vy = if v == 0. { Some(0.) } else { y.map(|y| v * y) };
                ux.zip(vy)
                    .map_or(Object::Null, |(u, v)| Object::Real(u + v + w))
            };
            out.extend([
                Object::Name(b"XYZ".to_vec()),
                coord(a, c, e),
                coord(b, d, f),
                number(4)?.map_or(Object::Null, |z| Object::Real(z / p.scale)),
            ]);
        }
        b"Fit" | b"FitB" | b"FitR" => {
            let r = if name == b"FitR" {
                [
                    number(2)?.unwrap_or(p.crop[0]),
                    number(3)?.unwrap_or(p.crop[1]),
                    number(4)?.unwrap_or(p.crop[2]),
                    number(5)?.unwrap_or(p.crop[3]),
                ]
            } else {
                p.crop
            };
            out.push(Object::Name(b"FitR".to_vec()));
            out.extend(p.rect(r).map(Object::Real));
        }
        b"FitH" | b"FitBH" | b"FitV" | b"FitBV" => {
            let horizontal = matches!(name, b"FitH" | b"FitBH");
            let (x, y) = p.point(
                if horizontal {
                    p.crop[0]
                } else {
                    number(2)?.unwrap_or(p.crop[0])
                },
                if horizontal {
                    number(2)?.unwrap_or(p.crop[3])
                } else {
                    p.crop[3]
                },
            );
            out.extend([
                Object::Name(b"XYZ".to_vec()),
                Object::Real(x),
                Object::Real(y),
                Object::Null,
            ]);
        }
        _ => {
            return Err(DualError::Invalid(
                "unsupported local destination type".into(),
            ))
        }
    }
    Ok(out)
}

/// 注释框按规范由阅读器归一化，零面积合法（不可点击）；只拒绝非数值。
pub(super) fn annotation_rect(object: &Object) -> Result<[f32; 4]> {
    let values = object.as_array()?;
    if values.len() != 4 {
        return Err(DualError::Invalid("invalid rectangle".into()));
    }
    let mut r = [0.; 4];
    for (slot, value) in r.iter_mut().zip(values) {
        *slot = value.as_float()?;
    }
    if r.iter().any(|v| !v.is_finite()) {
        return Err(DualError::Invalid("invalid rectangle dimensions".into()));
    }
    Ok([
        r[0].min(r[2]),
        r[1].min(r[3]),
        r[0].max(r[2]),
        r[1].max(r[3]),
    ])
}

pub(super) fn annotations(doc: &Document, id: ObjectId, p: Placement) -> Result<Vec<Dictionary>> {
    let mut out = Vec::new();
    for annotation in doc.get_page_annotations(id)? {
        let mut annotation = annotation.clone();
        let rect = annotation_rect(resolve(doc, annotation.get(b"Rect")?)?)?;
        annotation.set("Rect", numbers(p.rect(rect)));
        annotation.set("P", Object::Reference(p.page));
        for key in [b"QuadPoints".as_slice(), b"Vertices", b"L"] {
            if let Ok(points) = annotation.get(key) {
                let points = resolve(doc, points)?.as_array()?;
                if points.len() % 2 != 0 {
                    return Err(DualError::Invalid("unpaired annotation coordinates".into()));
                }
                let mut transformed = Vec::new();
                for pair in points.chunks_exact(2) {
                    let (x, y) = p.point(pair[0].as_float()?, pair[1].as_float()?);
                    transformed.extend([Object::Real(x), Object::Real(y)]);
                }
                annotation.set(key, Object::Array(transformed));
            }
        }
        out.push(annotation);
    }
    Ok(out)
}
