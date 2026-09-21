//! 内置字体包加载（fontdb）。
//!
//! 字体包布局（`syncpdf_core::fixtures::fonts_dir()` 指向的目录）：
//! `notocjk/`、`inter/`、`pt/`、`arabic/` 四个子目录，每个子目录有
//! `resources.json` 描述 family/variant → (path, face_index)。CJK 的
//! `.ttc` face 0 JP / 1 KR / 2 SC / 3 TC。
//!
//! 本 crate 不用 fontdb 的 CSS `query`，而是自己实现 [`FontQuery`] 匹配
//! （script 感知：CJK 由查询脚本选 ttc face），因为内置包的 family 命名
//! 不足以表达 "同一 ttc 的不同 regional face"。

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::Deserialize;

use crate::{FontError, Result};

/// 字体在 [`FontStore`] 内的句柄。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct FontId(pub u32);

/// 目标脚本，用于匹配内置字体包的 face。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Script {
    /// 拉丁（Inter / PT）。
    Latin,
    /// 简体中文（notocjk ttc face 2）。
    HanSC,
    /// 繁体中文（face 3）。
    HanTC,
    /// 假名（日文，face 0）。
    Kana,
    /// 谚文（韩文，face 1）。
    Hangul,
    /// 阿拉伯（NotoSansArabic）。
    Arabic,
    /// 通用 / 数字 / 标点（回拉丁处理）。
    Common,
}

/// 字体查询条件。
#[derive(Debug, Clone)]
pub struct FontQuery {
    /// 优先 family（如 "Inter"、"noto sans cjk sc"）；None 时仅按其余字段匹配。
    pub family: Option<String>,
    /// 衬线（PTSerif / NotoSerifCJK）优先。
    pub serif: bool,
    /// 等宽优先（本字体包无等宽，仅作 flags 传递）。
    pub mono: bool,
    /// 期望字重（100-900）。
    pub weight: u16,
    /// 期望斜体。
    pub italic: bool,
    /// 目标脚本。
    pub script: Script,
}

impl Default for FontQuery {
    fn default() -> Self {
        Self {
            family: None,
            serif: false,
            mono: false,
            weight: 400,
            italic: false,
            script: Script::Latin,
        }
    }
}

/// 已加载的单一 face。
#[derive(Debug, Clone)]
pub struct LoadedFont {
    /// 字体句柄。
    pub id: FontId,
    /// 源文件路径。
    pub path: PathBuf,
    /// ttc 内 face 序号。
    pub face_index: u32,
    /// 解析出的主 family（英文名）。
    pub family: String,
    /// 字重 100-900。
    pub weight: u16,
    /// 是否斜体。
    pub italic: bool,
    /// 原始字体数据（整文件，含 ttc 头）。
    pub data: Arc<Vec<u8>>,
}

/// 内置字体包 + 可选系统字体的集合。
///
/// 线程安全：内部无共享可变状态，加载完成后只读。
#[derive(Debug)]
pub struct FontStore {
    fonts: Vec<LoadedFont>,
    /// family 小写 → 字体索引集合。
    by_family: HashMap<String, Vec<u32>>,
}

/// `resources.json` 结构（各包 schema 一致）。
#[derive(Debug, Deserialize)]
struct ResourcesJson {
    #[allow(dead_code)]
    package: String,
    #[serde(rename = "schema_version")]
    #[allow(dead_code)]
    schema_version: u32,
    fonts: HashMap<String, HashMap<String, FaceSpec>>,
}

/// 单个变体条目。
#[derive(Debug, Deserialize)]
struct FaceSpec {
    path: String,
    face_index: u32,
}

impl FontStore {
    /// 从内置字体包目录加载（`fonts_dir()` 给出的目录）。
    ///
    /// 目录缺失或没有可解析字体时报错。
    pub fn load_builtin(dir: &Path) -> Result<Self> {
        let mut store = Self {
            fonts: Vec::new(),
            by_family: HashMap::new(),
        };
        for sub in ["notocjk", "inter", "pt", "arabic"] {
            store.load_package(&dir.join(sub))?;
        }
        if store.fonts.is_empty() {
            return Err(FontError::InvalidPackage(format!(
                "no loadable fonts under {}",
                dir.display()
            )));
        }
        Ok(store)
    }

