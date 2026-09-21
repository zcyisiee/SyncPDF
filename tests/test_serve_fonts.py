"""``bdt serve`` 的 ``GET /api/v1/fonts``：段落级中文字体族清单。

前端用它渲染"中文字体"下拉，选中的 ``id`` 写进草稿 ``layout.font_family``（仅局部块
编译消费）。本文件钉住两件事：

- **清单是注册表的投影**：顺序与 ``font_families.FONT_FAMILIES`` 一致，``serif`` 直接
  来自注册表（不依赖本机环境，前端可据此预告拉丁字形会不会跟着换）；
- **``available`` 只是本机事实，不是错误**：族字体文件没探测到 / LaTeX 能力探测直接
  炸了，接口仍是 200，只是全部 ``available=false``（请求该族仍可提交，渲染回落默认族）。

探测真起 ``kpsewhich`` 子进程，所以大多数用例用 monkeypatch 把它换掉；只有形状用例
走真实探测（在没装字体的机器上也成立，因为断言不碰 ``available`` 的值）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc.format.pdf.document_il.backend.latex_bbox import (  # noqa: E402
    capability as capability_module,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.capability import (  # noqa: E402
    LatexCapability,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.font_families import (  # noqa: E402
    FONT_FAMILIES,
)
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.routers import fonts as fonts_module  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX as API  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FONTS = f"{API}/fonts"

#: 注册表顺序（响应顺序的唯一判据）。
REGISTRY_IDS = [spec.id for spec in FONT_FAMILIES]


def _make_workdir(root: Path, did: str) -> Path:
    workdir = root / did
    (workdir / "agent").mkdir(parents=True)
    (workdir / "agent" / "run_state.json").write_text("{}", encoding="utf-8")
    return workdir


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    base.mkdir()
    _make_workdir(base, "alpha")
    return base


@pytest.fixture
def client(root: Path):
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 形状与注册表投影
# --------------------------------------------------------------------------- #
def test_fonts_lists_registry_in_order_with_frozen_shape(client):
    """200 + 非空 + 四项形状 + id 顺序/取值与 ``FONT_FAMILIES`` 一致。"""
    response = client.get(FONTS)
    assert response.status_code == 200, response.text
    items = response.json()
    assert items, "字体族清单不该为空"
    assert [item["id"] for item in items] == REGISTRY_IDS
    for item, spec in zip(items, FONT_FAMILIES, strict=True):
        assert set(item) == {"id", "label", "serif", "available"}
        assert item == {
            "id": spec.id,
            "label": spec.label,
            "serif": spec.serif,
            "available": item["available"],
        }
        assert isinstance(item["available"], bool)


def test_fonts_is_read_only(client):
    """只读端点：非 GET 一律 405（写端点白名单在 ``test_serve_app.py`` 另钉）。"""
    assert client.post(FONTS).status_code == 405
    assert client.delete(FONTS).status_code == 405


# --------------------------------------------------------------------------- #
# available：本机事实，探测失败不报错
# --------------------------------------------------------------------------- #
def test_fonts_are_all_unavailable_when_no_family_fonts_probed(client, monkeypatch):
    """探测到 0 个族字体 → 全部 ``available=false``，接口仍 200（不是错误）。"""

    def probe(**_kwargs):
        return LatexCapability(available=True, cjk_family_fonts={})

    monkeypatch.setattr(capability_module, "probe_latex_capability", probe)
    response = client.get(FONTS)
    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["id"] for item in items] == REGISTRY_IDS
    assert [item["available"] for item in items] == [False] * len(REGISTRY_IDS)
    # serif 仍来自注册表：缺字体不影响前端预告拉丁字形。
    assert [item["serif"] for item in items] == [spec.serif for spec in FONT_FAMILIES]


def test_fonts_mark_only_the_probed_families_available(client, monkeypatch):
    """只探测到一部分族 → 只有那些 ``available=true``（逐族判断，不是全或无）。"""
    probed = {spec.id for spec in FONT_FAMILIES[:2]}

    def probe(**_kwargs):
        return LatexCapability(
            available=True,
            cjk_family_fonts={fid: {"regular": "/fonts/x.ttf"} for fid in probed},
        )

    monkeypatch.setattr(capability_module, "probe_latex_capability", probe)
    items = client.get(FONTS).json()
    assert {item["id"]: item["available"] for item in items} == {
        spec.id: spec.id in probed for spec in FONT_FAMILIES
    }


def test_fonts_are_all_unavailable_when_latex_unavailable(client, monkeypatch):
    """LaTeX 整体不可用（缺 xelatex/宏包）→ 全部 ``available=false``，仍 200。

    字体文件探测到了也不算可用：本机出不了图，选哪一族都拿不到贴片。
    """

    def probe(**_kwargs):
        return LatexCapability(
            available=False,
            reasons=["未找到 xelatex 可执行文件"],
            cjk_family_fonts={
                spec.id: {"regular": "/fonts/x.ttf"} for spec in FONT_FAMILIES
            },
        )

    monkeypatch.setattr(capability_module, "probe_latex_capability", probe)
    response = client.get(FONTS)
    assert response.status_code == 200, response.text
    assert [item["available"] for item in response.json()] == [False] * len(
        REGISTRY_IDS
    )


def test_fonts_stay_200_when_capability_probe_raises(client, monkeypatch):
    """探测本身抛异常（kpsewhich 崩了）→ 全部 ``available=false``，仍 200。"""

    def probe(**_kwargs):
        raise RuntimeError("kpsewhich 挂了")

    monkeypatch.setattr(capability_module, "probe_latex_capability", probe)
    response = client.get(FONTS)
    assert response.status_code == 200, response.text
    assert [item["available"] for item in response.json()] == [False] * len(
        REGISTRY_IDS
    )


def test_fonts_router_without_block_compiler_returns_all_unavailable():
    """没有块编译器可借（拿不到探测结果）时清单照发，全部 ``available=false``。"""
    app = FastAPI()
    app.include_router(fonts_module.fonts_router(SimpleNamespace()))
    with TestClient(app) as test_client:
        response = test_client.get(FONTS)
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == REGISTRY_IDS
    assert all(item["available"] is False for item in response.json())


# --------------------------------------------------------------------------- #
# font_family_items：注册表 → 响应条目的映射
# --------------------------------------------------------------------------- #
def test_font_family_items_asks_capability_once_per_registry_id():
    """``available`` 直接取 ``capability.cjk_family(id)``，每个 id 各问一次。"""

    class FakeCapability:
        def __init__(self, available_ids):
            self.available = True
            self._available = set(available_ids)
            self.asked: list[str] = []

        def cjk_family(self, family_id):
            self.asked.append(family_id)
            return {"regular": "/x.ttf"} if family_id in self._available else None

    first = FONT_FAMILIES[0].id
    capability = FakeCapability([first])
    items = fonts_module.font_family_items(capability)

    assert capability.asked == REGISTRY_IDS
    assert [item.id for item in items] == REGISTRY_IDS
    assert [item.available for item in items] == [
        spec.id == first for spec in FONT_FAMILIES
    ]
    assert items[0].model_dump() == {
        "id": FONT_FAMILIES[0].id,
        "label": FONT_FAMILIES[0].label,
        "serif": FONT_FAMILIES[0].serif,
        "available": True,
    }


def test_font_family_items_without_capability_is_all_unavailable():
    """``capability=None``（探测不可用）→ 全部 ``available=false``，不抛异常。"""
    items = fonts_module.font_family_items(None)
    assert [item.id for item in items] == REGISTRY_IDS
    assert all(item.available is False for item in items)


def test_font_family_items_respects_latex_availability_flag():
    """``capability.available=False`` 时即使族字体都在也全部不可用（不是逐族判断）。"""
    capability = LatexCapability(
        available=False,
        cjk_family_fonts={spec.id: {"regular": "/x.ttf"} for spec in FONT_FAMILIES},
    )
    items = fonts_module.font_family_items(capability)
    assert [item.available for item in items] == [False] * len(REGISTRY_IDS)

    # 同一份字体表，能力可用时逐族转成 true。
    capability.available = True
    assert all(item.available for item in fonts_module.font_family_items(capability))
