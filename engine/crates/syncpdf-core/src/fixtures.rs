//! 测试夹具定位。夹具 PDF 不入库，缺失时测试应 skip 并打印原因。

use std::path::{Path, PathBuf};

/// `engine/fixtures/` 目录（编译期由 workspace 布局推出）。
pub fn dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../fixtures")
        .canonicalize()
        .unwrap_or_else(|_| Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures"))
}

/// 夹具路径；文件不存在返回 `None`。
pub fn path(name: &str) -> Option<PathBuf> {
    let p = dir().join(name);
    p.is_file().then_some(p)
}

/// 在测试里用：缺失时打印 skip 原因并提前返回。
#[macro_export]
macro_rules! require_fixture {
    ($name:expr) => {
        match $crate::fixtures::path($name) {
            Some(p) => p,
            None => {
                eprintln!(
                    "SKIP: fixture {} missing; run `cargo xtask fixtures`",
                    $name
                );
                return;
            }
        }
    };
}

/// pdfium 动态库目录：`PDFIUM_DYNAMIC_LIB_PATH`，否则 `engine/vendor/pdfium/lib`。
pub fn pdfium_lib_dir() -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("PDFIUM_DYNAMIC_LIB_PATH") {
        let p = PathBuf::from(p);
        if p.is_dir() {
            return Some(p);
        }
    }
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../vendor/pdfium/lib");
    p.is_dir().then_some(p)
}

#[cfg(test)]
mod tests {
    #[test]
    fn dir_points_into_engine() {
        assert!(super::dir().ends_with("fixtures"));
    }

    #[test]
    fn ci_fixture_present_or_skipped() {
        let p = crate::require_fixture!("ci-test.pdf");
        assert!(p.metadata().unwrap().len() > 0);
    }
}

fn env_or_vendor(var: &str, sub: &str) -> Option<PathBuf> {
    if let Some(p) = std::env::var_os(var) {
        let p = PathBuf::from(p);
        if p.is_dir() {
            return Some(p);
        }
    }
    let p = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../vendor")
        .join(sub);
    p.is_dir().then_some(p)
}

/// 内置字体包目录：`SYNCPDF_FONTS`，否则 `engine/vendor/fonts`。
pub fn fonts_dir() -> Option<PathBuf> {
    env_or_vendor("SYNCPDF_FONTS", "fonts")
}

/// 模型目录：`SYNCPDF_MODELS`，否则 `engine/vendor/models`。
pub fn models_dir() -> Option<PathBuf> {
    env_or_vendor("SYNCPDF_MODELS", "models")
}
