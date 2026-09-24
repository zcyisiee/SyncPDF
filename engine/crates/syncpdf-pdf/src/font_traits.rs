//! 源字体四维特征（serif / fixed_pitch / italic / bold）推断。
//!
//! 对齐 hjfy `FontTraits::from_font`（反编译 `pdf/font/FontTraits_.c` @ `0x1002d51b8`）
//! 的三层融合：**Flags → 度量（FontWeight / ItalicAngle / StemV）→ 名字（家族目录）**。
//! TeX 系字体的 FontDescriptor `Flags` 常为 4（仅 Symbolic），serif/mono 只能靠名字层救回。
//!
//! 名字层的家族分类复刻 hjfy `pdf::font_catalog::classify`
//! （`pdf/font_catalog.c` @ `0x1009f4b54`）的流程：
//!
//! 1. 剥 6 字符子集前缀（`ABCDEF+Name` → `Name`）；
//! 2. 小写并仅保留字母数字（`normalized` 的 filter 闭包 = `char::is_alphanumeric`，
//!    即去 `-`/`_`/空格等分隔符；名字超过 512 字节判未知）；
//! 3. 迭代剥离样式后缀（hjfy 的 memcmp 常量表 + URW 的 `roma`），每轮先查家族表；
//! 4. 家族表未命中时按 TeX 短名前缀（`cmr10`/`lmmono10`… = 前缀 + 纯数字尾，
//!    对应 hjfy from_font 的 CM/EC 特判）与通用关键词兜底。
//!
//! 融合规则（比 hjfy 略保守，见任务约定）：名字层只**补全** Flags 缺失的信息——
//! Flags 明确设置的位保持；sans 家族不得新增 serif；类别未知时维持 Flags 结果。
//! Flags 位掩码沿用 bind.rs 既有取法（italic = bit 7 / force-bold = bit 19，
//! pdfium 习惯位）；hjfy from_font 的可见反编译尾部未读这些位，不做位序改动。

/// FontDescriptor 中参与判定的度量值；缺失字段为 `None`。
#[derive(Debug, Clone, Copy, Default)]
pub struct Metrics {
    pub flags: Option<i64>,
    pub font_weight: Option<f64>,
    pub italic_angle: Option<f64>,
    pub stem_v: Option<f64>,
}

/// 四维特征，顺序与 [`crate::bind`] 的 `FontRef` 字段一致。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Traits {
    pub serif: bool,
    pub fixed_pitch: bool,
    pub italic: bool,
    pub bold: bool,
}

/// 家族类别。hjfy 的 `FamilyClass` 还有 script 等，这里只保留四维特征需要的三类。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FamilyClass {
    Serif,
    Sans,
    Mono,
}

/// hjfy classify 的名字长度上限（`0x200` 字节，超出直接判未知）。
const MAX_NAME_LEN: usize = 512;

/// 家族类别表（键为剥完样式后缀的规范家族名）。来源分两档，见各表注释：
/// hjfy 证据 = `hjfy-architecture/03-strings/hjfy-pdf.strings.utf8.txt` 里 font_catalog
/// 的成对条目与 hjfy PDFRuntime 回退栈；通用补充 = 真实存在的出版/TeX/Office 家族。
const SERIF_FAMILIES: &[&str] = &[
    // hjfy 证据（font_catalog 条目 / 回退栈）
    "timesnewroman",
    "timesnewromanps",
    "timesnewromanpsmt",
    "palatinolinotype",
    "nimbusroman",
    "nimbusromanno9l",
    "nimbusromno9l",
    "centuryoldstyle",
    "garamond",
    "garamondpremier",
    "georgia",
    "cambria",
    "stixtwotext",
    "dejavuserif",
    "liberationserif",
    // 通用补充
    "times", // Times-Roman 剥掉样式词 roman 后
    "palatino",
    "urwpalladiol", // URW Palladio L（Palatino 的 URW 克隆，TeX 常见）
    "bookman",
    "urwbookmanl",
    "centuryschoolbook",
    "centuryschoolbookl",
    "newcenturyschbook",
    "stix",
    "stixgeneral",
    "simsun",
    "nsimsun",
    "batang",
    "charissil",
    "charter",
    "xcharter",
];