    /// 附加扫描系统字体（macOS 系统目录），返回 `self` 便于链式调用。
    ///
    /// 仅内置字体包不含的字形/字体时才需要。跳过无法解析的字体。
    pub fn with_system_fonts(mut self) -> Self {
        let mut db = fontdb::Database::new();
        db.load_system_fonts();
        for face in db.faces() {
            let Some((source, index)) = db.face_source(face.id) else {
                continue;
            };
            let path = match &source {
                fontdb::Source::File(p) => p.clone(),
                _ => continue,
            };
            let Some(data) = db.with_face_data(face.id, |data, _| data.to_vec()) else {
                continue;
            };
            let family = face
                .families
                .first()
                .map(|(name, _)| name.clone())
                .unwrap_or_default();
            let next = self.fonts.len() as u32;
            let id = FontId(next);
            self.index_family(&family, next);
            self.fonts.push(LoadedFont {
                id,
                path,
                face_index: index,
                family,
                weight: face.weight.0,
                italic: face.style == fontdb::Style::Italic,
                data: Arc::new(data),
            });
        }
        self
    }

    /// 取字体，越界返回 None。
    pub fn get(&self, id: FontId) -> Option<&LoadedFont> {
        self.fonts.get(id.0 as usize)
    }

    /// 按查询条件找最佳字体。
    ///
    /// 打分制：family 命中权重最高；script 决定 CJK face（SC/TC/Kana/Hangul
    /// 分别匹配 notocjk 中相应 face 的 family）；serif/italic 匹配再过滤；
    /// weight 取最近。返回 None 表示包里没有可接受的候选。
    pub fn find(&self, q: &FontQuery) -> Option<FontId> {
        let mut best: Option<(i32, u32)> = None;
        let target_family = q.family.as_deref().map(str::to_ascii_lowercase);
        for (idx, f) in self.fonts.iter().enumerate() {
            if !self.font_acceptable(f, q) {
                continue;
            }
            let mut score = 0;
            if let Some(tf) = &target_family {
                if self
                    .by_family
                    .get(tf.as_str())
                    .is_some_and(|idxs| idxs.contains(&(idx as u32)))
                {
                    score += 1000;
                }
            }
            // 斜体匹配加分（非斜体查询时排除斜体，反之优先真斜体）。
            if f.italic == q.italic {
                score += 500;
            }
            // 字重差惩罚（hjfy 同款最近字重）。
            score -= (f.weight as i32 - q.weight as i32).abs().min(900);
            if best.is_none_or(|(bs, _)| score > bs) {
                best = Some((score, idx as u32));
            }
        }
        best.map(|(_, idx)| FontId(idx))
    }

    /// 全部字体 id（按加载序）。
    pub fn ids(&self) -> Vec<FontId> {
        self.fonts.iter().map(|f| f.id).collect()
    }

    /// 字体总数。
    pub fn len(&self) -> usize {
        self.fonts.len()
    }

    /// 是否为空。
    pub fn is_empty(&self) -> bool {
        self.fonts.is_empty()
    }

    /// 按别名 family 查找（例如 profile 用）。
    pub fn find_by_family(&self, family: &str, weight: u16, italic: bool) -> Option<FontId> {
        let key = family.to_ascii_lowercase();
        let idxs = self.by_family.get(&key)?;
        let mut best: Option<(i32, u32)> = None;
        for &idx in idxs {
            let f = &self.fonts[idx as usize];
            let mut score = -(f.weight as i32 - weight as i32).abs().min(900);
            if f.italic == italic {
                score += 100;
            }
            if best.is_none_or(|(bs, _)| score > bs) {
                best = Some((score, idx));
            }
        }
        best.map(|(_, idx)| FontId(idx))
    }

