//! 流式块解析：把模型的增量输出切成一个个完整的 `<p id="…">…</p>`。
//!
//! 设计基准：02-技术路径与架构.md §5（one-shot + 流式）。上游据此「边译边编译」，
//! 所以本模块只做一件事：**只要缓冲里凑出一个完整块就立刻吐出来**，且对 delta
//! 在任意字节位置切开都必须给出同一串块。
//!
//! 解析器只认块边界，不判块内是否合法——合法性交给 `validate`。模型常见的
//! 前后缀垃圾（``` 围栏、"Here are the translations:"、说明文字）在块外被丢弃。

use syncpdf_core::ParagraphId;

/// 一个从流里切出来的原始块（尚未校验）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawBlock {
    pub id: ParagraphId,
    /// 含首尾标签的完整 `<p id="…">…</p>`。
    pub html: String,
}

/// 块外缓冲最多保留的字节数：足够容下一个被切开的 `<p id="…"` 开标签。
const MAX_PENDING_OPEN: usize = 256;

#[derive(Debug, Clone, PartialEq, Eq)]
enum State {
    /// 块外：正在找下一个 `<p id="…">`。
    Outside,
    /// 块内：已确定 id，正在找 `</p>`。
    Inside { id: ParagraphId, body_start: usize },
}

/// 增量块提取器。
///
/// ```ignore
/// let mut s = BlockStream::new();
/// for d in deltas { for block in s.push(d) { /* 立刻校验并回写 */ } }
/// let (tail, residue) = s.finish();
/// ```
#[derive(Debug, Clone)]
pub struct BlockStream {
    buf: String,
    state: State,
}

impl Default for BlockStream {
    fn default() -> Self {
        Self::new()
    }
}

impl BlockStream {
    pub fn new() -> Self {
        Self {
            buf: String::new(),
            state: State::Outside,
        }
    }

    /// 喂入一段增量文本，返回本次新凑齐的块（可能为空、也可能一次多块）。
    pub fn push(&mut self, delta: &str) -> Vec<RawBlock> {
        self.buf.push_str(delta);
        self.drain()
    }

    /// 收尾：吐出剩余的完整块，并返回残余。
    ///
    /// 残余只可能是「开了 `<p>` 但没等到 `</p>`」的半个块（供上游报 issue）；
    /// 块外的说明文字、``` 围栏等垃圾在流过程中就被丢掉了，不进残余。
    pub fn finish(mut self) -> (Vec<RawBlock>, String) {
        let blocks = self.drain();
        let residue = match self.state {
            // 块内残余：连开标签一起还回去，方便定位问题。
            State::Inside { .. } => std::mem::take(&mut self.buf),
            State::Outside => std::mem::take(&mut self.buf),
        };
        (blocks, residue)
    }

    /// 反复推进状态机，直到缓冲里再也凑不出完整块。
    fn drain(&mut self) -> Vec<RawBlock> {
        let mut out = Vec::new();
        loop {
            match &self.state {
                State::Outside => {
                    let Some(i) = self.buf.find("<p") else {
                        // 没有 `<`，整段都是块外垃圾；留一个尾字节以防 `<` 被切开。
                        self.trim_outside();
                        break;
                    };
                    if i > 0 {
                        self.buf.drain(..i);
                    }
                    // 现在 buf 以 `<p` 开头；要完整开标签才能定 id。
                    let Some(gt) = self.buf.find('>') else {
                        if self.buf.len() > MAX_PENDING_OPEN {
                            // 明显不是开标签（比如正文里的 `<p` 后面跟了一大段），
                            // 跳过这个 `<` 继续找。
                            self.buf.drain(..1);
                            continue;
                        }
                        break;
                    };
                    match parse_open_tag(&self.buf[..=gt]) {
                        Some(id) => {
                            self.state = State::Inside {
                                id,
                                body_start: gt + 1,
                            };
                        }
                        // `<pre>`、`<p>`（无 id）之类：跳过这个 `<` 继续找。
                        None => {
                            self.buf.drain(..1);
                        }
                    }
                }
                State::Inside { id, body_start } => {
                    let Some(rel) = self.buf[*body_start..].find("</p>") else {
                        break;
                    };
                    let end = *body_start + rel + "</p>".len();
                    out.push(RawBlock {
                        id: id.clone(),
                        html: self.buf[..end].to_string(),
                    });
                    self.buf.drain(..end);
                    self.state = State::Outside;
                }
            }
        }
        out
    }

    /// 块外且确定没有开标签起点时，丢掉垃圾但保住可能被切开的 `<` 前缀。
    fn trim_outside(&mut self) {
        let keep = if self.buf.ends_with('<') { 1 } else { 0 };
        let cut = self.buf.len() - keep;
        if cut > 0 {
            self.buf.drain(..cut);
        }
    }
}

