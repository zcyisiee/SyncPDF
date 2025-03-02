import asyncio
import hashlib
import logging
import threading
from pathlib import Path

import httpx
from babeldoc.assets import embedding_assets_metadata
from babeldoc.assets.embedding_assets_metadata import DOC_LAYOUT_ONNX_MODEL_URL
from babeldoc.assets.embedding_assets_metadata import (
    DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256,
)
from babeldoc.assets.embedding_assets_metadata import EMBEDDING_FONT_METADATA
from babeldoc.assets.embedding_assets_metadata import FONT_METADATA_URL
from babeldoc.assets.embedding_assets_metadata import FONT_URL_BY_UPSTREAM
from babeldoc.const import get_cache_file_path
from tenacity import retry
from tenacity import stop_after_attempt
from tenacity import wait_exponential

logger = logging.getLogger(__name__)


class ResultContainer:
    def __init__(self):
        self.result = None

    def set_result(self, result):
        self.result = result


def run_in_another_thread(coro):
    result_container = ResultContainer()

    def _wrapper():
        result_container.set_result(asyncio.run(coro))

    thread = threading.Thread(target=_wrapper)
    thread.start()
    thread.join()
    return result_container.result


def run_coro(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        return run_in_another_thread(coro)


def _retry_if_not_cancelled_and_failed(retry_state):
    """Only retry if the exception is not CancelledError and the attempt failed."""
    if retry_state.outcome.failed:
        exception = retry_state.outcome.exception()
        # Don't retry on CancelledError
        if isinstance(exception, asyncio.CancelledError):
            logger.debug("Operation was cancelled, not retrying")
            return False
        # Retry on network related errors
        if isinstance(
            exception, httpx.HTTPError | ConnectionError | ValueError | TimeoutError
        ):
            logger.warning(f"Network error occurred: {exception}, will retry")
            return True
    # Don't retry on success
    return False


def verify_file(path: Path, sha3_256: str):
    if not path.exists():
        return False
    with path.open("rb") as f:
        hash_ = hashlib.file_digest(f, "sha3_256")
    return hash_.hexdigest() == sha3_256


@retry(
    retry=_retry_if_not_cancelled_and_failed,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    before_sleep=lambda retry_state: logger.warning(
        f"Download file failed, retrying in {retry_state.next_action.sleep} seconds... "
        f"(Attempt {retry_state.attempt_number}/3)"
    ),
)
async def download_file(
    client: httpx.AsyncClient | None = None,
    url: str = None,
    path: Path = None,
    sha3_256: str = None,
):
    if client is None:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, follow_redirects=True)
    else:
        response = await client.get(url, follow_redirects=True)

    response.raise_for_status()
    with path.open("wb") as f:
        f.write(response.content)
    if not verify_file(path, sha3_256):
        path.unlink(missing_ok=True)
        raise ValueError(f"File {path} is corrupted")


@retry(
    retry=_retry_if_not_cancelled_and_failed,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    before_sleep=lambda retry_state: logger.warning(
        f"Get font metadata failed, retrying in {retry_state.next_action.sleep} seconds... "
        f"(Attempt {retry_state.attempt_number}/3)"
    ),
)
async def get_font_metadata(
    client: httpx.AsyncClient | None = None, upstream: str = None
):
    if upstream not in FONT_METADATA_URL:
        logger.critical(f"Invalid upstream: {upstream}")
        exit(1)

    if client is None:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                FONT_METADATA_URL[upstream], follow_redirects=True
            )
    else:
        response = await client.get(FONT_METADATA_URL[upstream], follow_redirects=True)

    response.raise_for_status()
    logger.debug(f"Get font metadata from {upstream} success")
    return upstream, response.json()


async def get_fastest_upstream_for_font(
    client: httpx.AsyncClient | None = None, exclude_upstream: list[str] = None
):
    tasks: list[asyncio.Task[tuple[str, dict]]] = []
    for upstream in FONT_METADATA_URL:
        if exclude_upstream and upstream in exclude_upstream:
            continue
        tasks.append(asyncio.create_task(get_font_metadata(client, upstream)))
    for future in asyncio.as_completed(tasks):
        try:
            result = await future
            for task in tasks:
                if not task.done():
                    task.cancel()
            return result
        except Exception as e:
            logger.exception(f"Error getting font metadata: {e}")
    logger.error("All upstreams failed")
    return None, None


async def get_fastest_upstream_for_model(client: httpx.AsyncClient | None = None):
    return await get_fastest_upstream_for_font(client, exclude_upstream=["github"])