    /// 加载一个字体包子目录（按 resources.json，缺 json 则扫描目录）。
    fn load_package(&mut self, dir: &Path) -> Result<()> {
        if !dir.is_dir() {
            // 单个包缺失不算错误（例如裁剪发行版），跳过。
            return Ok(());
        }
        let resources = dir.join("resources.json");
        if resources.is_file() {
            let text =
                std::fs::read_to_string(&resources).map_err(|e| FontError::io(&resources, e))?;
            let parsed: ResourcesJson = serde_json::from_str(&text)
                .map_err(|e| FontError::InvalidPackage(format!("{resources:?}: {e}")))?;
            let mut faces: Vec<(PathBuf, u32, String)> = Vec::new();
            for (family, variants) in &parsed.fonts {
                for spec in variants.values() {
                    // family key（如 sans-sc / inter / arabic）作为别名；
                    // 真实 family/weight/italic 由 name/OS2 表读取。
                    let path = dir.join(&spec.path);
                    faces.push((path, spec.face_index, family.clone()));
                }
            }
            for (path, face_index, key) in faces {
                self.load_face_from_spec(&path, face_index, &key)?;
            }
            return Ok(());
        }
        // 兜底：直接扫描目录里的字体文件。
        let entries = std::fs::read_dir(dir).map_err(|e| FontError::io(dir, e))?;
        let mut files: Vec<PathBuf> = entries
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| {
                matches!(
                    p.extension().and_then(|s| s.to_str()),
                    Some("ttf" | "otf" | "ttc" | "otc")
                )
            })
            .collect();
        files.sort();
        for path in files {
            let count = self.load_collection(&path, &None)?;
            if count == 0 {
                tracing::warn!(path = %path.display(), "no loadable face in font");
            }
        }
        Ok(())
    }

    /// 解析 resources.json 条目：读取文件，展开 ttc 全部 face 或单 face。
    fn load_face_from_spec(&mut self, path: &Path, face_index: u32, key: &str) -> Result<()> {
        let data = std::fs::read(path).map_err(|e| FontError::io(path, e))?;
        if path.extension().and_then(|s| s.to_str()) == Some("ttc") {
            // ttc：只取 spec 指定的 face，但用 key 命名 family 别名。
            self.push_face(path, face_index, &data, key)
        } else {
            self.push_face(path, 0, &data, key)
        }
    }

    /// 加载字体文件（单文件或 ttc 全 face），返回新增 face 数。
    fn load_collection(&mut self, path: &Path, key: &Option<String>) -> Result<usize> {
        let data = std::fs::read(path).map_err(|e| FontError::io(path, e))?;
        let count = fonts_in_collection(&data).unwrap_or(1) as usize;
        for index in 0..count {
            let family = key
                .clone()
                .or_else(|| read_family(&data, index as u32))
                .unwrap_or_else(|| {
                    path.file_stem()
                        .and_then(|s| s.to_str())
                        .unwrap_or("unknown")
                        .to_string()
                });
            self.push_face(path, index as u32, &data, &family)?;
        }
        Ok(count)
    }

    /// 注册一个 face（解析名称/字重/斜体）。
    fn push_face(&mut self, path: &Path, face_index: u32, data: &[u8], alias: &str) -> Result<()> {
        let font = skrifa::FontRef::from_index(data, face_index)
            .map_err(|e| FontError::Parse(format!("{path:?}: {e:?}")))?;
        let (family, weight, italic) = read_font_info(&font, alias);
        let next = self.fonts.len() as u32;
        let id = FontId(next);
        self.index_family(&family, next);
        // 别名（resources.json key，如 sans-sc / inter）也建立索引。
        if !alias.is_empty() && alias != family {
            self.index_family(alias, next);
        }
        self.fonts.push(LoadedFont {
            id,
            path: path.to_path_buf(),
            face_index,
            family,
            weight,
            italic,
            data: Arc::new(data.to_vec()),
        });
        Ok(())
    }

    fn index_family(&mut self, family: &str, idx: u32) {
        let key = family.to_ascii_lowercase();
        self.by_family.entry(key).or_default().push(idx);
    }

    /// 查询候选过滤：script → family 组；serif / italic 匹配。
    fn font_acceptable(&self, f: &LoadedFont, q: &FontQuery) -> bool {
        // CJK 家族必须按脚本选择对应 region face，避免 SC 查询拿到 JP face。
        let cjk = is_cjk_family(&f.family);
        if cjk {
            // serif / sans 先分流（Noto Serif CJK vs Noto Sans CJK），
            // 再按脚本选 region 后缀，否则 sans face 也会通过 serif 查询。
            let is_serif = f.family.starts_with("Noto Serif");
            if is_serif != q.serif {
                return false;
            }
            let region_ok = match q.script {
                Script::HanSC => f.family.ends_with("SC"),
                Script::HanTC => f.family.ends_with("TC"),
                Script::Kana => f.family.ends_with("JP"),
                Script::Hangul => f.family.ends_with("KR"),
                // Common：任意 region 可接受。
                Script::Common => true,
                // 拉丁/阿拉伯查询不选 CJK face。
                Script::Latin | Script::Arabic => false,
            };
            return region_ok;
        }
        match q.script {
            Script::Arabic => return f.family.contains("Arabic"),
            // 拉丁文本优先非 CJK 字体（上面已排除 CJK）。
            Script::Latin | Script::Common => {}
            // CJK 脚本但字体不是 CJK 家族（如 Inter）→ 不作为候选。
            Script::HanSC | Script::HanTC | Script::Kana | Script::Hangul => return false,
        }
        // serif 查询落在非 CJK 字体上：PTSerif 优先，Inter/Arabic 不匹配。
        if q.serif {
            return f.family.contains("Serif");
        }
        if q.mono {
            // 字体包无等宽，先接受（调用方负责降级）。
        }
        true
    }
}

