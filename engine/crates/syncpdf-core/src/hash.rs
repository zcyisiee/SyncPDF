//! 内容哈希。阶段缓存键、翻译缓存键、资产寻址统一用 sha256。

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fmt;

/// 32 字节 sha256。serde 为 64 位十六进制字符串。
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct Sha256Hash(pub [u8; 32]);

impl Sha256Hash {
    pub fn of(bytes: impl AsRef<[u8]>) -> Self {
        Self(Sha256::digest(bytes.as_ref()).into())
    }

    /// 多段拼接哈希；段之间加长度前缀避免拼接歧义。
    pub fn of_parts<'a>(parts: impl IntoIterator<Item = &'a [u8]>) -> Self {
        let mut h = Sha256::new();
        for p in parts {
            h.update((p.len() as u64).to_le_bytes());
            h.update(p);
        }
        Self(h.finalize().into())
    }

    pub fn to_hex(&self) -> String {
        hex::encode(self.0)
    }

    pub fn as_bytes(&self) -> &[u8; 32] {
        &self.0
    }
}

impl fmt::Debug for Sha256Hash {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "sha256:{}", &self.to_hex()[..12])
    }
}

impl fmt::Display for Sha256Hash {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_hex())
    }
}

impl TryFrom<String> for Sha256Hash {
    type Error = String;
    fn try_from(s: String) -> Result<Self, Self::Error> {
        let bytes = hex::decode(&s).map_err(|e| format!("bad sha256 hex: {e}"))?;
        let arr: [u8; 32] = bytes
            .try_into()
            .map_err(|_| "sha256 must be 32 bytes".to_string())?;
        Ok(Self(arr))
    }
}

impl From<Sha256Hash> for String {
    fn from(h: Sha256Hash) -> Self {
        h.to_hex()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vector() {
        assert_eq!(
            Sha256Hash::of(b"abc").to_hex(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn parts_are_length_prefixed() {
        let a = Sha256Hash::of_parts([b"ab".as_slice(), b"c".as_slice()]);
        let b = Sha256Hash::of_parts([b"a".as_slice(), b"bc".as_slice()]);
        assert_ne!(a, b);
    }

    #[test]
    fn serde_roundtrip() {
        let h = Sha256Hash::of(b"x");
        let json = serde_json::to_string(&h).unwrap();
        assert_eq!(json.len(), 66);
        assert_eq!(serde_json::from_str::<Sha256Hash>(&json).unwrap(), h);
        assert!(serde_json::from_str::<Sha256Hash>("\"zz\"").is_err());
    }
}
