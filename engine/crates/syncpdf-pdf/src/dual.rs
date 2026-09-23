//! A3 landscape comparison export, with selectable vector content on both sides.
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use lopdf::{dictionary, Dictionary, Document, Object, ObjectId, Stream};

mod navigation;
#[cfg(test)]
mod tests;

pub const A3_WIDTH: f32 = 420.0 * 72.0 / 25.4;
pub const A3_HEIGHT: f32 = 297.0 * 72.0 / 25.4;

#[derive(Debug, thiserror::Error)]
pub enum DualError {
    #[error(transparent)]
    Pdf(#[from] lopdf::Error),
    #[error(transparent)]
    Write(#[from] crate::writer::WriteError),
    #[error("dual PDF: {0}")]
    Invalid(String),
}
type Result<T> = std::result::Result<T, DualError>;

#[derive(Debug, Clone, Copy)]
struct Placement {
    page: ObjectId,
    crop: [f32; 4],
    matrix: [f32; 6],
    scale: f32,
}

impl Placement {
    fn new(doc: &Document, id: ObjectId, page: ObjectId, right: bool) -> Result<Self> {
        let media = rectangle(
            &inherited(doc, id, b"MediaBox")?
                .ok_or_else(|| DualError::Invalid("missing MediaBox".into()))?,
        )?;
        let crop = inherited(doc, id, b"CropBox")?
            .map(|o| rectangle(&o))
            .transpose()?
            .unwrap_or(media);
        let crop = [
            crop[0].max(media[0]),
            crop[1].max(media[1]),
            crop[2].min(media[2]),
            crop[3].min(media[3]),
        ];
        let [x0, y0, x1, y1] = crop;
        if x1 <= x0 || y1 <= y0 {
            return Err(DualError::Invalid("empty visible page box".into()));
        }
        let rotation = inherited(doc, id, b"Rotate")?
            .map(|o| o.as_i64())
            .transpose()?
            .unwrap_or(0)
            .rem_euclid(360);
        let (mut matrix, width, height) = match rotation {
            0 => ([1., 0., 0., 1., -x0, -y0], x1 - x0, y1 - y0),
            90 => ([0., -1., 1., 0., -y0, x1], y1 - y0, x1 - x0),
            180 => ([-1., 0., 0., -1., x1, y1], x1 - x0, y1 - y0),
            270 => ([0., 1., -1., 0., y1, -x0], y1 - y0, x1 - x0),
            _ => {
                return Err(DualError::Invalid(
                    "page rotation is not a multiple of 90".into(),
                ))
            }
        };
        let scale = (A3_WIDTH * 0.5 / width).min(A3_HEIGHT / height);
        for v in &mut matrix {
            *v *= scale;
        }
        matrix[4] +=
            (A3_WIDTH * 0.5 - width * scale) * 0.5 + if right { A3_WIDTH * 0.5 } else { 0.0 };
        matrix[5] += (A3_HEIGHT - height * scale) * 0.5;
        Ok(Self {
            page,
            crop,
            matrix,
            scale,
        })
    }

    fn point(self, x: f32, y: f32) -> (f32, f32) {
        let [a, b, c, d, e, f] = self.matrix;
        (a * x + c * y + e, b * x + d * y + f)
    }

    fn rect(self, r: [f32; 4]) -> [f32; 4] {
        let points = [
            self.point(r[0], r[1]),
            self.point(r[0], r[3]),
            self.point(r[2], r[1]),
            self.point(r[2], r[3]),
        ];
        [
            points.iter().map(|p| p.0).fold(f32::INFINITY, f32::min),
            points.iter().map(|p| p.1).fold(f32::INFINITY, f32::min),
            points.iter().map(|p| p.0).fold(f32::NEG_INFINITY, f32::max),
            points.iter().map(|p| p.1).fold(f32::NEG_INFINITY, f32::max),
        ]
    }
}

fn rectangle(object: &Object) -> Result<[f32; 4]> {
    let values = object.as_array()?;
    if values.len() != 4 {
        return Err(DualError::Invalid("invalid rectangle".into()));
    }
    let r = [
        values[0].as_float()?,
        values[1].as_float()?,
        values[2].as_float()?,
        values[3].as_float()?,
    ];
    if r.iter().any(|v| !v.is_finite()) || r[2] <= r[0] || r[3] <= r[1] {
        return Err(DualError::Invalid("invalid rectangle dimensions".into()));
    }
    Ok(r)
}

fn resolve<'a>(doc: &'a Document, obj: &'a Object) -> Result<&'a Object> {
    Ok(doc.dereference(obj)?.1)
}

fn inherited(doc: &Document, mut page: ObjectId, key: &[u8]) -> Result<Option<Object>> {
    let mut seen = BTreeSet::new();
    while seen.insert(page) {
        let dict = doc.get_dictionary(page)?;
        if let Ok(value) = dict.get(key) {
            return Ok(Some(resolve(doc, value)?.clone()));
        }
        let Ok(parent) = dict.get(b"Parent") else {
            return Ok(None);
        };
        page = parent.as_reference()?;
    }
    Err(DualError::Invalid("cyclic page tree".into()))
}

fn page_form(doc: &Document, id: ObjectId, placement: Placement) -> Result<Stream> {
    let resources =
        inherited(doc, id, b"Resources")?.unwrap_or_else(|| Object::Dictionary(Dictionary::new()));
    let mut dict = dictionary! {
        "Type" => "XObject", "Subtype" => "Form", "FormType" => 1,
        "BBox" => numbers(placement.crop), "Resources" => resources,
    };
    if let Ok(group) = doc.get_dictionary(id)?.get(b"Group") {
        dict.set("Group", group.clone());
    }
    let mut content = Vec::new();
    for stream_id in doc.get_page_contents(id) {
        let stream = doc.get_object(stream_id)?.as_stream()?;
        content.extend(stream.get_plain_content()?);
        content.push(b'\n');
    }
    Ok(Stream::new(dict, content))
}

fn numbers<const N: usize>(values: [f32; N]) -> Object {
    Object::Array(values.into_iter().map(Object::Real).collect())
}

/// Additional export; source and mono files are never modified. Each input page
/// corresponds to one A3 sheet, including unchanged pages in a partial-page run.
pub fn export(source: &Path, translated: &Path, output: &Path) -> Result<()> {
    if [source, translated].iter().any(|p| {
        *p == output || (output.exists() && p.canonicalize().ok() == output.canonicalize().ok())
    }) {
        return Err(DualError::Invalid(
            "output must differ from both input PDFs".into(),
        ));
    }
    let mut left = Document::load(source)?;
    let mut right = Document::load(translated)?;
    if left.get_pages().is_empty() || left.get_pages().len() != right.get_pages().len() {
        return Err(DualError::Invalid(
            "source and translation page counts differ or are empty".into(),
        ));
    }
    right.renumber_objects_with(left.max_id + 1);
    let pairs: Vec<_> = left
        .get_pages()
        .values()
        .copied()
        .zip(right.get_pages().values().copied())
        .collect();
    let mut placements = BTreeMap::new();
    for &(l, r) in &pairs {
        placements.insert(l, Placement::new(&left, l, l, false)?);
        placements.insert(r, Placement::new(&right, r, l, true)?);
    }
    // The original catalog supplies one bookmark/name tree. Resolve right-side
    // named links before merging so equal destination names cannot jump left.
    navigation::resolve_named_links(&mut right)?;
    navigation::transform_destinations(&mut left, &placements)?;
    navigation::transform_destinations(&mut right, &placements)?;
    let mut sheets = Vec::new();
    for &(l, r) in &pairs {
        let lp = placements[&l];
        let rp = placements[&r];
        sheets.push((
            page_form(&left, l, lp)?,
            page_form(&right, r, rp)?,
            navigation::annotations(&left, l, lp)?,
            navigation::annotations(&right, r, rp)?,
        ));
    }
    left.max_id = right.max_id;
    left.objects.extend(right.objects);
    for ((l, r), (lf, rf, la, ra)) in pairs.into_iter().zip(sheets) {
        let lf = left.add_object(lf);
        let rf = left.add_object(rf);
        let mut operations = Vec::new();
        for (id, name) in [(l, b"Original".as_slice()), (r, b"Translated".as_slice())] {
            operations.push(lopdf::content::Operation::new("q", vec![]));
            operations.push(lopdf::content::Operation::new(
                "cm",
                placements[&id]
                    .matrix
                    .into_iter()
                    .map(Object::Real)
                    .collect(),
            ));
            operations.push(lopdf::content::Operation::new(
                "Do",
                vec![Object::Name(name.to_vec())],
            ));
            operations.push(lopdf::content::Operation::new("Q", vec![]));
        }
        let content = left.add_object(Stream::new(
            Dictionary::new(),
            lopdf::content::Content { operations }.encode()?,
        ));
        let annots: Vec<Object> = la
            .into_iter()
            .chain(ra)
            .map(|a| Object::Reference(left.add_object(a)))
            .collect();
        let parent = left.get_dictionary(l)?.get(b"Parent")?.clone();
        left.objects.insert(l, Object::Dictionary(dictionary! {
            "Type" => "Page", "Parent" => parent,
            "MediaBox" => numbers([0.,0.,A3_WIDTH,A3_HEIGHT]),
            "CropBox" => numbers([0.,0.,A3_WIDTH,A3_HEIGHT]), "Rotate" => 0, "UserUnit" => 1,
            "Resources" => dictionary! {"XObject" => dictionary! {"Original" => lf, "Translated" => rf}},
            "Contents" => content, "Annots" => annots,
        }));
    }
    left.prune_objects();
    crate::writer::save(&mut left, output)?;
    Ok(())
}
