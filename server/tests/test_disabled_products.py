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

    from app import live as _live
    from app.main import app
    from tests._signup import signup

    with TestClient(app) as c:
        signup(c, "live-csp@example.com")  # 未登录会 303 走掉, 那是张没有 CSP 的空响应
        # **每一个真的放视频的页面都要查**, 不能只查 /live。
        # 2026-09-09 直播拆成多间, 播放页变成 /live/{room}, 而 CSP 那边还是
        # `path in (...)` 的精确匹配 —— 新页面当场失去 media-src, 而 Safari 原生
        # 放 HLS 不走 MediaSource, 所以**在 Mac 上一切正常**, 只有 Chrome 黑屏。
        # 这条测试当时红了, 那是它唯一一次机会。
        pages = {"/live": c.get("/live")}
        for r in _live.rooms():
            pages[f"/live/{r}"] = c.get(f"/live/{r}")
        pages["/live/console"] = c.get("/live/console")
        home = c.get("/")

    for url, resp in pages.items():
        csp = resp.headers.get("content-security-policy", "")
        assert "media-src 'self' blob:" in csp, f"{url} 少了 media-src blob: —— Chrome 上是黑屏"
        assert "connect-src" not in csp, f"{url} 有跨源出口 —— HLS 是同源代转的"
        assert "microphone=()" in resp.headers.get("permissions-policy", "")
    assert "blob:" not in home.headers.get("content-security-policy", ""), "口子漏到首页了"


