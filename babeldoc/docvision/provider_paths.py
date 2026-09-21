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


def target_provider_artifact_path(
    provider_ir_dir, working_dir, filename="provider_ir.json"
):
    """**译文侧** provider 产物的路径：``<agent>/target/provider/<filename>``。

    与源侧的 ``source/provider``（历史 ``source/mineru``）镜像对称，但**没有**历史
    路径要兼容 —— 译文侧识别是编译后新增的一次性产物，只有这一个位置。目录不存在
    也返回路径（调用方负责建目录），与 :func:`provider_artifact_path` 的 ``existing=False``
    分支一致。
    """
    root = (
        Path(provider_ir_dir)
        if provider_ir_dir
        else Path(working_dir) / "agent"
        if working_dir
        else None
    )
    if root is None:
        return None
    return root / "target" / "provider" / filename
