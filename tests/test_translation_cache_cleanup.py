from concurrent.futures import ThreadPoolExecutor

from babeldoc.translator.cache import TranslationCache
from babeldoc.translator.cache import _TranslationCache
from babeldoc.translator.cache import clean_test_db
from babeldoc.translator.cache import init_test_db


def _prepare_records(cache: TranslationCache, num_records: int) -> None:
    """Insert *num_records* unique records into the cache."""
    for i in range(num_records):
        cache.set(f"text_{i}", f"translation_{i}")


def test_cleanup_under_limit(monkeypatch):
    """When total rows < MAX_CACHE_ROWS, cleanup should do nothing."""
    # Create an isolated test database
    test_db = init_test_db()
    try:
        cache = TranslationCache("dummy")
        # Make cleanup run every time for deterministic behaviour
        monkeypatch.setattr("babeldoc.translator.cache.CLEAN_PROBABILITY", 1.0)
        # Lower the MAX_CACHE_ROWS threshold for quick test execution
        monkeypatch.setattr("babeldoc.translator.cache.MAX_CACHE_ROWS", 1000)

        _prepare_records(cache, 900)
        cache.set("extra", "extra")  # This triggers cleanup
        assert _TranslationCache.select().count() == 901
    finally:
        clean_test_db(test_db)


def test_cleanup_over_limit(monkeypatch):
    """When rows > MAX_CACHE_ROWS, cleanup should trim to the limit."""
    test_db = init_test_db()
    try:
        cache = TranslationCache("dummy")
        monkeypatch.setattr("babeldoc.translator.cache.CLEAN_PROBABILITY", 1.0)
        monkeypatch.setattr("babeldoc.translator.cache.MAX_CACHE_ROWS", 500)

        total_records = 750
        _prepare_records(cache, total_records)
        cache.set("extra", "extra")

        assert _TranslationCache.select().count() <= 500  # capped at limit
    finally:
        clean_test_db(test_db)


def test_cleanup_thread_safety(monkeypatch):
    """Multiple threads attempting cleanup concurrently should not raise errors."""
    test_db = init_test_db()
    try:
        cache = TranslationCache("dummy")
        monkeypatch.setattr("babeldoc.translator.cache.CLEAN_PROBABILITY", 1.0)
        monkeypatch.setattr("babeldoc.translator.cache.MAX_CACHE_ROWS", 500)

        def task(n):
            cache.set(f"text_{n}", f"translation_{n}")

        # Use a pool of threads to stress cleanup
        with ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(task, range(600))

        # After all threads complete, ensure table size is capped
        assert _TranslationCache.select().count() <= 500
    finally:
        clean_test_db(test_db)
