"""六类划分的完整性 (apps_catalog.CATEGORIES)。

老板 2026-09-11 定的六类。这条测试存在的理由很具体: 他第一版分法给了五类,
**16 个产品里只放进去 14 个** —— Dify 和 LangChain 谁都没提, 两边都以为在对方那儿。
靠人眼数 16 个名字是数不出来的, 所以让机器数。

漏归类在运行时不会炸: grouped() 会把没归类的挂在末尾照常出卡 (宁可没标题也不能
让新产品从货架上静默消失)。也正因为运行时不炸, 只有这条测试会红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from . import test_webpages as _env

# 顺序是硬的: 隔壁模块在 import 期备好测试环境, app.config 在自己 import 时读 env。
_ENV_READY = _env is not None

from app import apps_catalog as cat  # noqa: E402
from app.main import app  # noqa: E402

I18N = Path(__file__).resolve().parent.parent / "config" / "i18n"
CAT_KEYS = [key for key, _ in cat.CATEGORIES]


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_every_product_is_in_exactly_one_category():
    placed = [i for _, ids in cat.CATEGORIES for i in ids]
    catalog_ids = [a.id for a in cat.CATALOG]
    missing = sorted(set(catalog_ids) - set(placed))
    unknown = sorted(set(placed) - set(catalog_ids))
    dupes = sorted({i for i in placed if placed.count(i) > 1})
    assert not missing, f"这些产品没归类, 会掉进无标题的兜底组: {missing}"
    assert not unknown, f"类目里引用了目录里没有的 id: {unknown}"
    assert not dupes, f"同一个产品归进了两类: {dupes}"


def test_category_keys_are_unique():
    assert len(CAT_KEYS) == len(set(CAT_KEYS)), f"类目 key 重复: {CAT_KEYS}"


@pytest.mark.parametrize("lang", ("zh", "en"))
def test_every_category_has_a_name_and_a_line(lang):
    """类名与那行小字都得有 —— t() 找不到键会把**键名原样印给访客**。
    /apps 上「数字人直播」那张卡就这么印了很久的 `apps.d.live`。"""
    c = json.loads((I18N / f"{lang}.json").read_text())
    missing = [f"apps.cat.{k}{s}" for k in CAT_KEYS for s in ("", "_desc") if not c.get(f"apps.cat.{k}{s}")]
    assert not missing, f"{lang} 缺类目文案: {missing}"


@pytest.mark.parametrize("lang", ("zh", "en"))
def test_every_product_has_a_description(lang):
    """同上, 针对卡片本身的描述。"""
    c = json.loads((I18N / f"{lang}.json").read_text())
    missing = [a.id for a in cat.CATALOG if not c.get(f"apps.d.{a.id}")]
    assert not missing, f"{lang} 缺产品描述, 卡上会印出键名: {missing}"


def test_grouping_keeps_the_usage_order_inside_a_category():
    """分组只改大块的次序。组内仍是 entries_with_status 排好的顺序 (按使用时长),
    那是老板 2026-09-06 定的规矩, 别在这儿顺手废掉。"""
    fake = [{"id": a.id} for a in cat.CATALOG]
    groups = cat.grouped(fake)
    seen = [x["id"] for _, g in groups for x in g]
    assert sorted(seen) == sorted(x["id"] for x in fake), "分组把产品弄丢或弄重了"
    for key, g in groups:
        ids = [x["id"] for x in g]
        order = [x["id"] for x in fake if x["id"] in set(ids)]
        assert ids == order, f"{key} 组内顺序被打乱了"


def test_an_uncategorised_product_still_reaches_the_shelf():
    """忘了归类时**不能把产品从货架上抹掉** —— 页面一切正常、产品没了, 这种错
    没人会发现。它该掉进末尾那个无标题的组。"""
    groups = cat.grouped([{"id": "brand-new-thing"}, {"id": cat.CATALOG[0].id}])
    assert ("", [{"id": "brand-new-thing"}]) in groups


def test_the_shelf_really_renders_those_anchors(client):
    """导航里六条都指向 /apps#<key>。锚点是模板里动态生成的 (`id="{{ cat }}"`),
    静态扫描看不见 —— 所以这里**真把页面渲出来**再找, 否则那六条链接可以全是死的
    而没有任何测试会红。"""
    body = client.get("/apps").text
    missing = [k for k in CAT_KEYS if f'id="{k}"' not in body]
    assert not missing, f"/apps 上没有这些锚点, 导航点过去会落空: {missing}"


def test_the_shelf_shows_every_product_under_a_heading(client):
    """货架上 16 格一个都不能少, 且都在某个类目标题下面。"""
    import json as _json

    zh = _json.loads((I18N / "zh.json").read_text())
    body = client.get("/apps", headers={"accept-language": "zh"}).text
    for a in cat.CATALOG:
        assert a.name in body, f"{a.name} 从货架上消失了"
    for k in CAT_KEYS:
        assert zh[f"apps.cat.{k}"] in body, f"类目标题「{zh[f'apps.cat.{k}']}」没渲染出来"
