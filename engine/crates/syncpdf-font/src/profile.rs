//! 角色字体链（profile）。
//!
//! 设计文档 §7：角色 `body / doc_title / paragraph_title / mono / raster`，
//! 每角色 regular / bold / italic / bold-italic 四变体链；CJK 由目标语言
//! 选 ttc face。缺变体时的降级：italic 缺失 → regular（上层用 italic_shear
//! 合成）；bold 缺失 → regular（上层用描边伪粗）。

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

/// 一个角色 → 四变体的字体链。
#[derive(Debug, Clone)]
pub struct FontProfile {
    /// 目标语言（zh-CN/zh-TW/ja/ko/en/ar）。
    pub target_lang: String,
    body: Chain,
    doc_title: Chain,
    paragraph_title: Chain,
    mono: Chain,
    raster: Chain,
    /// 回退顺序（所有可用字体，脚本感知排序）。
    fallbacks: Vec<FontId>,
}

/// 单角色四变体链。
#[derive(Debug, Clone, Default)]
struct Chain {
    regular: Option<FontId>,
    bold: Option<FontId>,
    italic: Option<FontId>,
    bold_italic: Option<FontId>,
}

impl FontProfile {
    /// 按角色 + 粗斜体取字体；缺变体逐级降级：
    /// bold_italic → (bold|italic) → regular。
    ///
    /// 恒有值：`default_profile` 保证每角色链非空；极端情况（字体包全缺）
    /// 用回退链首字体兜底。
    pub fn pick(&self, role: Role, bold: bool, italic: bool) -> FontId {
        let chain = self.chain(role);
        match (bold, italic) {
            (false, false) => chain.regular,
            (true, false) => chain.bold.or(chain.regular),
            (false, true) => chain.italic.or(chain.regular),
            (true, true) => chain
                .bold_italic
                .or(chain.bold)
                .or(chain.italic)
                .or(chain.regular),
        }
        .or_else(|| self.fallbacks.first().copied())
        .expect("FontProfile 恒有可用字体（default_profile 保证）")
    }

    /// 全部回退字体（去重，主字体在前）。
    pub fn fallbacks(&self) -> Vec<FontId> {
        self.fallbacks.clone()
    }

    fn chain(&self, role: Role) -> &Chain {
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
fn lang_script(lang: &str) -> Script {
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

    // 各角色链。
    let chain_for = |base: Option<FontId>, weight_base: u16| -> Chain {
        let mut c = Chain::default();
        if base.is_some() {
            c.regular = base;
            c.bold = find(false, weight_base, false);
            c.italic = find(false, 400, true);
            c.bold_italic = find(false, weight_base, true);
            // Inter/PT 有真斜体；CJK 无 → italic 链留 regular 供降级。
            if cjk {
                c.italic = c.italic.or(base);
                c.bold_italic = c.bold_italic.or(c.bold).or(base);
            }
        }
        c
    };

    let body_chain = chain_for(body, 700);
    let title_base = find(false, title_weight, false);
    let title_chain = chain_for(title_base, 700);
    let title_chain2 = chain_for(title_base, 700);
    // mono：内置包无等宽 → 用 PT Sans 占位（上层可接系统 mono）。
    let mono_chain = chain_for(store.find_by_family("sans", 400, false), 700);
    // raster：同 body。
    let raster_chain = chain_for(body, 700);

    FontProfile {
        target_lang: target_lang.to_string(),
        body: body_chain,
        doc_title: title_chain,
        paragraph_title: title_chain2,
        mono: mono_chain,
        raster: raster_chain,
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
        let body = p.pick(Role::Body, false, false);
        let f = s.get(body).unwrap();
        assert!(f.family.contains("SC"), "got {}", f.family);
        let bold = p.pick(Role::Body, true, false);
        let fb = s.get(bold).unwrap();
        assert!(fb.weight >= 600, "bold weight {}", fb.weight);
        // CJK 无斜体 → italic 降级到 regular。
        let it = p.pick(Role::Body, false, true);
        assert_eq!(it, body);
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
        let body = p.pick(Role::Body, false, false);
        assert!(s.get(body).unwrap().family.contains("TC"));
    }

    #[test]
    fn profile_en_uses_inter() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let p = default_profile(&s, "en");
        let body = p.pick(Role::Body, false, false);
        assert!(s.get(body).unwrap().family.contains("Inter"));
        let it = p.pick(Role::Body, false, true);
        assert!(s.get(it).unwrap().italic, "Inter 有真斜体");
        let bi = p.pick(Role::Body, true, true);
        assert!(s.get(bi).unwrap().italic);
        assert!(s.get(bi).unwrap().weight >= 600);
    }

    #[test]
    fn profile_ar() {
        let Some(s) = store() else {
            eprintln!("SKIP: font package missing");
            return;
        };
        let p = default_profile(&s, "ar");
        let body = p.pick(Role::Body, false, false);
        assert!(
            s.get(body).unwrap().family.contains("Arabic"),
            "got {}",
            s.get(body).unwrap().family
        );
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
                for (b, i) in [(false, false), (true, false), (false, true), (true, true)] {
                    let picked = p.pick(role, b, i);
                    assert!(
                        s.get(picked).is_some(),
                        "{lang} {role:?} bold={b} italic={i} 无字体"
                    );
                }
            }
        }
    }
}
