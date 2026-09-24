//! 角色字体链（profile）：角色 × 变体槽。
//!
//! 设计文档 §7：角色 `body / doc_title / paragraph_title / mono / raster`，
//! 每角色 regular / bold / italic / bold-italic 四变体槽（与 hjfy
//! `FileFontFamily` 的 `{regular, bold, italic, bold-italic, face-index}`
//! 同构）；CJK 由目标语言选 ttc face。缺真斜体字面时不静默丢斜体：槽位
//! 显式标记 [`VariantFace::synthetic_italic`]，由排版层对字形施加合成剪切
//! （[`SYNTHETIC_ITALIC_SHEAR_DEGREES`]，对应 hjfy `italic_shear_degrees`）。

use crate::loader::{FontId, FontQuery, FontStore, Script};

/// 文档语义角色。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Role {
    /// 正文。
    Body,
    /// 文档标题（封面大字）。
    DocTitle,
    /// 段落标题。
    ParagraphTitle,
    /// 等宽（代码/数字对齐）。
    Mono,
    /// 栅格/OCR 场景。
    Raster,
}

/// 变体槽：一个角色下按 (bold, italic) 组合的字形风格。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FontVariant {
    /// 常规正体。
    Regular,
    /// 粗体正体。
    Bold,
    /// 常规斜体。
    Italic,
    /// 粗斜体。
    BoldItalic,
}

impl FontVariant {
    /// 由样式的 (bold, italic) 取槽位。
    pub fn of(bold: bool, italic: bool) -> Self {
        match (bold, italic) {
            (false, false) => Self::Regular,
            (true, false) => Self::Bold,
            (false, true) => Self::Italic,
            (true, true) => Self::BoldItalic,
        }
    }

    fn wants_italic(self) -> bool {
        matches!(self, Self::Italic | Self::BoldItalic)
    }

    /// 期望字重（内置包只有 400/700 两档）。
    fn weight(self) -> u16 {
        if matches!(self, Self::Bold | Self::BoldItalic) {
            700
        } else {
            400
        }
    }
}

/// 一个变体槽的选字结果。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct VariantFace {
    /// 选中的字体。
    pub font: FontId,
    /// `true` = 库内没有该槽的真斜体字面，斜体由正体面 + 排版层合成剪切
    /// 表达（CJK 家族即此情况）；`false` = 真斜体面，无需剪切。
    pub synthetic_italic: bool,
}

/// 合成斜体的默认剪切角（度），对应 hjfy `italic_shear_degrees`。
/// 集中定义一处；排版/写回不得散落魔数。
pub const SYNTHETIC_ITALIC_SHEAR_DEGREES: f32 = 10.0;

/// 合成斜体的剪切量（tan 值；正值 = 向右倾斜）。
pub fn synthetic_italic_shear() -> f32 {
    SYNTHETIC_ITALIC_SHEAR_DEGREES.to_radians().tan()
}

/// 按查询条件解析一个变体槽；角色槽与 serif 覆盖路径共用同一判定。
///
/// 真斜体槽必须以 `LoadedFont::italic` 证实——[`FontStore::find`] 对没有
/// 斜体面的家族会返回非斜体的降级候选。证不实时回落同字重正体面并标记
/// 合成。库内完全无候选时返回 `None`，由调用方走角色槽或回退链。
pub fn resolve_variant(
    store: &FontStore,
    serif: bool,
    script: Script,
    variant: FontVariant,
) -> Option<VariantFace> {
    let query = |weight: u16, italic: bool| {
        store.find(&FontQuery {
            family: None,
            serif,
            mono: false,
            weight,
            italic,
            script,
        })
    };
    if !variant.wants_italic() {
        return query(variant.weight(), false)
            .or_else(|| query(400, false))
            .map(|font| VariantFace {
                font,
                synthetic_italic: false,
            });
    }
    let true_face =
        query(variant.weight(), true).filter(|&id| store.get(id).is_some_and(|f| f.italic));
    if let Some(font) = true_face {
        return Some(VariantFace {
            font,
            synthetic_italic: false,
        });
    }
    // 缺真斜体面：italic 槽 = regular 面 + 剪切；bold-italic 槽 = bold 面 + 剪切。
    query(variant.weight(), false)
        .or_else(|| query(400, false))
        .map(|font| VariantFace {
            font,
            synthetic_italic: true,
        })
}

