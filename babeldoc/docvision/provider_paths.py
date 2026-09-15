"""Backend-neutral provider artifacts, with explicit historical replay lookup."""

from pathlib import Path


def provider_artifact_path(
    provider_ir_dir, working_dir, filename="provider_ir.json", *, existing=False
):
    root = (
        Path(provider_ir_dir)
        if provider_ir_dir
        else Path(working_dir) / "agent"
        if working_dir
        else None
    )
    if root is None:
        return None
    primary = root / "source" / "provider" / filename
    if not existing or primary.is_file():
        return primary
    # Historical workdirs stay readable; new Paddle workdirs always use primary.
    legacy = root / "source" / "mineru" / filename
    return legacy if legacy.is_file() else None
