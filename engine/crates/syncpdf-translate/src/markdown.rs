//! Versioned, strict Markdown transport for one translated paragraph per block.
//!
//! A physical newline in body text is literal text. Only a backslash immediately
//! before a newline is a hard break. The newline before the end marker is framing
//! and is not part of the body.

use std::fmt;

use syncpdf_core::{AtomId, ParagraphId, StyleId};

use crate::unit::{ParsedUnit, Segment};

/// Include this in model prompt and cache identities when this transport is used.
pub const TRANSPORT_VERSION: &str = "syncpdf-markdown-v1";

/// A byte offset in the complete stream and a human-readable failure reason.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarkdownError {
    pub offset: usize,
    pub message: String,
}

impl MarkdownError {
    fn at(offset: usize, message: impl Into<String>) -> Self {
        Self {
            offset,
            message: message.into(),
        }
    }
}

impl fmt::Display for MarkdownError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Markdown byte {}: {}", self.offset, self.message)
    }
}

impl std::error::Error for MarkdownError {}

/// Serialize one unit, including both exclusive marker lines.
pub fn serialize(unit: &ParsedUnit) -> String {
    let mut out = format!("<!-- syncpdf:block {} -->\n", unit.id);
    write_segments(&unit.segments, &mut out);
    out.push('\n');
    out.push_str(&format!("<!-- syncpdf:end {} -->", unit.id));
    out
}

fn write_segments(segments: &[Segment], out: &mut String) {
    for segment in segments {
        match segment {
            Segment::Text(text) => {
                for ch in text.chars() {
                    if ch.is_ascii_punctuation() {
                        out.push('\\');
                    }
                    out.push(ch);
                }
            }
            Segment::Style { id, inner } => {
                out.push('[');
                write_segments(inner, out);
                out.push_str(&format!("]{{style={}}}", id.0));
            }
            Segment::Atom(id) => out.push_str(&format!("{{{{KEEP_{}}}}}", id.0)),
            Segment::Br => out.push_str("\\\n"),
        }
    }
}

fn is_reserved(ch: char) -> bool {
    matches!(
        ch,
        '\\' | '[' | ']' | '{' | '}' | '<' | '>' | '*' | '_' | '`'
    )
}

/// Parse exactly one block; surrounding whitespace is allowed.
pub fn parse(input: &str) -> Result<ParsedUnit, MarkdownError> {
    let mut stream = MarkdownStream::new();
    let mut results = stream.push(input);
    results.extend(stream.finish());
    if results.len() != 1 {
        if let Some(err) = results.iter().find_map(|r| r.as_ref().err()) {
            return Err(err.clone());
        }
        return Err(MarkdownError::at(0, "expected exactly one block"));
    }
    results.pop().expect("one result")
}

#[derive(Debug, Clone)]
struct Active {
    id: ParagraphId,
    body: String,
    body_offset: usize,
}

/// Incremental parser. Results are ordered; a malformed later block never erases
/// an already delivered block. Repeated IDs remain repeated for Engine validation.
#[derive(Debug, Clone, Default)]
pub struct MarkdownStream {
    pending: String,
    offset: usize,
    active: Option<Active>,
    outside_error: Option<MarkdownError>,
    after_end: bool,
}

impl MarkdownStream {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&mut self, delta: &str) -> Vec<Result<ParsedUnit, MarkdownError>> {
        self.pending.push_str(delta);
        self.drain(false)
    }

    pub fn finish(mut self) -> Vec<Result<ParsedUnit, MarkdownError>> {
        self.drain(true)
    }

    fn drain(&mut self, eof: bool) -> Vec<Result<ParsedUnit, MarkdownError>> {
        let mut out = Vec::new();
        loop {
            if self.after_end {
                if self.pending.starts_with('\n') {
                    self.consume(1);
                    self.after_end = false;
                    continue;
                }
                if self.pending.starts_with("\r\n") {
                    self.consume(2);
                    self.after_end = false;
                    continue;
                }
                if self.pending.is_empty() || (self.pending == "\r" && !eof) {
                    break;
                }
                if self.outside_error.is_none() {
                    self.outside_error = Some(MarkdownError::at(
                        self.offset,
                        "end marker has non-whitespace on the same line",
                    ));
                }
                self.after_end = false;
            }
            // A complete end marker is a commit point, including when no newline
            // has arrived yet. Any subsequent same-line material is handled outside.
            if self.active.is_some() {
                if let Some((id, len)) = marker_prefix(&self.pending, "end") {
                    let start = self.offset;
                    self.consume(len);
                    self.after_end = true;
                    let active = self.active.take().expect("checked active");
                    if id != active.id {
                        out.push(Err(MarkdownError::at(
                            start,
                            format!("end ID {id} does not match {}", active.id),
                        )));
                    } else {
                        out.push(parse_body(active));
                    }
                    continue;
                }
            }
            let line_len = self
                .pending
                .find('\n')
                .map(|i| i + 1)
                .or_else(|| eof.then_some(self.pending.len()));
            let Some(len) = line_len else {
                break;
            };
            if len == 0 {
                break;
            }
            let line = self.pending[..len].to_owned();
            let start = self.offset;
            self.consume(len);
            let logical = line.strip_suffix('\n').unwrap_or(&line);
            let logical = logical.strip_suffix('\r').unwrap_or(logical);
            if let Some(active) = &mut self.active {
                // A malformed or unknown marker is part of the block's invalid
                // body, so it is reported at this block's eventual close/EOF.
                active.body.push_str(&line);
            } else if let Some(id) = exact_marker(logical, "block") {
                if let Some(err) = self.outside_error.take() {
                    out.push(Err(err));
                }
                self.active = Some(Active {
                    id,
                    body: String::new(),
                    body_offset: self.offset,
                });
            } else if !line.trim().is_empty() && self.outside_error.is_none() {
                self.outside_error = Some(MarkdownError::at(
                    start,
                    "non-whitespace outside block or invalid block marker",
                ));
            }
        }
        if eof {
            if let Some(active) = self.active.take() {
                out.push(Err(MarkdownError::at(
                    active.body_offset,
                    format!("unterminated block {}", active.id),
                )));
            }
            if let Some(err) = self.outside_error.take() {
                out.push(Err(err));
            }
        }
        out
    }

    fn consume(&mut self, len: usize) {
        self.pending.drain(..len);
        self.offset += len;
    }
}