/// 一个角色 → 四变体槽。
#[derive(Debug, Clone)]
pub struct FontProfile {
    /// 目标语言（zh-CN/zh-TW/ja/ko/en/ar）。
    pub target_lang: String,
    body: VariantSlots,
    doc_title: VariantSlots,
    paragraph_title: VariantSlots,
    mono: VariantSlots,
    raster: VariantSlots,
    /// 回退顺序（所有可用字体，脚本感知排序）。
    fallbacks: Vec<FontId>,
}

/// 单角色四变体槽。
#[derive(Debug, Clone)]
struct VariantSlots {
    regular: VariantFace,
    bold: VariantFace,
    italic: VariantFace,
    bold_italic: VariantFace,
}

impl VariantSlots {
    fn pick(&self, variant: FontVariant) -> VariantFace {
        match variant {
            FontVariant::Regular => self.regular,
            FontVariant::Bold => self.bold,
            FontVariant::Italic => self.italic,
            FontVariant::BoldItalic => self.bold_italic,
        }
    }

    /// 解析四槽。`base` 是正体槽的显式锚点（标题角色用粗面、mono 用
    /// PT Sans 占位）；斜体槽由 [`resolve_variant`] 决定真面或合成。
    fn build(store: &FontStore, script: Script, base: Option<FontId>) -> Self {
        let resolve = |variant: FontVariant| resolve_variant(store, false, script, variant);
        // 每槽必有面：通用查询也落空时用库内任一字体兜底
        // （`default_profile` 只对非空库构造）。
        let fallback = store
            .ids()
            .first()
            .copied()
            .expect("字体库非空（load_builtin 拒绝空库）");
        let upright = |variant: FontVariant| VariantFace {
            font: resolve(variant).map(|face| face.font).unwrap_or(fallback),
            synthetic_italic: false,
        };
        let regular = base.map_or_else(
            || upright(FontVariant::Regular),
            |font| VariantFace {
                font,
                synthetic_italic: false,
            },
        );
        let bold = upright(FontVariant::Bold);
        // 斜体槽：resolve 已按真面/合成判定；完全无候选时回落正体槽 + 合成。
        let italic = resolve(FontVariant::Italic).unwrap_or(VariantFace {
            font: regular.font,
            synthetic_italic: true,
        });
        let bold_italic = resolve(FontVariant::BoldItalic).unwrap_or(VariantFace {
            font: bold.font,
            synthetic_italic: true,
        });
        Self {
            regular,
            bold,
            italic,
            bold_italic,
        }
    }
}

impl FontProfile {
    /// 按角色 + 变体槽取字体面。
    ///
    /// 恒有值：`default_profile` 保证每角色槽非空；极端情况（字体包全缺）
    /// 已在槽构建时用库内字体兜底。
    pub fn pick_variant(&self, role: Role, variant: FontVariant) -> VariantFace {
        self.slots(role).pick(variant)
    }

    /// 全部回退字体（去重，主字体在前）。
    pub fn fallbacks(&self) -> Vec<FontId> {
        self.fallbacks.clone()
    }

    fn slots(&self, role: Role) -> &VariantSlots {
        match role {
            Role::Body => &self.body,
            Role::DocTitle => &self.doc_title,
            Role::ParagraphTitle => &self.paragraph_title,
            Role::Mono => &self.mono,
            Role::Raster => &self.raster,
        }
    }
}

/// 目标语言 → 脚本。
pub fn lang_script(lang: &str) -> Script {
    match lang {
        "zh-CN" | "zh-SG" => Script::HanSC,
        "zh-TW" | "zh-HK" => Script::HanTC,
        "ja" => Script::Kana,
        "ko" => Script::Hangul,
        "ar" => Script::Arabic,
        _ => Script::Latin,
    }
}

