//! 稳定标识。所有跨阶段引用只用这些 id，禁止持有指针。

use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

/// 页索引，0 基。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct PageId(pub u32);

impl PageId {
    /// 1 基页号，用于面向用户的展示与 `ParagraphId`。
    pub fn number(self) -> u32 {
        self.0 + 1
    }
}

/// PDF 间接对象引用 `(obj, gen)`。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ObjRef {
    pub obj: u32,
    pub gen: u16,
}

impl ObjRef {
    pub const fn new(obj: u32, gen: u16) -> Self {
        Self { obj, gen }
    }
}

impl fmt::Display for ObjRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} {} R", self.obj, self.gen)
    }
}

/// 内容流操作键：`(流对象, 操作序号)`。与 hjfy 补丁键一致。
/// Form XObject 内的操作用 XObject 自身流的对象引用。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct OpKey {
    pub stream: ObjRef,
    pub op_index: u32,
}

impl OpKey {
    pub const fn new(stream: ObjRef, op_index: u32) -> Self {
        Self { stream, op_index }
    }
}

/// 源字形的稳定 id：页 + 操作键 + 该操作内的字形序号。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct GlyphId {
    pub page: PageId,
    pub op: OpKey,
    pub ordinal: u16,
}

impl fmt::Display for GlyphId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "p{}:{}_{}:{}:{}",
            self.page.0, self.op.stream.obj, self.op.stream.gen, self.op.op_index, self.ordinal
        )
    }
}

/// 段落身份 `P{page:02}-{seq:03}`。模型可见，不得重编号。
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct ParagraphId {
    /// 1 基页号。
    pub page: u32,
    /// 页内 1 基序号。
    pub seq: u32,
}

impl ParagraphId {
    pub fn new(page: PageId, seq: u32) -> Self {
        Self {
            page: page.number(),
            seq,
        }
    }
}

impl fmt::Display for ParagraphId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "P{:02}-{:03}", self.page, self.seq)
    }
}

impl fmt::Debug for ParagraphId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "ParagraphId({self})")
    }
}

/// `ParagraphId` 解析错误。
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("invalid paragraph id: {0:?}")]
pub struct ParseParagraphIdError(pub String);

impl FromStr for ParagraphId {
    type Err = ParseParagraphIdError;

    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        let err = || ParseParagraphIdError(s.to_owned());
        let rest = s.strip_prefix('P').ok_or_else(err)?;
        let (page, seq) = rest.split_once('-').ok_or_else(err)?;
        if page.len() < 2 || seq.len() < 3 {
            return Err(err());
        }
        let page: u32 = page.parse().map_err(|_| err())?;
        let seq: u32 = seq.parse().map_err(|_| err())?;
        if page == 0 || seq == 0 {
            return Err(err());
        }
        Ok(Self { page, seq })
    }
}

impl TryFrom<String> for ParagraphId {
    type Error = ParseParagraphIdError;
    fn try_from(s: String) -> std::result::Result<Self, Self::Error> {
        s.parse()
    }
}

impl From<ParagraphId> for String {
    fn from(id: ParagraphId) -> Self {
        id.to_string()
    }
}

/// 段内原子序号，对应 `{{KEEP_n}}`，1 基。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct AtomId(pub u32);

impl fmt::Display for AtomId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{{{{KEEP_{}}}}}", self.0)
    }
}

/// 段内样式 run 序号，对应 `data-style="n"`，1 基。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct StyleId(pub u32);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paragraph_id_roundtrip() {
        let id = ParagraphId::new(PageId(0), 3);
        assert_eq!(id.to_string(), "P01-003");
        assert_eq!("P01-003".parse::<ParagraphId>().unwrap(), id);
        assert_eq!(
            "P12-169".parse::<ParagraphId>().unwrap(),
            ParagraphId { page: 12, seq: 169 }
        );
        assert_eq!(
            "P123-1000".parse::<ParagraphId>().unwrap(),
            ParagraphId {
                page: 123,
                seq: 1000
            }
        );
    }

    #[test]
    fn paragraph_id_rejects_garbage() {
        for bad in [
            "", "P1-001", "P01-01", "P00-001", "P01-000", "X01-001", "P01_001", "P0a-001",
        ] {
            assert!(bad.parse::<ParagraphId>().is_err(), "{bad:?} should fail");
        }
    }

    #[test]
    fn paragraph_id_serde_is_string() {
        let id: ParagraphId = "P02-010".parse().unwrap();
        let json = serde_json::to_string(&id).unwrap();
        assert_eq!(json, "\"P02-010\"");
        let back: ParagraphId = serde_json::from_str(&json).unwrap();
        assert_eq!(back, id);
        assert!(serde_json::from_str::<ParagraphId>("\"nope\"").is_err());
    }

    #[test]
    fn glyph_id_display_and_serde() {
        let g = GlyphId {
            page: PageId(2),
            op: OpKey::new(ObjRef::new(17, 0), 42),
            ordinal: 5,
        };
        assert_eq!(g.to_string(), "p2:17_0:42:5");
        let json = serde_json::to_string(&g).unwrap();
        let back: GlyphId = serde_json::from_str(&json).unwrap();
        assert_eq!(back, g);
    }

    #[test]
    fn atom_id_display_matches_protocol() {
        assert_eq!(AtomId(3).to_string(), "{{KEEP_3}}");
    }

    #[test]
    fn ids_order_by_page_then_op_then_ordinal() {
        let a = GlyphId {
            page: PageId(0),
            op: OpKey::new(ObjRef::new(9, 0), 1),
            ordinal: 9,
        };
        let b = GlyphId {
            page: PageId(0),
            op: OpKey::new(ObjRef::new(9, 0), 2),
            ordinal: 0,
        };
        let c = GlyphId {
            page: PageId(1),
            op: OpKey::new(ObjRef::new(1, 0), 0),
            ordinal: 0,
        };
        assert!(a < b && b < c);
    }
}
