//! 翻译缓存（SQLite）。表结构照 02-技术路径与架构.md §5.5 / hjfy 共享缓存。
//!
//! 键是 `(source_language, target_language, sha256(transport_version + source_html))`；命中即跳过 LLM。
//! 用户手工编辑的译文写回同表并标 `origin='manual'`，优先级最高（`put_manual`）。

use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension};
use syncpdf_core::hash::Sha256Hash;

fn transport_key(source_html: &str) -> Sha256Hash {
    Sha256Hash::of(format!("{}\0{source_html}", crate::markdown::TRANSPORT_VERSION).as_bytes())
}

/// 手工编辑译文的 origin 标记；命中后不会被自动翻译覆盖。
pub const ORIGIN_MANUAL: &str = "manual";

#[derive(Debug, thiserror::Error)]
pub enum CacheError {
    #[error("translation cache error: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

pub type Result<T> = std::result::Result<T, CacheError>;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS translations(
  source_language TEXT NOT NULL,
  target_language TEXT NOT NULL,
  source_hash BLOB NOT NULL CHECK(length(source_hash) = 32),
  origin TEXT NOT NULL,
  source_html TEXT NOT NULL,
  translated_html TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(source_language, target_language, source_hash)
) WITHOUT ROWID;
";

/// upsert：译文或 origin 变化才更新；`origin='manual'` 的行不被自动译文覆盖。
const UPSERT: &str = "
INSERT INTO translations
  (source_language, target_language, source_hash, origin, source_html, translated_html, updated_at)
VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
ON CONFLICT(source_language, target_language, source_hash) DO UPDATE SET
  origin = excluded.origin,
  source_html = excluded.source_html,
  translated_html = excluded.translated_html,
  updated_at = excluded.updated_at
WHERE (translations.translated_html <> excluded.translated_html
       OR translations.origin <> excluded.origin)
  AND (translations.origin <> ?8 OR excluded.origin = ?8)
";

/// 翻译缓存连接。单线程持有；跨线程用各自的连接打开同一文件。
#[derive(Debug)]
pub struct Cache(Connection);

impl Cache {
    /// 打开（必要时创建）缓存库。
    pub fn open(path: &Path) -> Result<Self> {
        let conn = Connection::open(path)?;
        // WAL 让「边译边写缓存」与读互不阻塞；内存库不支持 WAL，失败可忽略。
        let _ = conn.query_row("PRAGMA journal_mode=WAL", [], |_| Ok(()));
        Self::init(conn)
    }

    /// 内存库，测试与「不落盘」场景用。
    pub fn open_in_memory() -> Result<Self> {
        Self::init(Connection::open_in_memory()?)
    }

    fn init(conn: Connection) -> Result<Self> {
        conn.execute_batch("PRAGMA synchronous=NORMAL;")?;
        conn.execute_batch(SCHEMA)?;
        conn.pragma_update(None, "user_version", 1)?;
        Ok(Self(conn))
    }

    /// 查缓存。命中返回译文 HTML。
    pub fn get(&self, src_lang: &str, tgt_lang: &str, source_html: &str) -> Result<Option<String>> {
        let h = transport_key(source_html);
        let found = self
            .0
            .query_row(
                "SELECT translated_html FROM translations \
                 WHERE source_language=?1 AND target_language=?2 AND source_hash=?3",
                params![src_lang, tgt_lang, &h.as_bytes()[..]],
                |r| r.get::<_, String>(0),
            )
            .optional()?;
        // 空译文当作未命中：空是「翻译失败」的落定，不该跳过重试。
        Ok(found.filter(|s| !s.is_empty()))
    }

    /// 写缓存。同键已存在时按 hjfy 语义 upsert（译文或 origin 变化才更新
    /// `updated_at`），但 `origin='manual'` 的行不会被自动译文覆盖。
    pub fn put(
        &self,
        src_lang: &str,
        tgt_lang: &str,
        origin: &str,
        source_html: &str,
        translated_html: &str,
    ) -> Result<()> {
        let h = transport_key(source_html);
        let now = now_unix();
        self.0.execute(
            UPSERT,
            params![
                src_lang,
                tgt_lang,
                &h.as_bytes()[..],
                origin,
                source_html,
                translated_html,
                now,
                ORIGIN_MANUAL,
            ],
        )?;
        Ok(())
    }

    /// 写入用户手工编辑的译文（优先级最高）。
    pub fn put_manual(
        &self,
        src_lang: &str,
        tgt_lang: &str,
        source_html: &str,
        translated_html: &str,
    ) -> Result<()> {
        self.put(
            src_lang,
            tgt_lang,
            ORIGIN_MANUAL,
            source_html,
            translated_html,
        )
    }