/// 按目标语言构建默认 profile。
///
/// 字体选择：
/// - zh-CN/zh-TW：正文 Noto Sans CJK（region face）；标题 Noto Sans CJK
///   Bold 亦可（hjfy 行为：标题用同族 bold）；
/// - en：正文 Inter、标题 Inter、raster PT Sans；
/// - ja/ko：Noto Sans CJK 对应 face；
/// - ar：Noto Sans Arabic（拉丁部分回退 Inter）。
///
/// serif 匹配由调用方（typeset 拿源字体 flags 后）通过 `find` 二次覆盖；
/// 本函数产出 sans 基线。
pub fn default_profile(store: &FontStore, target_lang: &str) -> FontProfile {
    let script = lang_script(target_lang);
    let find = |serif: bool, weight: u16, italic: bool| -> Option<FontId> {
        store.find(&FontQuery {
            family: None,
            serif,
            mono: false,
            weight,
            italic,
            script,
        })
    };

    // 主字体：CJK → Noto Sans CJK（region）；ar → Noto Sans Arabic；
    // en → Inter。serif 由源字体 flags 决定，默认 sans。
    let body_sans = find(false, 400, false);
    let body_serif = find(true, 400, false);
    let body = body_sans.or(body_serif);
    let title_weight = 700u16;

    let cjk = matches!(
        script,
        Script::HanSC | Script::HanTC | Script::Kana | Script::Hangul
    );

    let mut fallbacks: Vec<FontId> = Vec::new();
    let push_fb = |id: Option<FontId>, out: &mut Vec<FontId>| {
        if let Some(id) = id {
            if !out.contains(&id) {
                out.push(id);
            }
        }
    };
    push_fb(body, &mut fallbacks);
    // 回退：拉丁（Inter）、PT Sans、阿拉伯、其他 region CJK。
    for (weight, italic) in [(400u16, false), (700, false)] {
        push_fb(
            store.find(&FontQuery {
                family: Some("Inter".into()),
                weight,
                italic,
                script: Script::Latin,
                ..Default::default()
            }),
            &mut fallbacks,
        );
    }
    push_fb(store.find_by_family("arabic", 400, false), &mut fallbacks);
    if cjk {
        // CJK 目标：拉丁回退后补阿拉伯不现实，但保留其他 region 供混排
        // （如 zh 文档含日文假名）。
        push_fb(store.find_by_family("sans-jp", 400, false), &mut fallbacks);
        push_fb(store.find_by_family("sans-kr", 400, false), &mut fallbacks);
        push_fb(store.find_by_family("sans-tc", 400, false), &mut fallbacks);
        push_fb(store.find_by_family("sans-sc", 400, false), &mut fallbacks);
    } else {
        // 非目标语言的 CJK 全放回退。
        push_fb(store.find_by_family("sans-sc", 400, false), &mut fallbacks);
        push_fb(store.find_by_family("sans-tc", 400, false), &mut fallbacks);
        push_fb(store.find_by_family("sans-jp", 400, false), &mut fallbacks);
        push_fb(store.find_by_family("sans-kr", 400, false), &mut fallbacks);
    }
    push_fb(store.find_by_family("sans", 400, false), &mut fallbacks);
    push_fb(store.find_by_family("math", 400, false), &mut fallbacks);

    // 各角色槽：正体锚点 + 真斜体/合成判定（见 VariantSlots::build）。
    let title_base = find(false, title_weight, false);
    // mono：内置包无等宽 → 用 PT Sans 占位（上层可接系统 mono）。
    FontProfile {
        target_lang: target_lang.to_string(),
        body: VariantSlots::build(store, script, body),
        doc_title: VariantSlots::build(store, script, title_base),
        paragraph_title: VariantSlots::build(store, script, title_base),
        mono: VariantSlots::build(store, script, store.find_by_family("sans", 400, false)),
        raster: VariantSlots::build(store, script, body),
        fallbacks,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::loader::FontStore;

    fn store() -> Option<FontStore> {
        let dir = syncpdf_core::fixtures::fonts_dir()?;
        FontStore::load_builtin(&dir).ok()
    }

    #[test]
    fn profile_zh_cn() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let p = default_profile(&s, "zh-CN");
        let body = p.pick_variant(Role::Body, FontVariant::Regular);
        let f = s.get(body.font).unwrap();
        assert!(f.family.contains("SC"), "got {}", f.family);
        let bold = p.pick_variant(Role::Body, FontVariant::Bold);
        let fb = s.get(bold.font).unwrap();
        assert!(fb.weight >= 600, "bold weight {}", fb.weight);
        // 回退链非空且含拉丁字体。
        let fbs = p.fallbacks();
        assert!(fbs.len() >= 2);
        assert!(
            fbs.iter()
                .any(|&id| s.get(id).unwrap().family.contains("Inter")),
            "latin fallback missing"
        );
    }

    #[test]
    fn profile_zh_tw_uses_tc() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let p = default_profile(&s, "zh-TW");
        let body = p.pick_variant(Role::Body, FontVariant::Regular);
        assert!(s.get(body.font).unwrap().family.contains("TC"));
    }

    #[test]
    fn profile_en_uses_inter() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let p = default_profile(&s, "en");
        let body = p.pick_variant(Role::Body, FontVariant::Regular);
        assert!(s.get(body.font).unwrap().family.contains("Inter"));
    }

    #[test]
    fn profile_ar() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let p = default_profile(&s, "ar");
        let body = p.pick_variant(Role::Body, FontVariant::Regular);
        assert!(
            s.get(body.font).unwrap().family.contains("Arabic"),
            "got {}",
            s.get(body.font).unwrap().family
        );
    }

    /// 选字层：缺真斜体面 → 正体面 + 合成标记；拉丁（en）→ 真斜体面；
    /// 正体/粗体槽永不合成（反例）。
    #[test]
    fn variant_slots_split_synthetic_italic_from_true_faces() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let zh = default_profile(&s, "zh-CN");
        // CJK 家族无斜体面：italic 槽 = regular 面 + 合成，bold-italic = bold 面 + 合成。
        let regular = zh.pick_variant(Role::Body, FontVariant::Regular);
        let italic = zh.pick_variant(Role::Body, FontVariant::Italic);
        assert_eq!(italic.font, regular.font, "CJK italic 槽回落 regular 面");
        assert!(italic.synthetic_italic, "CJK italic 槽应标记合成斜体");
        assert!(!s.get(italic.font).unwrap().italic);
        let bold = zh.pick_variant(Role::Body, FontVariant::Bold);
        let bold_italic = zh.pick_variant(Role::Body, FontVariant::BoldItalic);
        assert_eq!(
            bold_italic.font, bold.font,
            "CJK bold-italic 槽回落 bold 面"
        );
        assert!(bold_italic.synthetic_italic);
        assert!(s.get(bold_italic.font).unwrap().weight >= 600);
        // 标题角色同样按字面可用性合成（不同形态正例）。
        for role in [Role::DocTitle, Role::ParagraphTitle, Role::Raster] {
            assert!(
                zh.pick_variant(role, FontVariant::Italic).synthetic_italic,
                "{role:?} italic"
            );
            assert!(
                zh.pick_variant(role, FontVariant::BoldItalic)
                    .synthetic_italic,
                "{role:?} bold-italic"
            );
        }
        // 拉丁目标：真斜体面，不合成。
        let en = default_profile(&s, "en");
        let it = en.pick_variant(Role::Body, FontVariant::Italic);
        assert!(!it.synthetic_italic, "Inter 有真斜体");
        assert!(s.get(it.font).unwrap().italic);
        let bi = en.pick_variant(Role::Body, FontVariant::BoldItalic);
        assert!(!bi.synthetic_italic);
        assert!(s.get(bi.font).unwrap().italic);
        assert!(s.get(bi.font).unwrap().weight >= 600);
        // 反例：正体/粗体槽永不合成。
        for p in [&zh, &en] {
            for variant in [FontVariant::Regular, FontVariant::Bold] {
                assert!(
                    !p.pick_variant(Role::Body, variant).synthetic_italic,
                    "{variant:?}"
                );
            }
        }
    }

    /// serif 覆盖路径（Text 区域正文）与角色槽使用同一套真斜体/合成判定。
    #[test]
    fn resolve_variant_applies_same_italic_rule_for_serif_override() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        // CJK serif：无真斜体面 → NotoSerifCJK regular + 合成。
        let cjk = resolve_variant(&s, true, Script::HanSC, FontVariant::Italic)
            .expect("CJK serif 面应存在");
        assert!(cjk.synthetic_italic);
        assert!(!s.get(cjk.font).unwrap().italic);
        assert!(s.get(cjk.font).unwrap().family.contains("Serif"));
        // 拉丁 serif：PTSerif-Italic 真面。
        let latin = resolve_variant(&s, true, Script::Latin, FontVariant::Italic)
            .expect("PTSerif-Italic 应存在");
        assert!(!latin.synthetic_italic);
        assert!(s.get(latin.font).unwrap().italic);
        assert!(s.get(latin.font).unwrap().family.contains("Serif"));
        let bi = resolve_variant(&s, true, Script::Latin, FontVariant::BoldItalic)
            .expect("PTSerif-BoldItalic 应存在");
        assert!(!bi.synthetic_italic);
        assert!(s.get(bi.font).unwrap().italic);
        assert!(s.get(bi.font).unwrap().weight >= 600);
    }

    #[test]
    fn profile_every_role_some() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        for lang in ["zh-CN", "zh-TW", "ja", "ko", "en", "ar"] {
            let p = default_profile(&s, lang);
            for role in [
                Role::Body,
                Role::DocTitle,
                Role::ParagraphTitle,
                Role::Mono,
                Role::Raster,
            ] {
                for variant in [
                    FontVariant::Regular,
                    FontVariant::Bold,
                    FontVariant::Italic,
                    FontVariant::BoldItalic,
                ] {
                    let picked = p.pick_variant(role, variant);
                    assert!(
                        s.get(picked.font).is_some(),
                        "{lang} {role:?} {variant:?} 无字体"
                    );
                }
            }
        }
    }
}