const SANS_FAMILIES: &[&str] = &[
    // hjfy 证据
    "helvetica",
    "helveticaltstd",
    "arial",
    "arialunicodems",
    "calibri",
    "verdana",
    "tahoma",
    "nimbussans",
    "nimbussansl",
    "centurygothic",
    "dejavusans",
    "liberationsans",
    "inter",
    // 通用补充
    "helveticaneue",
    "segoeui",
    "urwgothicl",
    "texgyreheros",
    "texgyreadventor",
];

const MONO_FAMILIES: &[&str] = &[
    // hjfy 证据
    "courier",
    "couriernew",
    "courierprime",
    "courierstd",
    "nimbusmono",
    "nimbusmonol",
    "nimbusmonops",
    "consolas",
    "inconsolata",
    "liberationmono",
    // 通用补充
    "dejavusansmono",
    "menlo",
    "monaco",
    "andalemono",
    "texgyrecursor",
];

/// 家族表未命中时的 TeX 短名前缀（前缀 + 纯数字尾，对应 hjfy from_font 的
/// CM/EC 字名特判：`cmr10`→serif、`cmbx12`→serif+bold、`cmti10`→serif+italic、
/// `cmtt10`/`sftt…`→mono、`cmss…`→sans）。前缀有包含关系时长前缀在前。
/// 后两个布尔 = 该前缀隐含的 bold / italic。
const FAMILY_PREFIXES: &[(&str, FamilyClass, bool, bool)] = &[
    ("cmcsc", FamilyClass::Serif, false, false),
    ("cmbx", FamilyClass::Serif, true, false),
    ("cmti", FamilyClass::Serif, false, true),
    ("cmsl", FamilyClass::Serif, false, true),
    ("cmb", FamilyClass::Serif, true, false),
    ("cmr", FamilyClass::Serif, false, false),
    ("sfbx", FamilyClass::Serif, true, false),
    ("sfti", FamilyClass::Serif, false, true),
    ("sfrm", FamilyClass::Serif, false, false),
    ("lmroman", FamilyClass::Serif, false, false),
    ("texcm", FamilyClass::Serif, false, false),
    ("cmssbx", FamilyClass::Sans, true, false),
    ("cmss", FamilyClass::Sans, false, false),
    ("lmsans", FamilyClass::Sans, false, false),
    ("cmtt", FamilyClass::Mono, false, false),
    ("sftt", FamilyClass::Mono, false, false),
    ("lmmono", FamilyClass::Mono, false, false),
    ("inconsolatazi4", FamilyClass::Mono, false, false),
];

/// hjfy classify 迭代剥离的样式后缀（`font_catalog.c` 的 memcmp 常量），
/// 加 URW 的 `roma`（`URWPalladioL-Roma` 的正体记号）。长后缀优先。
const STYLE_SUFFIXES: &[&str] = &[
    "extralight",
    "ultralight",
    "extrabold",
    "ultrabold",
    "semibold",
    "demibold",
    "semilight",
    "regular",
    "oblique",
    "italic",
    "medium",
    "normal",
    "roman",
    "heavy",
    "black",
    "light",
    "roma",
    "bold",
    "thin",
    "ital",
    "book",
    "psmt",
    "mt",
    "it",
];

/// hjfy from_font 的通用关键词（对整个小写名做 `contains`）：
/// mono = `mono`/`courier`/`typewriter`/`menlo`/`cmtt`/`consolas`。
const MONO_KEYWORDS: &[&str] = &["mono", "courier", "typewriter", "menlo", "cmtt", "consolas"];
/// serif = `times`/`serif`/`mincho`/`song`/`ming`；名字含 `sans` 时全部失效（hjfy 同序）。
const SERIF_KEYWORDS: &[&str] = &["times", "serif", "mincho", "song", "ming"];
/// hjfy 的粗体关键词（`semibold` 已含 `demi`/`bold`，保留完整列表以对齐反编译常量）。
const BOLD_KEYWORDS: &[&str] = &["bold", "black", "heavy", "demi", "semibold"];
/// hjfy 的斜体关键词。
const ITALIC_KEYWORDS: &[&str] = &["italic", "oblique", "slanted"];