async def get_fastest_upstream(client: httpx.AsyncClient | None = None):
    (
        fastest_upstream_for_font,
        online_font_metadata,
    ) = await get_fastest_upstream_for_font(client)
    if fastest_upstream_for_font is None:
        logger.error("Failed to get fastest upstream")
        exit(1)

    if fastest_upstream_for_font == "github":
        # since github is only store font, we need to get the fastest upstream for model
        fastest_upstream_for_model, _ = await get_fastest_upstream_for_model(client)
        if fastest_upstream_for_model is None:
            logger.error("Failed to get fastest upstream")
            exit(1)
    else:
        fastest_upstream_for_model = fastest_upstream_for_font

    return online_font_metadata, fastest_upstream_for_font, fastest_upstream_for_model


async def get_doclayout_onnx_model_path_async(client: httpx.AsyncClient | None = None):
    onnx_path = get_cache_file_path(
        "doclayout_yolo_docstructbench_imgsz1024.onnx", "model"
    )
    if verify_file(onnx_path, DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256):
        return onnx_path

    logger.info("doclayout onnx model not found or corrupted, downloading...")
    fastest_upstream, _ = await get_fastest_upstream_for_model(client)
    if fastest_upstream is None:
        logger.error("Failed to get fastest upstream")
        exit(1)

    url = DOC_LAYOUT_ONNX_MODEL_URL[fastest_upstream]

    await download_file(
        client, url, onnx_path, DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256
    )
    logger.info(f"Download doclayout onnx model from {fastest_upstream} success")
    return onnx_path


def get_doclayout_onnx_model_path():
    return run_coro(get_doclayout_onnx_model_path_async())


def get_font_url_by_name_and_upstream(font_file_name: str, upstream: str):
    if upstream not in FONT_URL_BY_UPSTREAM:
        logger.critical(f"Invalid upstream: {upstream}")
        exit(1)

    return FONT_URL_BY_UPSTREAM[upstream](font_file_name)


async def get_font_and_metadata_async(
    font_file_name: str,
    client: httpx.AsyncClient | None = None,
    fastest_upstream: str | None = None,
    font_metadata: dict | None = None,
):
    cache_file_path = get_cache_file_path(font_file_name, "fonts")
    if font_file_name in EMBEDDING_FONT_METADATA and verify_file(
        cache_file_path, EMBEDDING_FONT_METADATA[font_file_name]["sha3_256"]
    ):
        return cache_file_path, EMBEDDING_FONT_METADATA[font_file_name]

    logger.info(f"Font {cache_file_path} not found or corrupted, downloading...")
    if fastest_upstream is None:
        fastest_upstream, font_metadata = await get_fastest_upstream_for_font(client)
        if fastest_upstream is None:
            logger.critical("Failed to get fastest upstream")
            exit(1)

        if font_file_name not in font_metadata:
            logger.critical(f"Font {font_file_name} not found in {font_metadata}")
            exit(1)

        if verify_file(cache_file_path, font_metadata[font_file_name]["sha3_256"]):
            return cache_file_path, font_metadata[font_file_name]

    assert font_metadata is not None

    url = get_font_url_by_name_and_upstream(font_file_name, fastest_upstream)
    if "sha3_256" not in font_metadata[font_file_name]:
        logger.critical(f"Font {font_file_name} not found in {font_metadata}")
        exit(1)
    await download_file(
        client, url, cache_file_path, font_metadata[font_file_name]["sha3_256"]
    )
    return cache_file_path, font_metadata[font_file_name]


def get_font_and_metadata(font_file_name: str):
    return run_coro(get_font_and_metadata_async(font_file_name))


def get_font_family(lang_code: str):
    font_family = embedding_assets_metadata.get_font_family(lang_code)
    return font_family


async def download_all_fonts_async(client: httpx.AsyncClient | None = None):
    fastest_upstream, font_metadata = await get_fastest_upstream_for_font(client)
    if fastest_upstream is None:
        logger.error("Failed to get fastest upstream")
        exit(1)

    font_tasks = [
        asyncio.create_task(
            get_font_and_metadata_async(
                font_file_name, client, fastest_upstream, font_metadata
            )
        )
        for font_file_name in EMBEDDING_FONT_METADATA
    ]
    await asyncio.gather(*font_tasks)


async def async_warmup():
    logger.info("Warming up...")
    async with httpx.AsyncClient() as client:
        onnx_task = asyncio.create_task(get_doclayout_onnx_model_path_async(client))
        font_tasks = asyncio.create_task(download_all_fonts_async(client))
        await asyncio.gather(onnx_task, font_tasks)


def warmup():
    run_coro(async_warmup())


if __name__ == "__main__":
    from rich.logging import RichHandler

    logging.basicConfig(level=logging.DEBUG, handlers=[RichHandler()])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    warmup()
