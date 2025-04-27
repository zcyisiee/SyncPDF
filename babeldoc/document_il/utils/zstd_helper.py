import base64

import zstd


def zstd_compress(data) -> str:
    if isinstance(data, str):
        data = data.encode()
    if not isinstance(data, bytes):
        raise TypeError(f"data must be str or bytes, not {type(data)}")

    return base64.b85encode(zstd.compress(data)).decode()


def zstd_decompress(data) -> str:
    if isinstance(data, str):
        data = data.encode()
    if not isinstance(data, bytes):
        raise TypeError(f"data must be str or bytes, not {type(data)}")

    return zstd.decompress(base64.b85decode(data)).decode()
