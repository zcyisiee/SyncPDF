from pathlib import Path

CACHE_FOLDER = Path.home() / ".cache" / "babeldoc"


def get_cache_file_path(filename):
    return CACHE_FOLDER / filename
