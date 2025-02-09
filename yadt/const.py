import os

CACHE_FOLDER = os.path.join(os.path.expanduser("~"), ".cache", "yadt")


def get_cache_file_path(filename):
    if not os.path.exists(CACHE_FOLDER):
        os.makedirs(CACHE_FOLDER)

    return os.path.join(CACHE_FOLDER, filename)
