import os

CACHE_FOLDER = os.path.join(os.path.expanduser("~"), ".cache", "yadt")


def get_cache_file_path(filename):
    return os.path.join(CACHE_FOLDER, filename)