def test_the_console_is_admin_only_and_there_is_exactly_one_room(monkeypatch):
    """控制台是管理员专用, 而且全站只有一间 (老板 2026-09-08 定)。

    三道都要在: 没登录进不去; 普通用户 403 (页面也会被弹回观看页); 只有官方间的
    切片给发 —— 别的房间名一律 404, 不然这条把浏览器给的字符串拼进上游路径的路
    就成了摸 GPU 节点的探测器。
    """
    from fastapi.testclient import TestClient

    from app import config as cfg
    from app.main import app
    from tests._signup import signup

    monkeypatch.setattr(cfg, "LIVE_GPU_URL", "http://live.invalid/live")
    monkeypatch.setattr(cfg, "LIVE_ROOM", "official")
    monkeypatch.setattr(cfg, "AVATAR_TOKEN_SECRET", "t" * 32)

    with TestClient(app) as anon:
        for call in (
            lambda c: c.get("/api/live/room"),
            lambda c: c.put("/api/live/room", json={"lines": ["x"]}),
            lambda c: c.post("/api/live/room/start"),
            lambda c: c.post("/api/live/generate", json={"title": "x"}),
            lambda c: c.post("/api/live/say", json={"text": "x", "mode": "echo"}),
        ):
            assert call(anon).status_code in (401, 403)
        assert anon.get("/api/live/hls/official/index.m3u8").status_code != 403

    with TestClient(app) as c:
        signup(c, "live-plain@example.com")  # 普通用户
        assert c.get("/api/live/room").status_code == 403
        assert c.put("/api/live/room", json={"lines": ["x"]}).status_code == 403
        assert c.post("/api/live/room/start").status_code == 403
        assert c.post("/api/live/generate", json={"title": "x"}).status_code == 403
        # 让数字人当众说一句话, 显然只能管理员来
        assert c.post("/api/live/say", json={"text": "x", "mode": "echo"}).status_code == 403
        # 页面也拦一道: 非管理员被弹回观看页, 不给看一个自己用不了的壳
        r = c.get("/live/console", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/live"
        # 别的房间名: 404 (不是 403 —— 不告诉外面"这个名字存在但你没权限")
        assert c.get("/api/live/hls/d-someoneelse/index.m3u8").status_code == 404

    admin_mail = "live-admin@example.com"
    old = list(cfg.ADMIN_EMAILS)
    cfg.ADMIN_EMAILS.append(admin_mail)
    try:
        with TestClient(app) as c:
            signup(c, admin_mail)
            assert c.get("/live/console", follow_redirects=False).status_code == 200

            # 上游必定打不通 —— **显式让它抛**, 不靠"假域名解析不了": 这台开发机的
            # 系统代理会把任何域名都解析掉(连得上), CI 里 DNS 直接失败, 同一条断言
            # 在两边走的是完全不同的分支。第一版就是这么红的 CI, 而且顺带逼出一个
            # 真 bug: _gpu 当时没接 httpx 异常, GPU 节点一够不着用户就吃 500 带栈。
            import httpx as _httpx

            async def _boom(*a, **k):
                raise _httpx.ConnectError("upstream down")

            monkeypatch.setattr(_httpx.AsyncClient, "request", _boom)
            # 502 说明**管理员这一关放行了**, 才轮到网络出错 (403 会更早返回)。
            assert c.get("/api/live/room").status_code == 502
    finally:
        cfg.ADMIN_EMAILS[:] = old


def test_each_page_has_every_element_its_javascript_reaches_for():
    """每个页面的 JS 里 getElementById 的 id, 那个页面的模板里都得真有。

    这两边是**靠约定连着的**, 没有任何编译期检查。漂了的表现最难查: 页面照常渲染、
    浏览器控制台不报错 (多数调用在事件回调里才炸), 用户看到的只是"按钮点了没反应"。

    顺带钉住 hls.min.js 的引用 —— 少了它 Chrome 上是"画面永远转圈", 而 Safari 因为
    原生放 HLS 反而正常。只用 Mac 开发时这种事最容易漏。
    """
    import re
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app import config as cfg
    from app.main import app
    from tests._signup import signup

    static = Path(__file__).resolve().parents[1] / "app" / "static"

    def ids_in(js_name):
        js = (static / js_name).read_text(encoding="utf-8")
        got = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", js))
        got |= set(re.findall(r"getElementById\('([A-Za-z0-9_-]+)'\)", js))
        return got

    # 正则健全性只对**并集**判一次: 观看页的播放器统共就用四个 id, 按页卡阈值会
    # 把"这一页本来就简单"误判成"正则失效"。
    _all = (
        ids_in("live.js")
        | ids_in("live_console.js")
        | ids_in("live_rooms.js")
        | ids_in("live_chat.js")
        | ids_in("live_captions.js")
    )
    assert len(_all) >= 15, "正则大概过时了"

    admin_mail = "live-ids@example.com"
    old = list(cfg.ADMIN_EMAILS)
    cfg.ADMIN_EMAILS.append(admin_mail)
    try:
        with TestClient(app) as c:
            signup(c, admin_mail)
            # 2026-09-09 拆成多间: /live 变成列表页 (只有 live_rooms.js),
            # 播放器搬到 /live/{room}。这张表漂了正是这条测试要防的东西 ——
            # 上一次它就是这么红的。
            from app import live as _live

            first = _live.rooms()[0]
            # 单间时 /live 会 303 进播放页 (回滚成单间的方式就是只配一间),
            # 那时它没有列表, 自然也没有 live_rooms.js。
            pages = {
                f"/live/{first}": (["live.js", "live_chat.js", "live_captions.js"], set()),
                # live_chat.js 干两件事: 弹幕渲染 + 观众发言框。控制台只要前者 ——
                # 管理员那一栏是自己的"互动"面板 (能选复读/问答), 不是观众公屏。
                # 这三个 id 在控制台上**故意**没有, 脚本里也各自 if 兜住了。
                # 列在这里而不是放宽整条规则: 例外要写下来, 否则下次真漂了没人知道。
                "/live/console": (
                    ["live.js", "live_chat.js", "live_captions.js", "live_console.js"],
                    {"lvSayBox", "lvSayBtn", "lvSayHint"},
                ),
            }
            for url, (scripts, optional) in pages.items():
                html = c.get(url).text
                want = set()
                for js in scripts:
                    want |= ids_in(js)
                missing = [i for i in sorted(want - optional) if f'id="{i}"' not in html]
                assert not missing, f"{url} 的 JS 找这些 id, 模板里没有: {missing}"
                stale = [i for i in sorted(optional) if f'id="{i}"' in html]
                assert not stale, f"{url} 的例外名单过期了, 这些 id 其实有: {stale}"
                # hls.js 只有**真的放视频**的页面才需要。列表页没有播放器,
                # 硬要求它带上等于逼着每个页面都加载一个 400KB 的解码库。
                if "live.js" in scripts:
                    assert "/static/hls.min.js" in html, f"{url} 少了 hls.js"
                for js in scripts:
                    assert f"/static/{js}" in html, f"{url} 少了 {js}"
    finally:
        cfg.ADMIN_EMAILS[:] = old