    /// 命中行的 origin，供报告区分自动译 / 手工译。
    pub fn origin_of(
        &self,
        src_lang: &str,
        tgt_lang: &str,
        source_html: &str,
    ) -> Result<Option<String>> {
        let h = transport_key(source_html);
        Ok(self
            .0
            .query_row(
                "SELECT origin FROM translations \
                 WHERE source_language=?1 AND target_language=?2 AND source_hash=?3",
                params![src_lang, tgt_lang, &h.as_bytes()[..]],
                |r| r.get::<_, String>(0),
            )
            .optional()?)
    }

    /// 行数，测试与报告用。
    pub fn len(&self) -> Result<u64> {
        let n: i64 = self
            .0
            .query_row("SELECT COUNT(*) FROM translations", [], |r| r.get(0))?;
        Ok(n.max(0) as u64)
    }

    pub fn is_empty(&self) -> Result<bool> {
        Ok(self.len()? == 0)
    }
}

fn now_unix() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_transport_hash_cannot_hit_markdown_cache() {
        let cache = Cache::open_in_memory().unwrap();
        let source = "<p id=\"P01-001\">source</p>";
        let legacy = Sha256Hash::of(source.as_bytes());
        cache
            .0
            .execute(
                "INSERT INTO translations VALUES (?1, ?2, ?3, 'legacy', ?4, 'old translation', 0)",
                params!["en", "zh-CN", &legacy.as_bytes()[..], source],
            )
            .unwrap();
        assert!(cache.get("en", "zh-CN", source).unwrap().is_none());
        cache
            .put("en", "zh-CN", "markdown", source, "new translation")
            .unwrap();
        assert_eq!(
            cache.get("en", "zh-CN", source).unwrap().as_deref(),
            Some("new translation")
        );
    }

    #[test]
    fn miss_then_hit() {
        let c = Cache::open_in_memory().unwrap();
        let src = "<p id=\"P01-001\">hello</p>";
        assert_eq!(c.get("en", "zh", src).unwrap(), None);
        c.put("en", "zh", "pi/deepseek", src, "<p id=\"P01-001\">你好</p>")
            .unwrap();
        assert_eq!(
            c.get("en", "zh", src).unwrap().as_deref(),
            Some("<p id=\"P01-001\">你好</p>")
        );
        assert_eq!(c.len().unwrap(), 1);
    }

    #[test]
    fn key_includes_both_languages_and_source() {
        let c = Cache::open_in_memory().unwrap();
        let src = "<p id=\"P01-001\">hello</p>";
        c.put("en", "zh", "x", src, "A").unwrap();
        assert_eq!(c.get("en", "ja", src).unwrap(), None);
        assert_eq!(c.get("de", "zh", src).unwrap(), None);
        assert_eq!(c.get("en", "zh", "<p id=\"P01-001\">hi</p>").unwrap(), None);
    }

    #[test]
    fn empty_translation_is_not_a_hit() {
        let c = Cache::open_in_memory().unwrap();
        let src = "<p id=\"P01-001\">hello</p>";
        c.put("en", "zh", "x", src, "").unwrap();
        assert_eq!(c.get("en", "zh", src).unwrap(), None);
    }

    #[test]
    fn manual_origin_wins_over_automatic_rewrite() {
        let c = Cache::open_in_memory().unwrap();
        let src = "<p id=\"P01-001\">hello</p>";
        c.put_manual("en", "zh", src, "手工").unwrap();
        c.put("en", "zh", "pi/deepseek", src, "自动").unwrap();
        assert_eq!(c.get("en", "zh", src).unwrap().as_deref(), Some("手工"));
        assert_eq!(
            c.origin_of("en", "zh", src).unwrap().as_deref(),
            Some(ORIGIN_MANUAL)
        );
        // 再次手工编辑可以覆盖。
        c.put_manual("en", "zh", src, "手工2").unwrap();
        assert_eq!(c.get("en", "zh", src).unwrap().as_deref(), Some("手工2"));
    }

    #[test]
    fn upsert_updates_automatic_rows() {
        let c = Cache::open_in_memory().unwrap();
        let src = "<p id=\"P01-001\">hello</p>";
        c.put("en", "zh", "pi/a", src, "A").unwrap();
        c.put("en", "zh", "pi/b", src, "B").unwrap();
        assert_eq!(c.len().unwrap(), 1);
        assert_eq!(c.get("en", "zh", src).unwrap().as_deref(), Some("B"));
        assert_eq!(
            c.origin_of("en", "zh", src).unwrap().as_deref(),
            Some("pi/b")
        );
    }

    #[test]
    fn survives_reopen_on_disk() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("translations.sqlite3");
        let src = "<p id=\"P01-001\">hello</p>";
        {
            let c = Cache::open(&path).unwrap();
            assert!(c.is_empty().unwrap());
            c.put("en", "zh", "pi", src, "你好").unwrap();
        }
        let c = Cache::open(&path).unwrap();
        assert_eq!(c.get("en", "zh", src).unwrap().as_deref(), Some("你好"));
    }
}
