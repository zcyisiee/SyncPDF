use std::path::{Path, PathBuf};

use syncpdf_layout::{DetectOpts, LayoutDevice, LayoutModel, LayoutOptions, RawImage};

#[test]
fn defaults_and_legacy_cpu_are_explicit() {
    let options = LayoutOptions::default();
    assert_eq!(options.threads, 2);
    assert_eq!(options.device, LayoutDevice::Auto);
    assert!(options.cache_dir.is_none());
    assert!(options.profile_path.is_none());

    let Some(model_path) = bbox_model() else {
        return;
    };
    let model = LayoutModel::load(&model_path, 2).expect("legacy CPU session");
    assert_eq!(model.configured_device(), LayoutDevice::Cpu);
    assert_eq!(model.fallback_reason(), None);
}

fn bbox_model() -> Option<PathBuf> {
    std::env::var_os("SYNCPDF_BBOX_MODEL").map(PathBuf::from)
}

#[cfg(target_os = "macos")]
#[test]
fn old_vendor_graph_has_observable_auto_fallback_and_strict_failure() {
    let Some(models) = syncpdf_core::fixtures::models_dir() else {
        return;
    };
    let path = models.join("pp_doc_layoutv3.onnx");
    if !path.is_file() {
        return;
    }
    let options = LayoutOptions {
        device: LayoutDevice::CoreMl,
        ..LayoutOptions::default()
    };
    assert!(
        LayoutModel::load_with_options(&path, options).is_err(),
        "unsupported vendor graph must not silently register CPU"
    );
    let options = LayoutOptions {
        device: LayoutDevice::Auto,
        ..LayoutOptions::default()
    };
    let model = LayoutModel::load_with_options(&path, options).expect("Auto CPU fallback");
    assert_eq!(model.configured_device(), LayoutDevice::Cpu);
    assert!(model.fallback_reason().is_some());
}

/// Run manually with SYNCPDF_BBOX_MODEL set to a checked V3 bbox graph.
/// The profile proves CoreML node assignment; CoreML's compute-plan log from
/// `with_profile_compute_plan` must additionally be inspected for Apple GPU use.
#[cfg(target_os = "macos")]
#[test]
#[ignore = "requires checked local bbox model and CoreML runtime"]
fn bbox_graph_runs_with_coreml_and_writes_provider_profile() {
    let model_path = bbox_model().expect("set SYNCPDF_BBOX_MODEL");
    assert!(model_path.is_file());
    let temp = tempfile::tempdir().expect("tempdir");
    let profile_prefix = temp.path().join("layout-profile");
    let options = LayoutOptions {
        device: LayoutDevice::CoreMl,
        cache_dir: Some(temp.path().join("coreml-cache")),
        profile_path: Some(profile_prefix),
        ..LayoutOptions::default()
    };
    let mut model = LayoutModel::load_with_options(&model_path, options).expect("CoreML load");
    assert_eq!(model.configured_device(), LayoutDevice::CoreMl);
    assert!(model.fallback_reason().is_none());
    let rgba = vec![255u8; 800 * 800 * 4];
    let page = RawImage {
        width: 800,
        height: 800,
        rgba: &rgba,
    };
    let _detections = model
        .detect(&page, &DetectOpts::default())
        .expect("CoreML inference");
    let profile = model
        .end_profiling()
        .expect("finalize profile")
        .expect("profile path");
    assert!(
        Path::new(&profile).is_file(),
        "profile missing: {}",
        profile.display()
    );
    let contents = std::fs::read_to_string(&profile).expect("read profile");
    assert!(
        contents.contains("CoreMLExecutionProvider"),
        "no CoreML node in profile"
    );
    eprintln!("profile: {}", profile.display());
    let _persisted_dir = temp.keep();
}