/// 剥 6 字符子集前缀：仅当 `+` 恰好在第 7 字节且前 6 字节是 ASCII 字母数字
/// （hjfy：首个 `+` 匹配结束于 6，前面 6 字节通过字符类检查）。
fn strip_subset_prefix(name: &str) -> &str {
    let b = name.as_bytes();
    if b.len() > 7 && b[6] == b'+' && b[..6].iter().all(u8::is_ascii_alphanumeric) {
        &name[7..]
    } else {
        name
    }
}

/// hjfy `font_catalog::normalized`：小写 + 只留字母数字；超长返回 `None`（判未知）。
fn normalized(name: &str) -> Option<String> {
    if name.len() > MAX_NAME_LEN {
        return None;
    }
    Some(
        name.chars()
            .flat_map(char::to_lowercase)
            .filter(|c| c.is_alphanumeric())
            .collect(),
    )
}

/// 家族表精确匹配。
fn table_class(family: &str) -> Option<FamilyClass> {
    if SERIF_FAMILIES.contains(&family) {
        Some(FamilyClass::Serif)
    } else if MONO_FAMILIES.contains(&family) {
        Some(FamilyClass::Mono)
    } else if SANS_FAMILIES.contains(&family) {
        Some(FamilyClass::Sans)
    } else {
        None
    }
}

/// 家族分类：先查表，未命中则迭代剥样式后缀再查，仍未知再试 TeX 短名前缀。
/// 返回 `(类别, 该名字隐含的 bold, italic)`；未知返回 `None`。
fn classify_family(norm: &str) -> Option<(FamilyClass, bool, bool)> {
    let mut family = norm;
    loop {
        if let Some(class) = table_class(family) {
            return Some((class, false, false));
        }
        match STYLE_SUFFIXES.iter().find(|s| family.ends_with(*s)) {
            Some(suffix) => family = &family[..family.len() - suffix.len()],
            None => break,
        }
    }
    FAMILY_PREFIXES
        .iter()
        .find_map(|&(prefix, class, bold, italic)| {
            let rest = family.strip_prefix(prefix)?;
            (rest.is_empty() || rest.bytes().all(|b| b.is_ascii_digit()))
                .then_some((class, bold, italic))
        })
}

/// 一个候选名字给出的证据。
#[derive(Debug, Clone, Copy, Default)]
struct NameEvidence {
    serif: bool,
    mono: bool,
    bold: bool,
    italic: bool,
}

/// 提取单个字体名（`/BaseFont` 或 FontDescriptor `/FontName`，可含子集前缀与样式后缀）
/// 的四维证据：家族分类 + TeX 前缀特判 + 通用关键词。
fn name_evidence(name: &str) -> NameEvidence {
    let lower = name.to_ascii_lowercase();
    let mut ev = NameEvidence::default();

    // 家族分类：hjfy 的 classify 剥 6 字符子集前缀；CM 特判用最后一个 `+` 分量。
    let last_component = lower.rsplit('+').next().unwrap_or(&lower);
    for candidate in [strip_subset_prefix(&lower), last_component] {
        if let Some(norm) = normalized(candidate) {
            if let Some((class, bold, italic)) = classify_family(&norm) {
                match class {
                    FamilyClass::Serif => ev.serif = true,
                    FamilyClass::Mono => ev.mono = true,
                    // sans 家族不得被判为 serif：只补全，不清除 Flags。
                    FamilyClass::Sans => {}
                }
                ev.bold |= bold;
                ev.italic |= italic;
                break;
            }
        }
    }

    // 关键词层（hjfy from_font 对整个小写名做 contains；含 sans 时 serif 关键词失效）。
    if !lower.contains("sans") {
        ev.serif |= SERIF_KEYWORDS.iter().any(|k| lower.contains(k));
    }
    ev.mono |= MONO_KEYWORDS.iter().any(|k| lower.contains(k));
    ev.bold |= BOLD_KEYWORDS.iter().any(|k| lower.contains(k));
    ev.italic |= ITALIC_KEYWORDS.iter().any(|k| lower.contains(k));
    ev
}

