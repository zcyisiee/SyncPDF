import shutil
import subprocess
from pathlib import Path

__version__ = "0.1.13"

CACHE_FOLDER = Path.home() / ".cache" / "babeldoc"


def get_cache_file_path(filename: str) -> Path:
    return CACHE_FOLDER / filename


try:
    git_path = shutil.which("git")
    if git_path is None:
        raise FileNotFoundError("git executable not found")

    WATERMARK_VERSION = (
        subprocess.check_output(  # noqa: S603
            [git_path, "describe", "--always"],
            cwd=Path(__file__).resolve().parent,
        )
        .strip()
        .decode()
    )
except (OSError, FileNotFoundError, subprocess.CalledProcessError):
    WATERMARK_VERSION = f"v{__version__}"
