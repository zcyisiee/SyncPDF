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
    /// 等宽 run 的 CJK 回退链（黑体在前；等宽包无 CJK 字形）。
    mono_cjk_fallbacks: Vec<FontId>,
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

    /// 按 family 别名构建四槽（mono 角色）：四个变体都从该 family 内按
    /// 字重/斜体评分取面（[`FontStore::find_by_family`] 对斜体匹配加权），
    /// 绕开通用查询的 CJK 族过滤——等宽包不含 CJK 字形，其 CJK 回退由
    /// [`FontProfile::mono_fallbacks`] 承担，不参与槽解析。
    /// 缺等宽包时返回 `None`，由调用方走 sans 占位路径。
    fn build_family_anchored(store: &FontStore, family: &str) -> Option<Self> {
        let pick = |weight: u16, italic: bool| -> Option<VariantFace> {
            store
                .find_by_family(family, weight, italic)
                .map(|font| VariantFace {
                    font,
                    // 该 family 有真斜体面（如 JetBrains Mono 四面）；合成标记
                    // 以 `LoadedFont::italic` 证实，缺真面时按正体 + 合成处理。
                    synthetic_italic: italic && !store.get(font).is_some_and(|f| f.italic),
                })
        };
        let regular = pick(400, false)?;
        let bold = pick(700, false).unwrap_or(regular);
        let italic = pick(400, true).unwrap_or(VariantFace {
            font: regular.font,
            synthetic_italic: true,
        });
        let bold_italic = pick(700, true).unwrap_or(VariantFace {
            font: bold.font,
            synthetic_italic: true,
        });
        Some(Self {
            regular,
            bold,
            italic,
            bold_italic,
        })
    }

    /// 解析四槽。`base` 是正体槽的显式锚点（标题角色用粗面）；`serif`
    /// 决定槽内变体按衬线族还是无衬线族解析；斜体槽由 [`resolve_variant`]
    /// 决定真面或合成。
    fn build(store: &FontStore, script: Script, serif: bool, base: Option<FontId>) -> Self {
        let resolve = |variant: FontVariant| resolve_variant(store, serif, script, variant);
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

    /// 等宽 run 的缺字回退链：CJK 黑体在前（等宽包只含拉丁，CJK 字形须由
    /// Noto Sans CJK 承担），其余沿用全局链去重。
    pub fn mono_fallbacks(&self) -> Vec<FontId> {
        let mut chain = self.mono_cjk_fallbacks.clone();
        for id in &self.fallbacks {
            if !chain.contains(id) {
                chain.push(*id);
            }
        }
        chain
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
/// 角色字面策略（目标字体，不再依赖源字体 flags）：
/// - Body（一切非标题区域，含 Caption/Abstract/List）：衬线族——CJK →
///   Noto Serif CJK（思源宋体），拉丁沿用既有衬线链（PTSerif）；
/// - DocTitle/ParagraphTitle：无衬线族——CJK → Noto Sans CJK（黑体），
///   拉丁沿用 Inter/PT Sans 链；
/// - Mono：内置 JetBrains Mono 包（`mono` family，四变体真面）；
/// - en：正文 Inter（无衬线即正文字体）、标题 Inter；ar：Noto Sans Arabic。
///
/// 缺真斜体面时由 [`VariantSlots::build`] 标记合成剪切；mono 有真斜体面。
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

    // 正文锚点：CJK/拉丁衬线族优先（zh → Noto Serif CJK；en 无 CJK 概念，
    // 拉丁衬线查询回落 Inter 仍由 find 分数决定）。en/ar 目标保留原 sans
    // 基线（Inter/Arabic 即该语言的正文字体）。
    let body_sans = find(false, 400, false);
    let body_serif = find(true, 400, false);
    let cjk = matches!(
        script,
        Script::HanSC | Script::HanTC | Script::Kana | Script::Hangul
    );
    let body = if cjk {
        body_serif.or(body_sans)
    } else {
        body_sans.or(body_serif)
    };
    let title_weight = 700u16;

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

    // 等宽 run 的 CJK 回退链：黑体（region face）在前，其余由全局链补足。
    let mut mono_cjk_fallbacks: Vec<FontId> = Vec::new();
    push_fb(find(false, 400, false), &mut mono_cjk_fallbacks);
    push_fb(find(false, 700, false), &mut mono_cjk_fallbacks);

    // 各角色槽：正体锚点 + 真斜体/合成判定（见 VariantSlots::build）。
    let title_base = find(false, title_weight, false);
    // mono 槽四面从等宽包取（真斜体面，无合成剪切）；缺包时回落 sans
    // 占位（与裁剪发行版兼容）。
    let mono = VariantSlots::build_family_anchored(store, "mono").unwrap_or_else(|| {
        VariantSlots::build(
            store,
            script,
            false,
            store.find_by_family("sans", 400, false),
        )
    });
    FontProfile {
        target_lang: target_lang.to_string(),
        body: VariantSlots::build(store, script, cjk, body),
        doc_title: VariantSlots::build(store, script, false, title_base),
        paragraph_title: VariantSlots::build(store, script, false, title_base),
        mono,
        raster: VariantSlots::build(store, script, cjk, body),
        fallbacks,
        mono_cjk_fallbacks,
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
        assert!(
            f.family.contains("Serif") && f.family.contains("SC"),
            "正文角色应为思源宋体，got {}",
            f.family
        );
        let bold = p.pick_variant(Role::Body, FontVariant::Bold);
        let fb = s.get(bold.font).unwrap();
        assert!(fb.weight >= 600, "bold weight {}", fb.weight);
        assert!(
            s.get(bold.font).unwrap().family.contains("Serif"),
            "粗体仍在衬线族"
        );
        // 回退链非空且含拉丁字体。
        let fbs = p.fallbacks();
        assert!(fbs.len() >= 2);
        assert!(
            fbs.iter()
                .any(|&id| s.get(id).unwrap().family.contains("Inter")),
            "latin fallback missing"
        );
    }

    /// 标题角色用黑体（Noto Sans CJK）；CJK 目标下全部角色均如此。
    #[test]
    fn profile_heading_roles_use_sans_for_cjk() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let p = default_profile(&s, "zh-CN");
        for role in [Role::DocTitle, Role::ParagraphTitle] {
            let face = p.pick_variant(role, FontVariant::Regular);
            let f = s.get(face.font).unwrap();
            assert!(
                f.family.contains("Sans") && f.family.contains("SC"),
                "{role:?} 应为黑体，got {}",
                f.family
            );
        }
        // 反例：正文角色不落黑体。
        let body = s
            .get(p.pick_variant(Role::Body, FontVariant::Regular).font)
            .unwrap();
        assert!(
            body.family.contains("Serif"),
            "正文不落黑体：{}",
            body.family
        );
    }

    /// mono 角色四槽指向 JetBrains Mono（真斜体面，无合成剪切）。
    #[test]
    fn profile_mono_slots_are_jetbrains_mono() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let p = default_profile(&s, "zh-CN");
        for (variant, italic, weight_min) in [
            (FontVariant::Regular, false, 400),
            (FontVariant::Bold, false, 600),
            (FontVariant::Italic, true, 400),
            (FontVariant::BoldItalic, true, 600),
        ] {
            let face = p.pick_variant(Role::Mono, variant);
            let f = s.get(face.font).unwrap();
            assert_eq!(f.family, "JetBrains Mono", "{variant:?}");
            assert_eq!(f.italic, italic, "{variant:?}");
            assert!(f.weight >= weight_min, "{variant:?}: {}", f.weight);
            assert!(
                !face.synthetic_italic,
                "JetBrains Mono 有真斜体面，{variant:?} 不应合成"
            );
        }
    }

    /// mono 角色的 CJK 回退链以 Noto Sans CJK 打头（等宽包不含 CJK 字形）。
    #[test]
    fn profile_mono_fallbacks_start_with_cjk_sans() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let p = default_profile(&s, "zh-CN");
        let fbs = p.mono_fallbacks();
        assert!(!fbs.is_empty());
        let head = s.get(fbs[0]).unwrap();
        assert!(
            head.family.contains("Sans") && head.family.contains("SC"),
            "mono CJK 回退首个应是黑体，got {}",
            head.family
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
