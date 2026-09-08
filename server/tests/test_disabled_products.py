"""整格下架 (WORK_DISABLED_PRODUCTS) 的守卫。

下架和加锁是两件事, 混了就出静默错误:
  加锁 (WORK_LOCKED_PRODUCTS)  卡还在, 买了通行证就能开
  下架 (WORK_DISABLED_PRODUCTS) 卡都不出, 谁都开不了

这里钉三条, 每条都是"漏了不会报错、只是行为不对":
  · enabled() 要把下架的排除掉 —— 漏了它照样能被路由到, 用户点进去是转圈
  · 目录要**不出卡**, 而不是出一张灰的 —— 灰卡照样占位置, 那不叫下架
  · 下架不改 registry() —— 已经跑着的工作台还要能被 inspect/release, 定义得留着
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-disabled-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import apps_catalog, config, products


def test_disabled_ids_parses_env(monkeypatch):
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", " coze , dify ,, ")
    assert products.disabled_ids() == {"coze", "dify"}
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "")
    assert products.disabled_ids() == set()


def test_enabled_excludes_disabled(monkeypatch):
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "")
    before = {p.id for p in products.enabled()}
    victim = next(iter(before), None)
    if victim is None:  # 这个环境一个产品都没配, 没什么可测的
        return
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", victim)
    after = {p.id for p in products.enabled()}
    assert victim not in after
    assert after == before - {victim}  # 只少这一个, 别的一个都没连带掉


def test_disabled_still_in_registry(monkeypatch):
    """下架只影响"能不能新开", 不能把定义抽走 —— 已经跑着的那台还要被回收和计量。"""
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "coze")
    assert "coze" in products.registry()


def test_catalog_drops_the_card_entirely(monkeypatch):
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "")
    all_ids = {e["id"] for e in apps_catalog.entries_with_status(set())}
    # 样本从 coze 换成 dify (2026-09-08): coze 的目录位已被数字人直播顶掉, 拿一个
    # 不在目录里的 id 当样本, 这条用例就永远是绿的而什么也没验到。
    assert "dify" in all_ids, "目录里本来该有 dify, 不然这条测试什么也没验到"

    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "dify")
    entries = apps_catalog.entries_with_status(set())
    ids = {e["id"] for e in entries}
    # 不是"live=False 的灰卡", 是根本没有这一项
    assert "dify" not in ids
    assert ids == all_ids - {"dify"}


def test_catalog_sorting_survives_a_disabled_card(monkeypatch):
    """排序用的 order 表也要跟着去掉那一项, 否则按时长排序时 KeyError。"""
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "dify")
    entries = apps_catalog.entries_with_status(set(), minutes={"comfyui": 10})
    assert "dify" not in {e["id"] for e in entries}


def test_the_live_page_may_load_blob_media_but_gets_no_extra_openings():
    """直播页要能放 blob: 媒体, 但**只**多这一条。

    · `media-src blob:` —— hls.js 走 MediaSource, 视频源是 blob: URL。少了它
      `default-src 'self'` 会挡掉, 而表现是"画面一帧不动": 切片照常下载、状态照常
      显示直播中, 只有浏览器控制台里一行 CSP 违规。数字人当年就栽在这, 查了很久。
    · **不给 connect-src 开跨源口子** —— m3u8/ts 走 /api/live/hls/* 同源代转,
      正是为了不开这个口子 (见 app/live.py 头注释)。哪天有人图省事改成直连 GPU
      域名, 这条断言会拦下来。
    · 直播页不需要麦克风 (那是通话页的事), 保持全关。
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from tests._signup import signup

    with TestClient(app) as c:
        signup(c, "live-csp@example.com")  # 未登录会 303 走掉, 那是张没有 CSP 的空响应
        lv = c.get("/live")
        home = c.get("/")
    csp = lv.headers.get("content-security-policy", "")
    assert "media-src 'self' blob:" in csp
    assert "connect-src" not in csp, "直播页不该有跨源出口 —— HLS 是同源代转的"
    assert "microphone=()" in lv.headers.get("permissions-policy", "")
    assert "blob:" not in home.headers.get("content-security-policy", "")


def test_a_live_room_belongs_to_exactly_one_person(monkeypatch):
    """直播间的隔离必须落在**归属校验**上, 不能指望"别人不知道房间名"。

    房间名就是 `d-<用户id>` —— 用户 id 在站内到处都是, 猜得到。所以:
    · 别人的房间: 403 (且在打上游之前就拒, 不能让人拿我们当探测器);
    · 官方间: 人人可看 (它就是拿来展示的);
    · 没登录: 拿不到控制台的任何一个接口。
    """
    from fastapi.testclient import TestClient

    from app import config as cfg
    from app.main import app
    from tests._signup import signup

    monkeypatch.setattr(cfg, "LIVE_GPU_URL", "http://live.invalid/live")
    monkeypatch.setattr(cfg, "LIVE_ROOM", "official")
    monkeypatch.setattr(cfg, "AVATAR_TOKEN_SECRET", "t" * 32)

    with TestClient(app) as anon:
        # 未登录: 控制台一个都进不去
        assert anon.get("/api/live/room").status_code in (401, 403)
        assert anon.put("/api/live/room", json={"lines": ["x"]}).status_code in (401, 403)
        assert anon.post("/api/live/room/start").status_code in (401, 403)
        # 官方间的切片是公开的
        assert anon.get("/api/live/hls/official/index.m3u8").status_code != 403

    with TestClient(app) as c:
        signup(c, "live-owner@example.com")
        me = c.get("/api/live/room")
        # 上游是假地址, 打不通 —— 502 说明**归属这一关放行了**, 才轮到网络出错。
        assert me.status_code == 502, me.status_code
        # 别人的房间: 在打上游之前就该被拒
        assert c.get("/api/live/hls/d-somebodyelse/index.m3u8").status_code == 403
        assert c.get("/api/live/hls/official/index.m3u8").status_code != 403
