"""预览 WebP、对照版按需生成与缓存清理。"""

from __future__ import annotations

import io
import json
import os
import time
from urllib.parse import unquote

from babeldoc_tools.cloud.cleanup import cleanup
from babeldoc_tools.cloud.cleanup import evict
from babeldoc_tools.cloud.files import PREVIEW_WIDTH
from conftest import wait_for
from PIL import Image


def _webp(response) -> Image.Image:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/webp"
    assert "immutable" in response.headers["cache-control"]
    return Image.open(io.BytesIO(response.content))


def test_previews_render_1600px_webp_and_are_cached_by_content(cloud):
    client = cloud.client()
    job = cloud.upload(client, cloud.pdf("p.pdf")).json()
    base = f"/api/jobs/{job['id']}/pages"

    image = _webp(client.get(f"{base}/1.webp?v=src"))
    assert image.size == (PREVIEW_WIDTH, round(PREVIEW_WIDTH * 792 / 612))
    sha = cloud.service.db.one("SELECT sha256 FROM sources")[0]
    cached = cloud.paths.preview / sha / "1.webp"
    assert cached.is_file()
    os.utime(cached, (1, 1))
    _webp(client.get(f"{base}/1.webp?v=src"))
    assert cached.stat().st_mtime > 1  # 命中刷新访问时间，淘汰按最近访问

    assert client.get(f"{base}/1.webp?v=tr").status_code == 404  # 还在排队
    assert client.get(f"{base}/3.webp?v=src").status_code == 404
    assert client.get(f"{base}/1.webp?v=raw").status_code == 400

    cloud.runner.run_once()
    view = client.get(f"/api/jobs/{job['id']}").json()
    _webp(client.get(f"{base}/2.webp?v=tr&rev={view['final_rev']}"))
    translated_sha = cloud.service.db.one("SELECT translated_sha FROM translations")[0]
    assert (cloud.paths.preview / translated_sha / "2.webp").is_file()


def test_live_translated_preview_uses_attempt_revision_key(cloud, monkeypatch):
    import threading

    gate = cloud.tmp / "gate"
    monkeypatch.setenv("FAKE_GATE", str(gate))
    client = cloud.client()
    job = cloud.upload(client, cloud.pdf("live.pdf")).json()
    thread = threading.Thread(target=cloud.runner.run_once)
    thread.start()
    try:
        page = wait_for(lambda: next(
            (e for e in cloud.service.events_after(job["id"], 0) if e["kind"] == "page"), None
        ))
        response = client.get(f"/api/jobs/{job['id']}/pages/1.webp?v=tr&rev={page['rev']}")
        _webp(response)
        tid = cloud.service.db.one("SELECT id FROM translations")[0]
        assert (cloud.paths.preview / f"{tid}-{page['rev']}" / "1.webp").is_file()
        bad = client.get(f"/api/jobs/{job['id']}/pages/1.webp?v=tr&rev=../../x")
        assert bad.status_code == 404
    finally:
        gate.touch()
        thread.join(timeout=15)


def test_dual_is_generated_once_on_first_download(cloud):
    client = cloud.client()
    job = cloud.upload(client, cloud.pdf("Deep Paper.pdf")).json()
    assert client.get(f"/api/jobs/{job['id']}/download?kind=dual").status_code == 409
    cloud.runner.run_once()

    translated = client.get(f"/api/jobs/{job['id']}/download")
    assert translated.status_code == 200 and translated.content.startswith(b"%PDF-")
    assert "Deep Paper-中文.pdf" in unquote(translated.headers["content-disposition"])

    calls = cloud.tmp / "engine-calls.jsonl"
    before = len(calls.read_text().splitlines())
    for _ in range(2):
        dual = client.get(f"/api/jobs/{job['id']}/download?kind=dual")
        assert dual.status_code == 200
        assert "Deep Paper-中英对照.pdf" in unquote(dual.headers["content-disposition"])
    dual_calls = [json.loads(line) for line in calls.read_text().splitlines()[before:]]
    assert [c[0] for c in dual_calls] == ["dual"]  # 第二次走缓存
    tid = cloud.service.db.one("SELECT id FROM translations")[0]
    assert (cloud.paths.dual / f"{tid}.pdf").is_file()
    assert client.get(f"/api/jobs/{job['id']}/download?kind=zip").status_code == 400


def test_evict_removes_least_recently_used_until_under_limit(tmp_path):
    root = tmp_path / "preview"
    for index, key in enumerate(("old", "mid", "new")):
        path = root / key / "1.webp"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * 100)
        os.utime(path, (1000 + index, 1000 + index))
    assert evict(root, 250) == 1
    assert sorted(p.name for p in root.iterdir()) == ["mid", "new"]  # 空目录一并收掉
    assert evict(root, 1000) == 0


def test_cleanup_drops_expired_caches_but_never_sources_or_translations(cloud):
    client = cloud.client()
    job = cloud.upload(client, cloud.pdf("keep.pdf")).json()
    cloud.runner.run_once()
    client.get(f"/api/jobs/{job['id']}/download?kind=dual")
    tid = cloud.service.db.one("SELECT id FROM translations")[0]
    events = cloud.paths.translations / tid / "events.jsonl.gz"
    dual = cloud.paths.dual / f"{tid}.pdf"
    stale_tmp = cloud.paths.tmp / "abandoned.pdf"
    stale_tmp.write_bytes(b"%PDF-")
    old = time.time() - 8 * 24 * 3600
    for path in (events, dual, stale_tmp):
        os.utime(path, (old, old))

    report = cleanup(cloud.service.db, cloud.paths)
    assert (report["events"], report["dual"], report["tmp"]) == (1, 1, 1)
    assert not events.exists() and not dual.exists() and not stale_tmp.exists()
    assert cloud.paths.translated(tid).is_file() and any(cloud.paths.sources.iterdir())
    # 删掉的对照版下次下载时重建
    assert client.get(f"/api/jobs/{job['id']}/download?kind=dual").status_code == 200