/// 解析 face 的（family, weight, italic）。
///
/// resources.json 的 key（alias）只作为 family 兜底；真实名称优先，
/// 因为 CJK ttc 每个 face 的 name 表已带 region（Noto Sans CJK SC 等）。
fn read_font_info(font: &skrifa::FontRef<'_>, alias: &str) -> (String, u16, bool) {
    use read_fonts::TableProvider;
    use skrifa::MetadataProvider;
    let name_family = font
        .localized_strings(read_fonts::types::NameId::FAMILY_NAME)
        .english_or_first()
        .map(|s| s.to_string());
    let os2 = font.os2().ok();
    let weight = os2.as_ref().map(|t| t.us_weight_class()).unwrap_or(400);
    let italic = os2
        .as_ref()
        .map(|t| {
            t.fs_selection()
                .contains(read_fonts::tables::os2::SelectionFlags::ITALIC)
        })
        .unwrap_or(false);
    let family = match name_family {
        Some(f) if !f.is_empty() => f,
        _ => alias.to_string(),
    };
    (family, weight, italic)
}

/// 读 face 的英文 family 名。
fn read_family(data: &[u8], index: u32) -> Option<String> {
    let font = skrifa::FontRef::from_index(data, index).ok()?;
    use skrifa::MetadataProvider;
    font.localized_strings(read_fonts::types::NameId::FAMILY_NAME)
        .english_or_first()
        .map(|s| s.to_string())
}

/// ttc 内 face 数。
fn fonts_in_collection(data: &[u8]) -> Option<u32> {
    // 'ttcf' 标签。
    if data.len() >= 8 && &data[0..4] == b"ttcf" {
        let n = u32::from_be_bytes([data[4], data[5], data[6], data[7]]);
        Some(n)
    } else {
        None
    }
}

/// 是否 CJK 字体（Noto Sans/Serif CJK）。
fn is_cjk_family(family: &str) -> bool {
    let f = family.to_ascii_lowercase();
    f.contains("cjk") && (f.contains("noto") || f.contains("serif"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store() -> Option<FontStore> {
        let dir = syncpdf_core::fixtures::fonts_dir()?;
        FontStore::load_builtin(&dir).ok()
    }

    #[test]
    fn load_builtin_package() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        // 4 个包：notocjk 16 face + inter 4 + pt 8 + arabic 2 = 30。
        assert!(s.len() >= 24, "loaded {} faces", s.len());
        assert_eq!(s.ids().len(), s.len());
        // 全部 id 可 get。
        for id in s.ids() {
            let f = s.get(id).unwrap();
            assert!(!f.data.is_empty());
            assert!(f.path.is_file() || f.path.exists());
        }
    }

    #[test]
    fn find_cjk_face_by_script() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let sc = s
            .find(&FontQuery {
                family: None,
                serif: false,
                mono: false,
                weight: 400,
                italic: false,
                script: Script::HanSC,
            })
            .expect("SC face should exist");
        let f = s.get(sc).unwrap();
        assert!(f.family.contains("SC"), "got family {}", f.family);
        assert!(s.find_by_family("sans-sc", 400, false).is_some());
    }

    #[test]
    fn find_inter_latin() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&FontQuery {
                family: Some("Inter".into()),
                serif: false,
                mono: false,
                weight: 700,
                italic: false,
                script: Script::Latin,
            })
            .expect("Inter bold should exist");
        let f = s.get(id).unwrap();
        assert!(f.family.contains("Inter"));
        assert!(f.weight >= 600);
    }

    #[test]
    fn find_serif_for_han() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let id = s
            .find(&FontQuery {
                family: None,
                serif: true,
                mono: false,
                weight: 400,
                italic: false,
                script: Script::HanTC,
            })
            .expect("TC serif face should exist");
        let f = s.get(id).unwrap();
        assert!(f.family.contains("Serif"), "got {}", f.family);
        assert!(f.family.contains("TC"), "got {}", f.family);
    }

    #[test]
    fn missing_dir_is_error() {
        let err = FontStore::load_builtin(Path::new("/nonexistent-font-dir"));
        assert!(err.is_err());
    }
}
