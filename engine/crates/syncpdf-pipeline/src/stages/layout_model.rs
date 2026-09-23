//! Verified PP-DocLayout-V3 assets. Model caches are keyed by these exact bytes.

use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use super::PipelineError;

/// Official V3 bbox export, revision 46bbdf188bb0a772c08aed74882ce7e51a8f1ea6.
pub const V3_BBOX_SHA256: &str = "fe3bc78476c982401caf389a8e8e928cb94cc0dbb89be73a363838f19fcaf271";
const V3_FULL_SHA256: &str = "45bf71750b00739a41fc209f132eb104a4d6b5bb29483c9078164d8b87cf28ba";
const V3_LEGACY_SHA256: &str = "250dbad1dfb9e4983fab75e1bf5085cd56ec3f41d5c7d0f8623ec74856e7aa67";

#[derive(Debug, Clone)]
pub struct LayoutModelAsset {
    pub path: PathBuf,
    pub sha256: String,
}

fn inspect(path: &Path) -> Result<LayoutModelAsset, PipelineError> {
    let io_error = |source| PipelineError::Io {
        path: path.into(),
        source,
    };
    let mut file = File::open(path).map_err(io_error)?;
    let mut digest = Sha256::new();
    let mut buf = [0; 65536];
    loop {
        let n = file.read(&mut buf).map_err(io_error)?;
        if n == 0 {
            break;
        }
        digest.update(&buf[..n]);
    }
    Ok(LayoutModelAsset {
        path: path.into(),
        sha256: format!("{:x}", digest.finalize()),
    })
}

fn verified(path: &Path, hashes: &[&str]) -> Result<LayoutModelAsset, PipelineError> {
    let asset = inspect(path)?;
    if !hashes.contains(&asset.sha256.as_str()) {
        return Err(PipelineError::Layout(format!(
            "unverified PP-DocLayout-V3 model {} (sha256={})",
            path.display(),
            asset.sha256
        )));
    }
    Ok(asset)
}

/// Prefer the locked bbox export in the configured directory. For the bundled
/// default only, reuse the already installed and verified Python V3 asset read-only.
/// Explicit model directories never silently switch to a different directory.
pub fn resolve(models_dir: &Path) -> Result<LayoutModelAsset, PipelineError> {
    let bbox = models_dir.join("inference_bbox.onnx");
    if bbox.is_file() {
        return verified(&bbox, &[V3_BBOX_SHA256]);
    }
    let bundled = syncpdf_core::fixtures::dir().join("../vendor/models");
    let is_bundled = models_dir
        .canonicalize()
        .ok()
        .zip(bundled.canonicalize().ok())
        .is_some_and(|(actual, default)| actual == default);
    if is_bundled {
        if let Some(home_dir) = std::env::var_os("HOME") {
            let installed =
                PathBuf::from(home_dir).join(".cache/babeldoc/paddle-models/inference_bbox.onnx");
            if installed.is_file() {
                match verified(&installed, &[V3_BBOX_SHA256]) {
                    Ok(asset) => return Ok(asset),
                    Err(error) => {
                        tracing::warn!(%error, "ignoring unverified optional local V3 asset")
                    }
                }
            }
        }
    }
    let legacy = models_dir.join("pp_doc_layoutv3.onnx");
    verified(&legacy, &[V3_BBOX_SHA256, V3_FULL_SHA256, V3_LEGACY_SHA256])
}

/// Runtime override for reproducible CPU/strict CoreML diagnostics.
pub fn parse_device(
    value: Option<&str>,
) -> Result<syncpdf_layout::session::LayoutDevice, PipelineError> {
    use syncpdf_layout::session::LayoutDevice;
    match value.unwrap_or("auto") {
        "auto" => Ok(LayoutDevice::Auto),
        "cpu" => Ok(LayoutDevice::Cpu),
        "coreml" => Ok(LayoutDevice::CoreMl),
        other => Err(PipelineError::Layout(format!(
            "unsupported SYNCPDF_LAYOUT_DEVICE={other}; expected auto, cpu, or coreml"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn device_selection_never_silently_accepts_an_unknown_backend() {
        use syncpdf_layout::LayoutDevice;
        assert_eq!(parse_device(None).unwrap(), LayoutDevice::Auto);
        assert_eq!(parse_device(Some("cpu")).unwrap(), LayoutDevice::Cpu);
        assert_eq!(parse_device(Some("coreml")).unwrap(), LayoutDevice::CoreMl);
        assert!(parse_device(Some("cuda")).is_err());
    }

    #[test]
    fn explicit_unverified_model_does_not_silently_use_local_cache() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("inference_bbox.onnx"), b"not a V3 model").unwrap();
        let error = resolve(dir.path()).unwrap_err().to_string();
        assert!(error.contains("unverified PP-DocLayout-V3"));
        assert!(error.contains("inference_bbox.onnx"));
    }

    #[test]
    fn hash_verification_uses_actual_file_contents() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("model");
        std::fs::write(&path, b"abc").unwrap();
        let asset = verified(
            &path,
            &["ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"],
        )
        .unwrap();
        std::fs::write(&path, b"abcd").unwrap();
        assert!(verified(&path, &[&asset.sha256]).is_err());
    }
}
