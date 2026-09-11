"""直播控制台只给**设计过的**形象+音色搭配, 不给自由组合。

上游 /config 返回 persons(5) 与 voices(7) 两个互不相干的列表 —— 自由组合是 35 种,
而设计过的只有 5 种。其余 30 种是意外, 最难受的是女性形象配上 yunjian/yunxi 这类
男声, 一开口就穿帮。旧控制台还有第二层坑: 两个下拉互不联动, 换了形象音色留在原地,
于是**静默错配**而界面毫无提示 (2026-09-10 创始人截图撞到: lin 的脸配着 xiaoya)。
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-preset-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, live  # noqa: E402
from app.main import app  # noqa: E402
from tests._signup import signup  # noqa: E402

db.ensure_schema()

#: 这五对来自五个直播间 room.json 里当初存下的搭配。钉死它们 —— 改这张表就是改产品,
#: 应该是有意为之, 不该是谁顺手动了一下。
DESIGNED = {
    ("source-v3-head", "xiaoya"),
    ("hao", "yunxi"),
    ("chen", "yunjian"),
    ("yue", "hsiaochen"),
    ("lin", "xiaoxiao"),
}


def test_only_the_designed_pairs_exist():
    got = {(p["person"], p["voice"]) for p in live.LIVE_PRESETS}
    assert got == DESIGNED, f"搭配表被改了: 多了 {got - DESIGNED}, 少了 {DESIGNED - got}"
    assert len({p["id"] for p in live.LIVE_PRESETS}) == len(live.LIVE_PRESETS), "预设 id 撞了"


def test_preset_of_falls_back_but_never_returns_none():
    """收敛存量组合。**绝不返回 None** —— 上层拿它填下拉, 空值会让选择器瞎掉。"""
    assert live.preset_of("lin", "xiaoxiao")["id"] == "lin", "整对精确匹配都不中"
    # 存量房间可能存着自由搭配 (旧控制台两个下拉各选各的) —— 按形象收敛回它自己那对
    got = live.preset_of("lin", "xiaoya")
    assert got["person"] == "lin" and got["voice"] == "xiaoxiao", f"错配没被收敛回 lin 自己的音色, 拿到 {got}"
    assert live.preset_of("", "")["id"] == live.LIVE_PRESETS[0]["id"], "全不认识时没给兜底"
    assert live.preset_of("不存在的形象", "不存在的音色") is not None


def _admin(email="preset-boss@t.local"):
    config.ADMIN_EMAILS = [email]
    c = TestClient(app)
    signup(c, email)
    return c


def test_console_offers_exactly_the_designed_pairs(monkeypatch):
    """控制台渲染出来的就是那 5 个, 而且**没有第二个各选各的音色下拉**。"""
    monkeypatch.setattr(config, "ADMIN_EMAILS", ["preset-boss@t.local"])
    html = _admin().get("/live/console").text

    assert 'id="lvVoice"' not in html, "音色又变成独立下拉了 —— 自由组合会回来"
    assert 'id="lvPreset"' in html, "没有渲染出搭配选择器"

    import re

    opts = re.findall(r'<option[^>]*data-person="([^"]+)"[^>]*data-voice="([^"]+)"', html)
    assert set(opts) == DESIGNED, f"页面上的搭配与设计不符: {opts}"
    assert len(opts) == 5, f"渲染了 {len(opts)} 个搭配, 应该是 5 个"


def test_console_labels_are_translated(monkeypatch):
    """标签走 i18n —— 漏了 key 的话页面上会露出 live.preset.xxx 这种原文。"""
    monkeypatch.setattr(config, "ADMIN_EMAILS", ["preset-boss@t.local"])
    html = _admin().get("/live/console").text
    for p in live.LIVE_PRESETS:
        assert f"live.preset.{p['id']}" not in html, f"live.preset.{p['id']} 没有翻译, 键名漏到页面上了"


# ── 人设 (2026-09-10) ────────────────────────────────────────────────────────
#
# 在这之前直播这边只有形象和音色, 回评论用的是一条**没有身份**的通用提示词 ——
# 换哪个形象她都是同一个没名字的人。而 1:1 通话页早就有五个有名有姓的人设
# (avatar.js 的 PRESETS)。创始人 2026-09-10: "我每一次切形象·音色·人设, 你就给我
# 切对应的人设"。


def _i18n(lang: str = "zh") -> dict:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "config" / "i18n"
    return json.loads((root / f"{lang}.json").read_text("utf-8"))


def test_每套搭配都要有名字和人设():
    for p in live.LIVE_PRESETS:
        for k in ("name", "trait", "persona"):
            assert p.get(k), f"{p['id']} 缺 {k} —— 下拉里会显示成空的"
        assert len(p["persona"]) > 8, f"{p['id']} 的人设太短, 起不到作用"


def test_同一个形象在通话页和直播间必须是同一个名字():
    """观众在通话里见到的是"初雪 · 温柔", 直播间也得是她。

    两处各写各的话, 改了一处忘了另一处, 就变成两个人 —— 而这正是 2026-09-10
    之前的状态: 直播间的下拉写的是音色名(chen · 云健沉稳), 通话页写的是人设名
    (晨 · 沉稳)。
    """
    zh = _i18n("zh")
    for p in live.LIVE_PRESETS:
        key = "js.avatar.p." + p["id"]
        assert key in zh, f"通话页没有 {p['id']} 这个形象, 两边的清单对不上"
        assert zh[key] == f"{p['name']} · {p['trait']}", (
            f"{p['id']} 两处名字不一致: 直播间 {p['name']} · {p['trait']}, 通话页 {zh[key]}"
        )


def test_下拉标签中英成对且带上人设():
    zh, en = _i18n("zh"), _i18n("en")
    for p in live.LIVE_PRESETS:
        key = "live.preset." + p["id"]
        assert key in zh and key in en, f"{key} 中英没配齐"
        assert p["name"] in zh[key], f"{key} 的中文标签里没有人设名 {p['name']}"
    assert "人设" in zh["live.persona"], "字段名还写着「形象 · 音色」, 没提人设"


def test_人设是加在底线前面_不是替换掉它():
    """人设只管"怎么说话", 不管"能说什么"。

    顺序反了(或者替换了)等于让人设去覆盖底线 —— 不编造价格库存、被问就承认是数字人
    这些是要担责的东西, 必须由 _REPLY 兜着, 而且排在后面更靠近输出。
    """
    import asyncio
    from unittest import mock

    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "好的呀"}}]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen["system"] = json["messages"][0]["content"]
            return _Resp()

    with (
        mock.patch.object(live.config, "UPSTREAM_BASE_URL", "http://x/v1"),
        mock.patch.object(live.config, "UPSTREAM_API_KEY", "k"),
        mock.patch.object(live.httpx, "AsyncClient", lambda **kw: _Client()),
        mock.patch.object(live.credits, "spend", lambda *a, **kw: None),
    ):
        asyncio.run(live._compose_reply("在吗", "u_1", person="chen"))

    sys_prompt = seen["system"]
    assert "你叫晨" in sys_prompt, "没带上人设"
    assert live._REPLY in sys_prompt, "人设把底线替换掉了"
    assert sys_prompt.index("你叫晨") < sys_prompt.index(live._REPLY), (
        "人设排在底线后面 —— 越靠近输出约束越强, 顺序反了等于让人设压过底线"
    )


def test_没给形象时退回通用口径():
    """取不到房间配置不该让她答不出来。"""
    import asyncio
    from unittest import mock

    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "好"}}]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen["system"] = json["messages"][0]["content"]
            return _Resp()

    with (
        mock.patch.object(live.config, "UPSTREAM_BASE_URL", "http://x/v1"),
        mock.patch.object(live.config, "UPSTREAM_API_KEY", "k"),
        mock.patch.object(live.httpx, "AsyncClient", lambda **kw: _Client()),
        mock.patch.object(live.credits, "spend", lambda *a, **kw: None),
    ):
        asyncio.run(live._compose_reply("在吗", "u_1", person=""))

    assert seen["system"] == live._REPLY, "没给形象却硬塞了一个人设进去"


def test_房间的形象要真的传到回评论那条路(monkeypatch):
    """人设是靠 person 查出来的 —— 传不到就等于没有人设。

    ⚠️ 这条是配合 test_live_comments 里的替身写的: 那个替身少一个参数时, 真实调用
    是 TypeError, 而 _maybe_reply 一律吞异常(评论已经飘出去了), 表现成"她就是不接
    话" —— 光看现象查不到根因。
    """
    import asyncio

    from tests.test_live_comments import Upstream

    up = Upstream(live_=True)
    up.state["person"] = "chen"
    up.install(monkeypatch, reply="好的")
    monkeypatch.setattr(live, "_LAST_REPLY_AT", {})  # 多间之后按房间记冷却

    assert asyncio.run(live._maybe_reply("c_1", "在吗", live.rooms()[0])) is True
    assert up.person_seen == "chen", f"房间的形象没传到回评论那条路 (拿到 {up.person_seen!r}) —— 人设不会生效"


def test_控制台要能看出产出低于实时(monkeypatch):
    """低于 1.0 = 观众必卡, 而这在界面上本来完全看不出来。

    形象下拉只有名字, 看不出哪个走云端 TTS(实测 2.4x)、哪个走自建(0.7x) ——
    2026-09-10 创始人因此以为自己换了云端却还卡, 实际配置里从没换过。
    """
    live._RATE["pts"] = [(1000.0, 0.0), (1100.0, 87.0)]  # 0.87x, 跨度 100 秒
    assert live._rate_now() == 0.87

    live._RATE["pts"] = [(1000.0, 0.0), (1005.0, 5.0)]  # 跨度才 5 秒
    assert live._rate_now() == 0.0, "采样不够就该回 0, 别拿短窗的数去吓人"