/// Flags → 度量 → 名字的三层融合。
///
/// `names` 是该字体的全部候选名（`/BaseFont` 与 FontDescriptor `/FontName`），
/// 任一名字给出的证据只补全（OR），不会撤销 Flags 或度量已确定的位。
pub fn infer(metrics: Metrics, names: &[&str]) -> Traits {
    // 层 1：Flags（沿用 bind.rs 既有位掩码：bit2 serif、bit1 fixed、bit7 italic、
    // bit19 force-bold）。
    let mut serif = metrics.flags.is_some_and(|f| f & 2 != 0);
    let mut fixed_pitch = metrics.flags.is_some_and(|f| f & 1 != 0);
    let mut italic = metrics.flags.is_some_and(|f| f & 64 != 0);
    let mut bold = metrics.flags.is_some_and(|f| f & (1 << 18) != 0);

    // 层 2：度量。hjfy：FontWeight >= 600 → bold、ItalicAngle != 0 → italic；
    // StemV > 120 → bold 是本仓库既有规则（hjfy 未用），保留。
    if metrics.italic_angle.is_some_and(|v| v.abs() > 0.01) {
        italic = true;
    }
    if metrics.font_weight.is_some_and(|w| w >= 600.0) {
        bold = true;
    }
    if metrics.stem_v.is_some_and(|v| v > 120.0) {
        bold = true;
    }

    // 层 3：名字（只补全）。
    for name in names {
        let ev = name_evidence(name);
        serif |= ev.serif;
        fixed_pitch |= ev.mono;
        italic |= ev.italic;
        bold |= ev.bold;
    }

    Traits {
        serif,
        fixed_pitch,
        italic,
        bold,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn infer_one(metrics: Metrics, name: &str) -> Traits {
        infer(metrics, &[name])
    }

    #[test]
    fn classify_strips_suffixes_iteratively() {
        let norm = |n: &str| normalized(strip_subset_prefix(n)).unwrap();
        assert_eq!(
            classify_family(&norm("ABCDEF+URWPalladioL-BoldItal")),
            Some((FamilyClass::Serif, false, false))
        );
        assert_eq!(
            classify_family(&norm("TimesNewRomanPS-ItalicMT")),
            Some((FamilyClass::Serif, false, false))
        );
        assert_eq!(
            classify_family(&norm("LMMono10-Regular")),
            Some((FamilyClass::Mono, false, false))
        );
    }

    #[test]
    fn classify_technical_prefixes() {
        assert_eq!(
            classify_family("cmbx12"),
            Some((FamilyClass::Serif, true, false))
        );
        assert_eq!(
            classify_family("cmti10"),
            Some((FamilyClass::Serif, false, true))
        );
        assert_eq!(
            classify_family("cmtt10"),
            Some((FamilyClass::Mono, false, false))
        );
        assert_eq!(
            classify_family("cmssbx10"),
            Some((FamilyClass::Sans, true, false))
        );
        assert_eq!(
            classify_family("cmss10"),
            Some((FamilyClass::Sans, false, false))
        );
        // 前缀后必须全是数字（hjfy 的 isdigit 循环）。
        assert_eq!(classify_family("cmrx"), None);
    }

    #[test]
    fn normalized_removes_separators_and_caps() {
        // normalized 只小写 + 去分隔符；子集前缀由 strip_subset_prefix 剥。
        assert_eq!(
            normalized(strip_subset_prefix("URXHTJ+URWPalladioL-Roma")).as_deref(),
            Some("urwpalladiolroma")
        );
        assert_eq!(normalized("A B_C").as_deref(), Some("abc"));
    }

    #[test]
    fn infer_sans_word_suppresses_serif_keywords() {
        let t = infer_one(Metrics::default(), "SuperSerifSans");
        assert!(!t.serif, "含 sans 的名字不得因 serif 关键词判衬线");
    }
}
