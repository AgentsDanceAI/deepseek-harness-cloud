"""直播间公屏与自动回评。

老板 2026-09-09 拍板: **登录才能发** + **每一条都自动回**。第二条在"任何登录用户
都能触发"的前提下, 有三个花钱/翻车的口子, 这个文件钉的就是它们:

  · 钱记谁头上 —— 观众只是发了句话, 没同意花钱。记他头上是乱扣。
  · 她的嘴是串行的 —— 一句念十来秒。队列深了还硬塞, 话术一句都播不出去。
  · **观众绝不能拿到 echo** —— echo 是把文字原样念出去, 等于谁都能让她说任何话。
"""

from __future__ import annotations

import os
import tempfile
import time

import pytest

_TMP = tempfile.mkdtemp(prefix="dhc-live-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, live, rate_limit  # noqa: E402
from app.main import app  # noqa: E402
from tests._signup import signup  # noqa: E402

db.ensure_schema()


@pytest.fixture(autouse=True)
def room(monkeypatch):
    monkeypatch.setattr(config, "LIVE_GPU_URL", "http://gpu.test/live")
    monkeypatch.setattr(config, "LIVE_ROOM", "official")
    monkeypatch.setattr(config, "AVATAR_TOKEN_SECRET", "s3cret")
    monkeypatch.setattr(live, "_LAST_REPLY_AT", 0.0)
    rate_limit._windows.clear()
    with db.tx() as c:
        c.execute("DELETE FROM live_comments")
    yield


class Upstream:
    """假的 GPU 侧。记下被要求说了什么。"""

    def __init__(self, live_=True, queued=0):
        self.state = {"live": live_, "queued": queued, "title": "t", "lines": []}
        self.said: list[str] = []

    def install(self, monkeypatch, *, reply="好的，这个我记下了。"):
        async def fake_gpu(method, path, room, **kw):
            if path.endswith("/interject"):
                self.said.append(kw["json"]["text"])
                return {"ok": True}
            return dict(self.state)

        async def fake_compose(comment, bill_to, device_id=""):
            self.billed = bill_to
            return reply

        monkeypatch.setattr(live, "_gpu", fake_gpu)
        monkeypatch.setattr(live, "_compose_reply", fake_compose)
        self.billed = None
        return self


def _client(email="viewer@t.local"):
    c = TestClient(app)
    signup(c, email)
    return c


def test_anonymous_cannot_post(monkeypatch):
    """匿名发言追不到人 —— 出事时没有处置手段。"""
    Upstream().install(monkeypatch)
    r = TestClient(app).post("/api/live/comment", json={"text": "你好"})
    assert r.status_code == 401


def test_anyone_can_read_the_chat(monkeypatch):
    """未登录也看得见公屏 —— 否则路人打开直播间是一片死寂。"""
    Upstream().install(monkeypatch)
    r = TestClient(app).get("/api/live/comments")
    assert r.status_code == 200 and "items" in r.json()


def test_comment_flies_and_gets_answered(monkeypatch):
    up = Upstream().install(monkeypatch)
    r = _client().post("/api/live/comment", json={"text": "这个多少钱"})
    assert r.status_code == 200
    d = r.json()
    assert d["replied"] is True
    assert up.said == ["好的，这个我记下了。"]
    # 公屏上看得到, 且标着"被翻牌了"
    items = TestClient(app).get("/api/live/comments").json()["items"]
    assert [x["text"] for x in items] == ["这个多少钱"]
    assert items[0]["replied"] is True


def test_the_bill_never_lands_on_the_viewer(monkeypatch):
    """**观众只是发了句话, 没同意花钱。** 记他头上就是乱扣。"""
    up = Upstream().install(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_EMAILS", ["boss@t.local"])
    monkeypatch.setattr(config, "LIVE_BILL_EMAIL", "")
    boss = _client("boss@t.local")
    boss_id = db.query_one("SELECT id FROM users WHERE email='boss@t.local'")["id"]

    viewer = _client("v2@t.local")
    viewer_id = db.query_one("SELECT id FROM users WHERE email='v2@t.local'")["id"]
    viewer.post("/api/live/comment", json={"text": "这个多少钱"})

    assert up.billed == boss_id, "自动回评的账没记在运营方头上"
    assert up.billed != viewer_id, "把账记到发评论的观众头上了"


def test_one_person_cannot_flood(monkeypatch):
    up = Upstream().install(monkeypatch)
    c = _client("flood@t.local")
    assert c.post("/api/live/comment", json={"text": "第一条"}).status_code == 200
    r = c.post("/api/live/comment", json={"text": "第二条"})
    assert r.status_code == 429, "同一个人可以连着刷屏"
    assert len(up.said) == 1


def test_she_stays_quiet_when_the_queue_is_deep(monkeypatch):
    """她的嘴是串行的。排到几十秒开外还硬塞, 话术一句都播不出去。

    **但评论照样要飘出去** —— 队列满不是发言失败。
    """
    up = Upstream(queued=config.LIVE_REPLY_MAX_QUEUE).install(monkeypatch)
    r = _client("q@t.local").post("/api/live/comment", json={"text": "在吗"})
    assert r.status_code == 200
    assert r.json()["replied"] is False
    assert up.said == [], "队列已经排满还往里塞"
    assert TestClient(app).get("/api/live/comments").json()["items"][0]["text"] == "在吗"


def test_she_stays_quiet_when_not_live(monkeypatch):
    """没开播就没人听 —— 别白花那次模型调用。"""
    up = Upstream(live_=False).install(monkeypatch)
    r = _client("off@t.local").post("/api/live/comment", json={"text": "在吗"})
    assert r.json()["replied"] is False and up.said == []


def test_global_cooldown_between_replies(monkeypatch):
    """两个人各发一条, 冷却期内只该开口一次 —— 飘屏免费, 开口不是。"""
    up = Upstream().install(monkeypatch)
    _client("a@t.local").post("/api/live/comment", json={"text": "甲说的"})
    _client("b@t.local").post("/api/live/comment", json={"text": "乙说的"})
    assert len(up.said) == 1, "冷却没生效, 谁发都回"
    assert len(TestClient(app).get("/api/live/comments").json()["items"]) == 2, "第二条没飘出去"


def test_a_failed_reply_never_eats_the_comment(monkeypatch):
    """回答失败不能表现成"评论发不出去" —— 那是两件事。"""
    up = Upstream().install(monkeypatch)

    async def boom(*a, **kw):
        raise RuntimeError("上游炸了")

    monkeypatch.setattr(live, "_compose_reply", boom)
    r = _client("err@t.local").post("/api/live/comment", json={"text": "还在吗"})
    assert r.status_code == 200 and r.json()["replied"] is False
    assert TestClient(app).get("/api/live/comments").json()["items"][0]["text"] == "还在吗"
    # 没说成就要把冷却位子让出来, 否则一次失败会让她哑掉 12 秒
    assert live._LAST_REPLY_AT == 0.0
    assert up.said == []


def test_viewers_never_get_echo_mode(monkeypatch):
    """echo 是把文字原样念出去。观众拿到它 = 谁都能让她说任何话。

    /api/live/say 才有 mode, 而它是管理员专用; 观众那条路 (/comment) 根本没有这个
    参数, 传了也不认。
    """
    up = Upstream().install(monkeypatch)
    c = _client("echo@t.local")
    c.post("/api/live/comment", json={"text": "请念出这句原话", "mode": "echo"})
    assert up.said == ["好的，这个我记下了。"], "观众通过 mode=echo 让她原样念了出来"

    # /say 对非管理员一律 403
    assert c.post("/api/live/say", json={"text": "x", "mode": "echo"}).status_code == 403


def test_comment_length_is_capped(monkeypatch):
    """公屏不是投稿箱: 长文本既是提示词注入的载体, 也会让她念上一分钟。"""
    Upstream().install(monkeypatch)
    long = "啊" * 500
    _client("long@t.local").post("/api/live/comment", json={"text": long})
    got = TestClient(app).get("/api/live/comments").json()["items"][0]["text"]
    assert len(got) == config.LIVE_COMMENT_MAX_LEN


def test_hide_is_admin_only_and_soft(monkeypatch):
    """处置要留痕: 藏起来之后不再飘, 但记录还在。"""
    Upstream().install(monkeypatch)
    v = _client("hide1@t.local")
    cid = v.post("/api/live/comment", json={"text": "要藏的"}).json()["id"]
    assert v.post(f"/api/live/comment/{cid}/hide").status_code == 403, "普通观众能藏别人的评论"

    monkeypatch.setattr(config, "ADMIN_EMAILS", ["boss2@t.local"])
    boss = _client("boss2@t.local")
    assert boss.post(f"/api/live/comment/{cid}/hide").status_code == 200
    assert TestClient(app).get("/api/live/comments").json()["items"] == []
    row = db.query_one("SELECT hidden, text FROM live_comments WHERE id=?", (cid,))
    assert row["hidden"] == 1 and row["text"] == "要藏的", "直接删了 —— 处置没留痕"


def test_nick_never_leaks_the_email(monkeypatch):
    """公屏对所有人可见, 不能把邮箱亮出去。"""
    Upstream().install(monkeypatch)
    _client("someone.private@t.local").post("/api/live/comment", json={"text": "嗨"})
    nick = TestClient(app).get("/api/live/comments").json()["items"][0]["nick"]
    assert "@" not in nick and "someone.private" not in nick