fn exact_marker(line: &str, kind: &str) -> Option<ParagraphId> {
    let id = line
        .strip_prefix(&format!("<!-- syncpdf:{kind} "))?
        .strip_suffix(" -->")?;
    let parsed: ParagraphId = id.parse().ok()?;
    (parsed.to_string() == id).then_some(parsed)
}

fn marker_prefix(input: &str, kind: &str) -> Option<(ParagraphId, usize)> {
    input.strip_prefix(&format!("<!-- syncpdf:{kind} "))?;
    let end = input.find(" -->")? + 4;
    exact_marker(&input[..end], kind).map(|id| (id, end))
}

fn parse_body(mut active: Active) -> Result<ParsedUnit, MarkdownError> {
    // The last newline separates body from its exclusive end-marker line.
    if !active.body.is_empty() && !active.body.ends_with('\n') {
        return Err(MarkdownError::at(
            active.body_offset + active.body.len(),
            "end marker must occupy its own line",
        ));
    }
    active.body.pop();
    let segments = parse_segments(&active.body, active.body_offset, false)?;
    Ok(ParsedUnit {
        id: active.id,
        segments,
    })
}

fn parse_segments(body: &str, base: usize, in_style: bool) -> Result<Vec<Segment>, MarkdownError> {
    let mut segments = Vec::new();
    let mut text = String::new();
    let mut pos = 0;
    while pos < body.len() {
        let rest = &body[pos..];
        if rest.starts_with("\\\r\n") || rest.starts_with("\\\n") {
            flush_text(&mut text, &mut segments);
            segments.push(Segment::Br);
            pos += if rest.starts_with("\\\r\n") { 3 } else { 2 };
        } else if let Some(escaped) = rest.strip_prefix('\\') {
            let Some(ch) = escaped.chars().next() else {
                return Err(MarkdownError::at(base + pos, "trailing escape"));
            };
            if !ch.is_ascii_punctuation() {
                return Err(MarkdownError::at(base + pos, "unknown escape"));
            }
            text.push(ch);
            pos += 1 + ch.len_utf8();
        } else if rest.starts_with("{{KEEP_") {
            flush_text(&mut text, &mut segments);
            let Some(end) = rest.find("}}") else {
                return Err(MarkdownError::at(base + pos, "unterminated atom"));
            };
            let number = &rest[7..end];
            let id = parse_number(number)
                .ok_or_else(|| MarkdownError::at(base + pos, "invalid atom ID"))?;
            segments.push(Segment::Atom(AtomId(id)));
            pos += end + 2;
        } else if rest.starts_with('[') {
            if in_style {
                return Err(MarkdownError::at(base + pos, "nested style"));
            }
            flush_text(&mut text, &mut segments);
            let mut end = pos + 1;
            while end < body.len() {
                let r = &body[end..];
                if let Some(escaped) = r.strip_prefix('\\') {
                    let Some(ch) = escaped.chars().next() else {
                        return Err(MarkdownError::at(base + end, "trailing escape"));
                    };
                    end += 1 + ch.len_utf8();
                } else if r.starts_with(']') {
                    break;
                } else {
                    end += r.chars().next().expect("nonempty").len_utf8();
                }
            }
            if end == body.len() {
                return Err(MarkdownError::at(base + pos, "unclosed style"));
            }
            let after = &body[end + 1..];
            let Some(attr_end) = after.find('}') else {
                return Err(MarkdownError::at(base + end + 1, "missing style attribute"));
            };
            let attr = &after[..attr_end];
            let number = attr
                .strip_prefix("{style=")
                .and_then(parse_number)
                .ok_or_else(|| {
                    MarkdownError::at(base + end + 1, "unknown or invalid style attribute")
                })?;
            let inner = parse_segments(&body[pos + 1..end], base + pos + 1, true)?;
            segments.push(Segment::Style {
                id: StyleId(number),
                inner,
            });
            pos = end + 1 + attr_end + 1;
        } else {
            let ch = rest.chars().next().expect("nonempty");
            if is_reserved(ch) {
                return Err(MarkdownError::at(
                    base + pos,
                    format!("unescaped Markdown character {ch:?}"),
                ));
            }
            text.push(ch);
            pos += ch.len_utf8();
        }
    }
    flush_text(&mut text, &mut segments);
    Ok(segments)
}

fn flush_text(text: &mut String, segments: &mut Vec<Segment>) {
    if !text.is_empty() {
        segments.push(Segment::Text(std::mem::take(text)));
    }
}

fn parse_number(s: &str) -> Option<u32> {
    if s.is_empty() || s.starts_with('0') || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    s.parse().ok().filter(|n| *n > 0)
}
