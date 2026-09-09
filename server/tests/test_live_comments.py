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
    _client("boss@t.local")  # 建出运营方这个用户; 返回的 client 这条用例不用
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


# ── 禁词闸 ───────────────────────────────────────────────────────────────
# 2026-09-09 上线第一条实测就翻车: 提示词第 4 条白纸黑字写着"不要编造优惠",
# 她照样说了"今天直播间有专属优惠"。人已经不在环里了 (自动回每一条), 所以
# 提示词管不住的东西必须在服务端拦。


@pytest.mark.parametrize(
    "bad",
    [
        "今天直播间有专属优惠，想了解的可以关注一下。",
        "这款现在有现货，下单就给您发货。",
        "全网最低价，放心拍。",
        "我保证这个有效果。",
        "买一送一，还包邮呢。",
    ],
)
def test_fabricated_claims_never_reach_the_stream(monkeypatch, bad):
    up = Upstream().install(monkeypatch, reply=bad)
    r = _client(f"c{abs(hash(bad)) % 9999}@t.local").post("/api/live/comment", json={"text": "问一句"})
    assert r.status_code == 200
    assert up.said == [live._SAFE_FALLBACK], f"编造的话播出去了: {up.said}"


def test_a_clean_reply_passes_through(monkeypatch):
    """闸不能宽到把正常回答也拦了 —— 那样她永远只会说那一句兜底。"""
    ok = "我是数字人主播，不是真人。您想了解哪一款，我给您说说它能做什么。"
    up = Upstream().install(monkeypatch, reply=ok)
    _client("clean@t.local").post("/api/live/comment", json={"text": "你是真人吗"})
    assert up.said == [ok]


def test_the_admin_path_is_not_gagged(monkeypatch):
    """管理员自己插播不过这道闸 —— 话是他写的、他负责, 而运营本来就要提活动。"""
    up = Upstream().install(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_EMAILS", ["boss3@t.local"])
    boss = _client("boss3@t.local")
    r = boss.post("/api/live/say", json={"text": "今天全场八折", "mode": "echo"})
    assert r.status_code == 200
    assert up.said == ["今天全场八折"]


# ── 字幕 ─────────────────────────────────────────────────────────────────


def test_captions_are_public_and_cached(monkeypatch):
    """字幕是给观众看的, 所以公开; 但每个观众各自打 GPU 是不行的。

    一百个人看同一场直播、看的是同一份内容 —— 不缓存就是每秒几十次打到那张卡上,
    而它同时还在生成画面。
    """
    calls = {"n": 0}
    state = {"live": True, "queued": 0,
             "recent": [{"t": 1.0, "kind": "script", "text": "第一句"},
                        {"t": 2.0, "kind": "interject", "text": "回你这条"}]}

    async def fake_gpu(method, path, room, **kw):
        calls["n"] += 1
        return dict(state)

    monkeypatch.setattr(live, "_gpu", fake_gpu)
    monkeypatch.setattr(live, "_CAP_CACHE", {"at": 0.0, "data": None})

    c = TestClient(app)
    r = c.get("/api/live/captions")          # 未登录也要能拿到
    assert r.status_code == 200
    d = r.json()
    assert [x["text"] for x in d["lines"]] == ["第一句", "回你这条"]
    assert d["lines"][1]["kind"] == "interject", "回评论那句要能被前端挑出来"

    for _ in range(5):
        c.get("/api/live/captions")
    assert calls["n"] == 1, f"缓存没生效, 打了上游 {calls['n']} 次"


def test_captions_do_not_leak_upstream_state(monkeypatch):
    """这条路没有鉴权 —— 别把队列深度、错误、话术全文顺手带出去。"""
    async def fake_gpu(method, path, room, **kw):
        return {"live": True, "queued": 7, "err": "内部错误细节",
                "lines": ["完整话术第一句", "完整话术第二句"],
                "person": "source-v3-head", "voice": "xiaoxiao",
                "recent": [{"t": 1.0, "kind": "script", "text": "只该露这个"}]}

    monkeypatch.setattr(live, "_gpu", fake_gpu)
    monkeypatch.setattr(live, "_CAP_CACHE", {"at": 0.0, "data": None})
    d = TestClient(app).get("/api/live/captions").json()
    assert set(d) == {"live", "lines"}, f"多回了字段: {set(d) - {'live', 'lines'}}"
    assert set(d["lines"][0]) == {"t", "kind", "text"}


def test_captions_survive_an_unreachable_gpu(monkeypatch):
    """上游够不着时字幕停住就行 —— 不能让整块变成红字报错。"""
    from fastapi import HTTPException

    async def boom(*a, **kw):
        raise HTTPException(502, "upstream_unreachable")

    monkeypatch.setattr(live, "_gpu", boom)
    monkeypatch.setattr(live, "_CAP_CACHE", {"at": 0.0, "data": None})
    r = TestClient(app).get("/api/live/captions")
    assert r.status_code == 200 and r.json() == {"live": False, "lines": []}
