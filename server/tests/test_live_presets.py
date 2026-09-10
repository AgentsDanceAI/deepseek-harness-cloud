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