/// 解析 `<p id="…">`（允许标签内多余空白），返回段落 id。
///
/// 这里比 `unit::parse_unit_html` 宽松一点：块边界识别不该因为模型多打一个空格
/// 就整块丢失；标签本身的严格性由后续 `validate` 负责。
fn parse_open_tag(tag: &str) -> Option<ParagraphId> {
    let inner = tag.strip_prefix("<p")?.strip_suffix('>')?;
    let inner = inner.strip_suffix('/').unwrap_or(inner);
    let trimmed = inner.trim_start();
    // `<p` 与属性之间必须有空白，否则是 `<pre …>` 之类。
    if trimmed.len() == inner.len() && !inner.is_empty() {
        return None;
    }
    let attr = trimmed.trim_start().strip_prefix("id")?.trim_start();
    let value = attr.strip_prefix('=')?.trim_start();
    let (quote, rest) = match value.as_bytes().first()? {
        b'"' => ('"', &value[1..]),
        b'\'' => ('\'', &value[1..]),
        _ => return None,
    };
    let end = rest.find(quote)?;
    rest[..end].trim().parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    const DOC: &str = concat!(
        "Sure, here you go:\n```html\n",
        r#"<p id="P01-001">First <span data-style="1">block</span>.</p>"#,
        "\n",
        r#"<p id="P01-002">Second {{KEEP_1}} block.</p>"#,
        "\n",
        r#"<p id="P02-001">Third block.</p>"#,
        "\n```\nDone.",
    );

    fn expected() -> Vec<RawBlock> {
        vec![
            RawBlock {
                id: "P01-001".parse().unwrap(),
                html: r#"<p id="P01-001">First <span data-style="1">block</span>.</p>"#.into(),
            },
            RawBlock {
                id: "P01-002".parse().unwrap(),
                html: r#"<p id="P01-002">Second {{KEEP_1}} block.</p>"#.into(),
            },
            RawBlock {
                id: "P02-001".parse().unwrap(),
                html: r#"<p id="P02-001">Third block.</p>"#.into(),
            },
        ]
    }

    /// 按给定分片方案喂入，返回块序列与残余。
    fn run(chunks: impl Iterator<Item = String>) -> (Vec<RawBlock>, String) {
        let mut s = BlockStream::new();
        let mut out = Vec::new();
        for c in chunks {
            out.extend(s.push(&c));
        }
        let (tail, residue) = s.finish();
        out.extend(tail);
        (out, residue)
    }

    #[test]
    fn whole_document_at_once() {
        let (blocks, residue) = run(std::iter::once(DOC.to_string()));
        assert_eq!(blocks, expected());
        // 块外垃圾（围栏、结语）被丢弃，残余只承载「没闭合的块」。
        assert_eq!(residue, "");
    }

    #[test]
    fn byte_by_byte_gives_the_same_blocks() {
        // 逐字符（UTF-8 边界安全）喂入。
        let chunks = DOC.chars().map(|c| c.to_string()).collect::<Vec<_>>();
        let (blocks, _) = run(chunks.into_iter());
        assert_eq!(blocks, expected());
    }

    #[test]
    fn every_split_point_gives_the_same_blocks() {
        // 所有二切点。
        let idx: Vec<usize> = (0..=DOC.len())
            .filter(|i| DOC.is_char_boundary(*i))
            .collect();
        for i in idx {
            let (blocks, _) = run([DOC[..i].to_string(), DOC[i..].to_string()].into_iter());
            assert_eq!(blocks, expected(), "split at {i}");
        }
    }

    #[test]
    fn fixed_size_chunks_of_every_width() {
        for w in 1..=37 {
            let mut chunks = Vec::new();
            let mut start = 0;
            while start < DOC.len() {
                let mut end = (start + w).min(DOC.len());
                while !DOC.is_char_boundary(end) {
                    end += 1;
                }
                chunks.push(DOC[start..end].to_string());
                start = end;
            }
            let (blocks, _) = run(chunks.into_iter());
            assert_eq!(blocks, expected(), "chunk width {w}");
        }
    }

    #[test]
    fn unterminated_block_is_residue_not_a_block() {
        let (blocks, residue) = run(std::iter::once(
            r#"<p id="P01-001">done</p><p id="P01-002">half"#.to_string(),
        ));
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].id, "P01-001".parse().unwrap());
        assert_eq!(residue, r#"<p id="P01-002">half"#);
    }

    #[test]
    fn junk_between_blocks_is_dropped_and_buffer_stays_bounded() {
        let mut s = BlockStream::new();
        for _ in 0..1000 {
            assert!(s.push("noise noise noise ").is_empty());
        }
        assert!(
            s.buf.len() <= MAX_PENDING_OPEN,
            "buffer grew: {}",
            s.buf.len()
        );
        let blocks = s.push(r#"<p id="P03-007">ok</p>"#);
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].html, r#"<p id="P03-007">ok</p>"#);
    }

    #[test]
    fn other_p_like_tags_do_not_start_a_block() {
        let (blocks, _) = run(std::iter::once(
            r#"<pre>x</pre><p>no id</p><p id="nope">bad</p><p id="P01-009">good</p>"#.to_string(),
        ));
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].id, "P01-009".parse().unwrap());
    }

    #[test]
    fn open_tag_whitespace_and_quotes_are_tolerated() {
        let (blocks, _) = run(std::iter::once(
            "<p  id = 'P01-001' >a</p><p\nid=\"P01-002\">b</p>".to_string(),
        ));
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[1].id, "P01-002".parse().unwrap());
    }

    #[test]
    fn empty_stream_yields_nothing() {
        let (blocks, residue) = run(std::iter::empty());
        assert!(blocks.is_empty());
        assert!(residue.is_empty());
    }
}
