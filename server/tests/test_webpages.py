"""Web console page tests. Environment is prepared BEFORE the app import."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types

_DATA_DIR = tempfile.mkdtemp(prefix="dhc-test-data-")
_LEGAL_DIR = tempfile.mkdtemp(prefix="dhc-test-legal-")  # empty: legal pages must show placeholder

os.environ["DHC_DEV"] = "1"
os.environ["AUTH_SECRET"] = "test"
os.environ["DHC_DATA_DIR"] = _DATA_DIR
os.environ["DHC_LEGAL_DIR"] = _LEGAL_DIR
os.environ.pop("DOWNLOAD_URL_MAC", None)
os.environ.pop("DOWNLOAD_URL_WIN", None)

# The payments API is developed in parallel; stub its router if not present yet
# so the page tests do not depend on it.
try:
    importlib.import_module("app.payments.api")
except Exception:  # pragma: no cover - only taken while payments is unfinished
    from fastapi import APIRouter

    _stub = types.ModuleType("app.payments.api")
    _stub.router = APIRouter(prefix="/api/pay")
    sys.modules["app.payments.api"] = _stub

import pytest
from fastapi.testclient import TestClient

from app.main import app

from ._signup import signup, signup_with_password


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# --- public pages ------------------------------------------------------------


def test_landing_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "AI Store" in body
    # 主张本身, 不是某个角落里的徽章 —— 2026-09-09 品牌从"云空间"改成 AI Store,
    # 而首页 h1 与 meta 曾经一个说"云"一个说"Store", 两边不一致了好几天没人发现。
    assert "全世界最好的" in body and "个 AI 产品" in body
    assert "/static/app.css" in body
    assert "/download" in body
    assert "/legal/terms" in body


def test_the_headline_count_comes_from_the_shelf(client):
    """首页最大那行字里的数字**必须**等于货架上真有几个。

    写死的话, 加一个产品或下架一个, 它当场变成假话 —— 而这是整个站上最显眼、
    最像承诺的一句 (2026-09-09 换成"16 个"时差点就写死了)。
    这里不去问代码要数字, 直接**数页面上的瓦片**再和标题比: 两个都出自同一次
    渲染, 对不上就是真的对不上。
    """
    import re as _re

    body = client.get("/").text
    tiles = len(_re.findall(r'class="hero-app[ "]', body))
    assert tiles > 0, "首屏一个产品瓦片都没有, 这条断言失去意义"
    m = _re.search(r"最好的\s*(\d+)\s*个 AI 产品", body)
    assert m, "标题里没有数字 —— 文案换了就把这条一起更新"
    assert int(m.group(1)) == tiles, f"标题说 {m.group(1)} 个, 首屏实际摆了 {tiles} 个"


def test_apps_page_shows_every_card_with_live_status(client, monkeypatch):
    """云空间: 每张卡都在, 上线与否由 products.enabled() 实时判定。

    目录 (apps_catalog) 是愿景, registry 才是事实 —— 卡片可点性必须跟着 registry
    走, 否则接入新产品时这页会静默漏掉它, 或者反过来给没上线的挂真链接。

    ⚠️ 这里**不再写死张数**。原先钉的是 16 ("4x4 网格就是 16 个"), 但那是审美意图
    不是布局约束 —— 网格是 auto-fill 的, 多一张只是换行。写死张数的结果是每加一个
    产品都要回来改一个与被测行为无关的数字, 而它挡不住任何真问题 (重复 id 那条才是)。
    """
    from app import apps_catalog, config, products

    ids = [a.id for a in apps_catalog.listed()]
    assert len(ids) >= 16, "货架上的产品少于 16 个, 是不是有人误删了目录条目"
    assert len(set(ids)) == len(ids), "目录里有重复 id"

    monkeypatch.setattr(config, "WORK_ENABLED", True)
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    body = client.get("/apps").text
    for a in apps_catalog.listed():
        assert a.name in body, f"{a.name} 没出现在页面上"
    # 上线的卡是真链接
    assert "/work?product_id=comfyui" in body
    enabled = {pr.id for pr in products.enabled()}
    assert "comfyui" in enabled, "前提: 测试配置里 comfyui 已启用"
    # 没上线的绝不能挂工作台链接 —— 点进去是 404/错误页
    for a in apps_catalog.listed():
        if a.id not in enabled:
            assert f"/work?product_id={a.id}" not in body, f"{a.id} 未上线却挂了链接"


def test_app_links_open_in_a_new_tab(client, monkeypatch):
    """点云空间产品要开新标签, 别把用户从目录页顶掉。

    这些工作界面是长驻的 (跑一个任务几十分钟很常见), 而用户常要在几个产品之间
    来回; 在原标签里跳走等于每次都要退回来重新找。rel=noopener 是安全默认 ——
    被打开的页面拿不到 window.opener。
    老板 2026-08-30 点名要的。

    **例外是住在主站上的产品** (数字人): 它就在本站, 顶层导航还在, 开新标签只是
    给用户平添一个要自己关的窗口。所以这条断言按渲染结果分两种卡查 —— 早先是
    逐行 grep 模板文本, 那种查法把"哪个链接"和"排版怎么折行"绑在了一起。
    """
    import re

    from app import config

    monkeypatch.setattr(config, "WORK_ENABLED", True)
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    monkeypatch.setattr(config, "AVATAR_TOKEN_SECRET", "s" * 32)

    for path in ("/apps", "/"):
        body = client.get(path).text
        # 指向工作台的链接**一个都不许漏**。早先这里只挑产品卡, 把主页旗舰区
        # 那个"进入"按钮排除在外 —— 于是同一个去处有了两种行为, 而它们在页面上
        # 看起来一模一样 (都是一个按钮)。现在按去处筛, 不按长相筛。
        links = re.findall(r"<a\b[^>]*>", body)
        work = [a for a in links if "/work?product_id=" in a]
        assert work, f"{path}: 找不到工作台链接"
        for a in work:
            assert 'target="_blank"' in a, f"{path}: 工作台链接没开新标签 — {a}"
            assert 'rel="noopener"' in a, f"{path}: 少了 noopener — {a}"
        site = [a for a in links if 'href="/avatar"' in a]
        assert site, f"{path}: 数字人卡没指向本站页面"
        for a in site:
            assert "target=" not in a, f"{path}: 本站页面不该开新标签 — {a}"


def test_apps_page_without_workspace_has_no_dead_links(client, monkeypatch):
    """自部署 (云工作台关) 且没配托管地址: 只陈列, 不放会 404 的按钮。"""
    from app import config

    monkeypatch.setattr(config, "WORK_ENABLED", False)
    monkeypatch.setattr(config, "HOSTED_SITE", "")
    body = client.get("/apps").text
    assert "/work?product_id=" not in body
    assert 'href="/work"' not in body


def test_landing_is_the_storefront(client, monkeypatch):
    """转型后主页即货架: 16 张产品卡上主页, 与 /apps 共用同一张卡 (include)。

    另外钉两条: 旗舰区的 composer 必须还在 (它是 dsh 的转化入口, 挪位置不能
    挪没); ComfyUI 旗舰卡直达工作台。
    """
    from app import apps_catalog, config

    monkeypatch.setattr(config, "WORK_ENABLED", True)
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    body = client.get("/").text
    assert 'href="/apps"' in body, "主页没有云空间入口"
    for a in apps_catalog.listed():
        assert a.name in body, f"{a.name} 没上主页货架"
    assert "hero-composer" in body, "composer 挪没了 —— 那是 dsh 的转化入口"
    assert "/work?product_id=comfyui" in body, "ComfyUI 旗舰卡没直达工作台"


def test_landing_no_icp_when_unset(client):
    r = client.get("/")
    assert "beian.miit.gov.cn" not in r.text  # ICP_NUMBER empty by default


def test_login_page_renders(client):
    """Assert the sign-in AFFORDANCES, not their wording — the copy moves with
    design work and pinning it turned every visual change into a red suite."""
    r = client.get("/login")
    assert r.status_code == 200
    assert 'data-tab="pw"' in r.text and 'data-tab="code"' in r.text
    assert 'id="form-code"' in r.text and 'id="form-pw"' in r.text
    assert "/api/auth/google/start" in r.text and "/api/auth/github/start" in r.text


def test_login_page_offers_a_way_out(client):
    """Sign-in used to be a dead end: no header, no link home. Anyone who lands
    here by accident must be able to leave."""
    r = client.get("/login")
    assert 'href="/"' in r.text


def test_pricing_page_renders(client):
    r = client.get("/pricing")
    assert r.status_code == 200
    body = r.text
    assert "定价" in body
    assert "Pro" in body
    assert "积分包" in body
    assert "plan:pro:monthly" in body
    assert "pack:pack1000" in body


def test_pricing_headline_is_the_price_checkout_charges(client):
    """The number in the big type and the number on the order have to be the same
    one. They were not: the card advertised the first-month price while checkout
    charged the standard one, so a Max buyer saw $60 and was billed $100."""
    import re

    from app import plans

    signup(client, "headline@example.com")

    body = client.get("/pricing").text
    table = plans.pricing()["tiers"]
    for tier in ("plus", "pro", "max"):
        card = body.split(f'data-tier="{tier}"', 1)[1].split("</div>\n        </div>", 1)[0]
        shown = re.search(r'class="price-now"[^>]*>[^0-9]*([0-9,]+)<', card)
        assert shown, tier
        headline = int(shown.group(1).replace(",", ""))

        r = client.post("/api/pay/checkout", json={"item": f"plan:{tier}:monthly"})
        assert r.status_code == 200, r.text
        order = r.json()
        # No provider is configured in tests, so checkout records an intent —
        # priced by the same price_for the real providers are handed.
        charged = client.get(f"/api/pay/orders/{order['order_id']}").json()["order"]["amount_cents"]
        assert charged == table[tier]["monthly_intro_cents"], tier
        assert headline == charged // 100, f"{tier}: page shows {headline}, order charges {charged // 100}"


def test_activate_page_renders(client):
    r = client.get("/activate?code=AB12-CD34")
    assert r.status_code == 200
    assert "授权此设备" in r.text
    assert "拒绝" in r.text
    assert "AB12-CD34" in r.text


def test_download_page_placeholder(client):
    """With no DOWNLOAD_URL_* set, the buttons must be inert rather than linking
    somewhere broken."""
    r = client.get("/download")
    assert r.status_code == 200
    assert "disabled" in r.text


def test_download_page_links_through_the_counter(client):
    """Installers are linked as /dl/<key>, never as the storage URL: that is what
    makes the advertised download count a real number, and what lets the bytes
    move to another host without touching a template."""
    os.environ["DOWNLOAD_URL_MAC"] = "https://example.com/dsh.dmg"
    try:
        r = client.get("/download")
        assert "/dl/mac-arm64" in r.text
        assert "https://example.com/dsh.dmg" not in r.text
    finally:
        os.environ.pop("DOWNLOAD_URL_MAC", None)


def test_download_redirect_counts_then_forwards(client):
    os.environ["DOWNLOAD_URL_MAC"] = "https://example.com/dsh.dmg"
    try:
        from app import db

        before = db.query_one("SELECT v FROM kv WHERE k='downloads_total'")
        before = int((before["v"] if before else 0) or 0)
        r = client.get("/dl/mac-arm64", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "https://example.com/dsh.dmg"
        after = db.query_one("SELECT v FROM kv WHERE k='downloads_total'")
        assert int(after["v"]) == before + 1
    finally:
        os.environ.pop("DOWNLOAD_URL_MAC", None)


def test_download_redirect_404s_for_a_platform_we_do_not_ship(client):
    os.environ.pop("DOWNLOAD_URL_WIN_ARM", None)
    assert client.get("/dl/win-arm64", follow_redirects=False).status_code == 404


# --- legal pages -------------------------------------------------------------


def test_legal_pages_placeholder_when_missing(client):
    for doc in ("terms", "privacy", "refund", "aup"):
        r = client.get(f"/legal/{doc}")
        assert r.status_code == 200, doc
        assert "文档整理中" in r.text, doc


def test_legal_page_renders_markdown(client, tmp_path, monkeypatch):
    # A dedicated tmp dir per run: this test writes and deletes a fixture file,
    # and must never be able to touch the repo's real legal/ documents.
    monkeypatch.setenv("DHC_LEGAL_DIR", str(tmp_path))
    path = tmp_path / "terms.zh.md"
    path.write_text(
        "# 服务条款\n\n欢迎使用 **AI Store**。\n\n- 第一条\n- 第二条\n\n"
        "| 项目 | 说明 |\n|---|---|\n| 积分 | 1 积分 = ¥0.01 |\n\n"
        "详见[隐私政策](/legal/privacy)。\n",
        encoding="utf-8",
    )
    try:
        r = client.get("/legal/terms")
        assert r.status_code == 200
        assert "文档整理中" not in r.text
        assert "<h1>服务条款</h1>" in r.text
        assert "<strong>AI Store</strong>" in r.text
        assert "<li>第一条</li>" in r.text
        assert "<th>项目</th>" in r.text
        assert '<a href="/legal/privacy"' in r.text
    finally:
        path.unlink()


def test_legal_redirects(client):
    r = client.get("/privacy", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    assert r.headers["location"] == "/legal/privacy"
    r = client.get("/terms", follow_redirects=False)
    assert r.headers["location"] == "/legal/terms"


def test_markdown_escapes_html():
    from app.webpages import markdown_to_html

    out = markdown_to_html("<script>alert(1)</script>\n\n**bold** ok")
    assert "<script>" not in out
    assert "<strong>bold</strong>" in out


# --- auth-gated pages --------------------------------------------------------


def test_console_redirects_anonymous(client):
    r = client.get("/console", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].startswith("/login")


def test_orders_redirects_anonymous(client):
    r = client.get("/orders", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].startswith("/login")


def test_register_login_console_flow(client):
    email = "webuser@example.com"
    password = "secret-pass-123"

    signup_with_password(client, email, password)
    assert client.cookies.get("dhc_session")

    # fresh client: prove password login works, then browse the console
    with TestClient(app) as c2:
        r = c2.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text

        r = c2.get("/console")
        assert r.status_code == 200
        body = r.text
        assert "控制台" in body
        assert email in body
        assert "积分余额" in body
        assert "免费版" in body  # default plan
        assert "危险区" in body

        r = c2.get("/orders")
        assert r.status_code == 200
        assert "我的订单" in r.text


def test_static_assets_served(client):
    for path in ("/static/app.css", "/static/app.js", "/static/qr.js"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_release_downloads_are_capped_per_ip(client):
    """Installers are 100-280MB and share this machine with the model gateway.
    Two concurrent transfers per address is the budget; the third is told to
    come back rather than being served a trickle that pins a worker."""
    from app.release_throttle import ReleaseThrottle

    mw = ReleaseThrottle(None)
    import time

    now = time.time()
    mw._active["1.2.3.4"] = [now, now]
    assert mw._prune(now) == 2
    # a slot older than the stale window is a leaked one, not a live transfer
    mw._active["1.2.3.4"] = [now - 99999]
    assert mw._prune(now) == 0


def test_release_throttle_holds_the_slot_until_the_body_ends():
    """The whole point of the raw-ASGI form. BaseHTTPMiddleware released the
    slot when the response STARTED, so a 282MB transfer occupied the limiter
    for microseconds and nothing was ever rejected."""
    import asyncio

    from app.release_throttle import ReleaseThrottle

    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        started.set()
        await finish.wait()  # body still streaming
        await send({"type": "http.response.body", "body": b"x", "more_body": False})

    mw = ReleaseThrottle(slow_app)
    scope = {"type": "http", "path": "/releases/big.dmg", "headers": [], "client": ("9.9.9.9", 1)}

    async def run():
        sent = []
        task = asyncio.create_task(mw(scope, None, lambda m: sent.append(m) or asyncio.sleep(0)))
        await started.wait()
        # mid-transfer the slot must still be held
        assert mw._prune(__import__("time").time()) == 1
        finish.set()
        await task
        assert mw._prune(__import__("time").time()) == 0

    asyncio.run(run())


# --- currency picker ---------------------------------------------------------


def test_currency_picker_is_only_on_the_pricing_page(client):
    """It belongs next to the billing-period toggle, not in the nav: currency
    changes nothing anywhere else, and a control on every page reads as
    something the reader has to deal with on every page."""
    assert "cur-picker" in client.get("/pricing").text
    for path in ("/", "/product", "/solutions", "/download"):
        assert "cur-picker" not in client.get(path).text, path
        assert "lang-switch" in client.get(path).text, path  # language still is global


def test_currency_defaults_to_the_visitor_country(client):
    """Cloudflare puts CF-IPCountry in front of every request; the price a
    visitor sees should follow it without them doing anything."""
    body = client.get("/pricing", headers={"CF-IPCountry": "CN"}).text
    assert "¥ CNY" in body
    assert client.get("/pricing", headers={"CF-IPCountry": "JP"}).text.count("¥ JPY")
    assert "£ GBP" in client.get("/pricing", headers={"CF-IPCountry": "GB"}).text
    assert "€ EUR" in client.get("/pricing", headers={"CF-IPCountry": "DE"}).text
    # nowhere on the map -> USD rather than a currency they must convert
    assert "$ USD" in client.get("/pricing", headers={"CF-IPCountry": "BR"}).text


def test_currency_picker_offers_every_currency_and_a_way_back(client):
    """An explicit ?cur= sticks in a cookie for a year. Without a visible picker
    a single shared link pinned a visitor to a currency their country would
    never have chosen — which is exactly what happened."""
    from app import currency

    r = client.get("/pricing?cur=USD", headers={"CF-IPCountry": "CN"})
    assert r.cookies.get(currency.COOKIE) == "USD"
    body = r.text
    for code in currency.SUPPORTED:
        assert f">{code}<" in body, code
    # the country's own currency is labelled, so the way back is findable
    assert "按所在地" in body or "your region" in body


def test_picker_drops_the_country_qualifier_but_prices_keep_it(client):
    """HK$ next to the letters HKD repeats itself, and it was the only row wide
    enough to collide with its own label. A price is the opposite case: "$780"
    beside a Hong Kong price reads as US dollars."""
    from app import currency

    assert currency.glyph("HKD") == "$" and currency.symbol("HKD") == "HK$"
    body = client.get("/pricing?cur=HKD").text
    assert "HK$780" in body or "HK$</span>780" in body  # prices stay qualified
    assert "<b>HK$</b>" not in body  # the picker row does not


def test_switchers_do_not_reset_each_other(client):
    """Bare `?lang=en` hrefs replace the whole query string; switching language
    on /pricing?cur=CNY used to silently drop the currency."""
    body = client.get("/pricing?lang=en&cur=CNY").text
    assert "cur=CNY" in body and "lang=zh" in body  # language link keeps cur
    assert "lang=en" in body and "cur=EUR" in body  # currency links keep lang


def test_login_page_tells_selfhosters_where_the_dev_code_goes(client, monkeypatch):
    """开发模式 + 没配 SMTP 时验证码只打到服务端日志, 登录页必须说出来。

    2026-08-25 验收实测: 自部署用户点"获取验证码"后页面毫无反馈, 邮件永远不来
    (它在 docker logs 里), 首次登录直接卡死 —— 这是那次的回归钉。
    """
    from app import config

    body = client.get("/login").text
    assert "验证码打印在服务端日志里" in body

    # 配了 SMTP 就是真发信, 提示必须消失, 免得线上吓到用户。
    monkeypatch.setattr(config, "MAIL_SMTP_HOST", "smtp.example.com")
    assert "验证码打印在服务端日志里" not in client.get("/login").text


def test_selfhost_without_workspace_has_no_dead_ends(client, monkeypatch):
    """工作台关着时, 页面不能再把人指向 /work —— 那会 302 回 /download, 绕成死循环。

    2026-08-25 老板本地部署实测: 点"云端体验"落到 /download, 页面写着"正在重新
    构建，这段时间可以直接用浏览器版云工作台"并给出 /work 链接, 而 /work 又跳回
    /download; iPhone 卡片的"打开云工作台"同样。整个产品看着像个前端空壳。
    """
    from app import config

    monkeypatch.setattr(config, "WORK_ENABLED", False)
    for path in ("/", "/download"):
        body = client.get(path).text
        assert 'href="/work"' not in body, f"{path} 在工作台关闭时仍指向 /work"
        # 但"云端体验"这个入口不能消失 —— 改指官方站点即可
        assert "aistore.best/work" in body, f"{path} 少了云端体验入口"
    # 而且不能再谎称"正在重新构建" —— 自部署只是没配下载地址
    download = client.get("/download").text
    assert "本部署未配置桌面安装包" in download

    # 开着的时候一切照旧
    monkeypatch.setattr(config, "WORK_ENABLED", True)
    assert 'href="/work"' in client.get("/download").text


def test_selfhost_offers_the_hosted_service_as_a_labelled_alternative(client, monkeypatch):
    """本部署给不了的能力, 挂官方托管版入口 —— 这是有意的引流。

    两条性质必须成立, 否则引流会反噬:
      · 链接明写"官方托管版", 不能让人以为点的是自己这台服务;
      · 托管版自己的站点不能给自己挂一个指向自己的按钮。
    """
    from app import config

    monkeypatch.setattr(config, "WORK_ENABLED", False)
    body = client.get("/download").text
    # 三张卡的按钮都得真的指向官方站点, 而不是一个禁用的"重新构建中"
    assert "https://aistore.best/download" in body, "macOS/Windows 按钮该直连官网"
    assert "https://aistore.best/work" in body, "iPhone 卡该给云端体验入口"
    assert "aistore.best" in body and "官方站点" in body, "必须标明去的是官方站点"

    # 托管版自己: hosted_site 与 PUBLIC_BASE 同源时不挂
    monkeypatch.setattr(config, "PUBLIC_BASE", "https://aistore.best")
    assert "aistore.best/work" not in client.get("/download").text

    # 自部署方想彻底关掉引流: 置空即可
    monkeypatch.setattr(config, "PUBLIC_BASE", "http://localhost:8787")
    monkeypatch.setattr(config, "HOSTED_SITE", "")
    assert "aistore.best" not in client.get("/download").text


def test_hero_composer_never_leads_into_the_dead_end(client, monkeypatch):
    """首页那个输入框是最显眼的入口, 不能把人送进 /work → /download 的死胡同。

    2026-08-25 老板看着自部署首页问"点云端体验去哪" —— 一查, 导航按钮和云工作台
    卡片确实已经按开关隐藏了, 但输入框 (回车即执行) 还硬指着本站 /work。
    """
    from app import config

    monkeypatch.setattr(config, "WORK_ENABLED", False)
    body = client.get("/").text
    assert 'data-target="https://aistore.best"' in body, "该把任务送去托管版"
    assert "官方托管版" in body, "必须说清任务会在托管版执行, 不能让人以为跑在本机"

    # 没有托管版可去时, 输入框本身就不该出现 —— 没有任何地方能执行任务
    monkeypatch.setattr(config, "HOSTED_SITE", "")
    assert "hero-composer" not in client.get("/").text

    # 本地工作台开着: 一切照旧, 走本站
    monkeypatch.setattr(config, "WORK_ENABLED", True)
    body = client.get("/").text
    assert 'data-target=""' in body and "hero-composer" in body


def test_avatar_page_needs_an_account(client):
    """未登录进 /avatar -> 先去登录, 别让人看着界面一路 401。

    这页上**每一个**动作 (形象清单、背景图、上传、通话) 都挂着 resolve_user,
    所以先渲染再失败没有任何好处 —— 用户看到的是"点什么都没反应", 而那与真坏了
    分不出来。
    """
    r = client.get("/avatar", follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert r.headers["location"] == "/login?next=/avatar"


def test_avatar_page_renders_for_a_signed_in_user(client, monkeypatch):
    """登录后能开: 舞台、形象/音色选择、开始通话按钮都在。"""
    from app import config

    monkeypatch.setattr(config, "AVATAR_TOKEN_SECRET", "s" * 32)
    signup(client, "avatar-page@example.com")
    # 2026-09-17 伴聊拆成 /avatar(挑人) + /avatar/{形象}(通话) —— 钩子在后者上
    body = client.get("/avatar/serena").text
    # 只有**一个**选择器 (成套预设), 没有单独的音色选择 —— 分开选会出现"男样子
    # 配女嗓音"。少了这条断言, 谁把音色选择加回来都没人拦。
    assert "avVoice" not in body, "音色不该能单独选 — 它跟着人走"
    for hook in ("avPerson", "avCall", "avBg", "avTimer", "/static/avatar.js"):
        assert hook in body, f"通话页少了 {hook}"


def test_store_page_is_fully_translated(client):
    """切成英文之后, 首页正文里**不能再有中文**。

    创始人 2026-09-17 截图报的: 英文页上标题、副标题、搜索框、四个卖点、精选卡的角标
    与按钮、SOTA 那一段、状态药丸、页尾全是中文 —— 因为那些文案压根没进 i18n, 是写死
    在 home_v2.html 里的。这条测试就是钉住"别再往模板里写死文案"。

    ⚠️ 只查 <section> 到 </footer> 之间: 页头的语言切换按钮**本来就**写着"中文"
       (那是给中文读者看的入口), 把它算进来这条永远红。
    ⚠️ 产品名是例外中的例外: 第三方名字(ComfyUI/Codex)不翻, 而我们自己那两格是
       描述性名字, 走 AppEntry.name_key —— 所以这里顺带钉住它们译出来了。
    """
    import re

    body = client.get("/store", params={"lang": "en"}).text
    i = body.find('<section class="hero-dark')
    j = body.find("</footer>", i)
    assert i > 0, "首页结构变了, 这条测试该跟着改"
    main = body[i : j if j > 0 else None]
    cjk = re.findall(r"[\u4e00-\u9fff]+", main)
    assert not cjk, f"英文页正文里还有中文: {cjk[:8]}"
    for s in ("Digital Human Live", "Digital Human Companion", "SOTA picks"):
        assert s in main, f"英文页少了 {s!r}"

    # 中文页照旧
    zh = client.get("/store", params={"lang": "zh"}).text
    assert "严选商店" in zh and "数字人直播" in zh


def test_store_card_name_is_not_painted_with_the_brand_color(client):
    """精选卡上的应用名**不能涂品牌色**。

    创始人 2026-09-17: "右侧紫色区域中的文字颜色与紫色不搭配"。那张卡的底是固定的
    紫/蓝渐变, 而一半应用的品牌色就在同一个色系里 —— ComfyUI 的 #7A5AF8 涂在 #8b46e0
    上对比度只有 1.08:1, 等于看不见。
    原来的写法是黑名单四个近黑色换成浅蓝, 只挡住"太黑", 挡不住"跟底同色系"。
    ⚠️ 也别改成"把品牌色提亮": 连纯白在这张卡最亮处(扫光经过时约 #9a5ae8)也只有
       4.20:1, 带彩的字更低。品牌辨识交给右边那个自带底色的图标块。
    """
    body = client.get("/store", params={"lang": "zh"}).text
    i = body.find('class="v2-slide-name"')
    assert i > 0, "精选卡结构变了"
    assert "style=" not in body[i : i + 60], "应用名又被涂上了内联颜色 — 那正是看不见的原因"

    # 卡上每一处文字都要**显式声明颜色**, 一处都不能继承: 浅色主题下外层 .hero-dark
    # 的字色是 var(--fg)(近黑), 忘了声明的那一处就在紫底上变成黑字。
    # 2026-09-17 线上实测过: 标题与角标当时都是 rgb(13,21,38)。
    css = body[body.find("<style") : body.find("</style>")]
    for sel in (".v2-slide-l h2{", ".v2-badge{", ".v2-slide-name{"):
        j = css.find(sel)
        assert j > 0, f"样式里找不到 {sel}"
        rule = css[j : css.find("}", j)]
        assert "color:" in rule, f"{sel} 没有显式给颜色 — 浅色主题下会继承成黑字"


def test_avatar_pick_page_lists_every_persona(client, monkeypatch):
    """伴聊的第一屏是**挑人**, 不是直接进一通电话。

    创始人 2026-09-17: 「伴聊那个现在是 1 个直播间, 能不能复制数字人直播中的形象,
    类似布局排版开出对应的伴聊直播间」。形象本来就有十七套, 之前全藏在通话页侧栏的
    一个 select 里 —— 没进来过的人根本不知道有谁可聊。

    ⚠️ 这条同时钉住**两处清单不许漂**: 服务端的 AVATAR_PERSONS 与 static/avatar.js 的
       PRESETS 必须是同一批 id。漂了的表现是"卡片点进去是另一个人"或"列表里少一个人",
       而两边都不会报错。
    """
    import re
    from pathlib import Path

    from app import config
    from app.avatar import AVATAR_PERSONS

    monkeypatch.setattr(config, "AVATAR_TOKEN_SECRET", "s" * 32)
    signup(client, "avatar-pick@example.com")
    body = client.get("/avatar").text

    assert body.count('class="lv-roomcard"') == len(AVATAR_PERSONS), "卡片数与清单对不上"
    for pid, _key in AVATAR_PERSONS:
        assert f'href="/avatar/{pid}"' in body, f"列表里少了 {pid}"
        assert f"/api/avatar/cover/{pid}" in body, f"{pid} 没有封面"
    # 名字必须是翻译过的, 不能回落成 i18n 键本身 (source-v3-head 的键是历史遗留的
    # "default", 少那条特判第一张卡就会显示成 js.avatar.p.default)。
    # ⚠️ 只能查卡片标题那一处: 整份 i18n 字典本来就嵌在页面里给前端 t() 用, 所以
    #    "整页不含 js.avatar.p." 这种写法必红 —— 我第一版就是这么写的。
    assert 'lv-roomname">js.avatar.p.' not in body, "有名字没翻出来 — 卡片上会显示键名"

    js = (Path(__file__).resolve().parents[1] / "app" / "static" / "avatar.js").read_text("utf-8")
    block = js[js.index("const PRESETS") : js.index("const PRESETS") + 2000]
    in_js = set(re.findall(r'"([a-z0-9-]+)":\s*\{\s*name:', block))
    ours = {p for p, _ in AVATAR_PERSONS}
    assert ours == in_js, (
        f"服务端清单与 avatar.js 的 PRESETS 漂了 — "
        f"只在服务端: {sorted(ours - in_js)}; 只在 JS: {sorted(in_js - ours)}"
    )


def test_avatar_idle_layer_breathes(client, monkeypatch):
    """通话页空闲时她要**还在呼吸**, 不是一张定格的合成图。

    创始人 2026-09-17:「伴聊中, 空镜状态, 没有呼吸的感觉啊」。直播那边当天已经把补空镜
    换成模型喂静音渲的待机循环, 通话页跟上 —— 同一段片子, 因为它就是这个形象的人像
    裁剪区, 与视频层同一块位置。

    钉四件:
    · 待机层在页面上, 且**带 .av-video 类** —— 定位与羽化遮罩必须与视频层共用一套,
      各写一份迟早错位 (她一开口画面会挪一下)。
    · muted/loop/playsinline/autoplay 四样齐全: 少一样浏览器就不给自动播, 空闲态又变回定格。
    · 前端从 /api/avatar/idle.mp4 取片, 且**取不到要把这一层藏起来** —— 底下那张静止
      合成图还在, 退回改之前的样子; 绝不能因此变成一块黑。
    · 她开口时待机层要让位 (两层画的是同一张脸, 叠着就是重影)。
    """
    from pathlib import Path

    from app import config

    monkeypatch.setattr(config, "AVATAR_TOKEN_SECRET", "s" * 32)
    signup(client, "avatar-idle@example.com")
    body = client.get("/avatar/serena").text
    i = body.find('id="avIdle"')
    assert i > 0, "通话页没有待机层 — 空闲时又变成一张定格图了"
    tag = body[body.rfind("<video", 0, i) : body.find(">", i) + 1]
    assert "av-video" in tag, "待机层没跟视频层共用 .av-video — 定位/羽化会各走各的"
    for attr in ("muted", "loop", "playsinline", "autoplay"):
        assert attr in tag, f"待机层少了 {attr}, 浏览器不会自动播 — 空闲态还是定格"

    js = (Path(__file__).resolve().parents[1] / "app" / "static" / "avatar.js").read_text("utf-8")
    assert "/api/avatar/idle.mp4" in js, "前端没去取待机片"
    assert "onerror" in js and "avIdle" in js, "待机片取不到时没有藏起来的兜底"
    k = js.find("function showVideo")
    assert k > 0 and "avIdle" in js[k : k + 900], "她开口时待机层没让位 — 两层同一张脸会重影"


def test_avatar_unknown_person_falls_back_to_the_list(client, monkeypatch):
    """乱填的形象要回列表页, 而且**永远不能**拿去问上游。

    形象名会被拼进上游 URL, 而库里还有别的租户上传的**人脸照片**(前缀 t-<租户>--)。
    放行任意字符串就是隐私事故, 所以只认清单里的共享形象。
    """
    from app import config

    monkeypatch.setattr(config, "AVATAR_TOKEN_SECRET", "s" * 32)
    signup(client, "avatar-404@example.com")
    r = client.get("/avatar/nope", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/avatar"
    assert client.get("/api/avatar/cover/t-someone--face").status_code == 404
    assert client.get("/api/avatar/cover/../etc/passwd").status_code in (404, 400)


def test_tab_icon_is_wired_up(client):
    """标签页图标: link 标签要有, /favicon.ico 也要真能拿到东西。

    图标文件一直都在 (手机壳那套 PWA 图标), 只是从来没接到网页上 —— 结果全站
    每一页都白吃一个 /favicon.ico 404, 标签页上是个空白方块。这类毛病不报错、
    不变红, 只在网络面板里留一行, 所以拿测试钉住。
    """
    body = client.get("/").text
    assert 'rel="icon"' in body
    assert 'rel="apple-touch-icon"' in body

    ico = client.get("/favicon.ico")
    assert ico.status_code == 200
    assert ico.headers["content-type"].startswith("image/")
    assert len(ico.content) > 500


# --- admin console ----------------------------------------------------------


def test_admin_page_has_per_product_usage(client):
    """老板要看"每个用户用哪个产品, 积分和时长消耗在哪": 页面必须带全站消耗卡、
    周期选择器和用户行上的「用量」展开; 两种语言都得渲染出来 (JS 文案走 tojson,
    缺一个键整页脚本就挂, 而模板测试不会执行 JS —— 所以这里直接查键)。"""
    import json

    from app import db, i18n

    signup(client, "boss@t.local")
    db.query("UPDATE users SET role='admin' WHERE email=?", ("boss@t.local",))
    zh = client.get("/console/admin?lang=zh")
    assert zh.status_code == 200
    body = zh.text
    assert 'id="usage-all"' in body and 'id="period"' in body
    assert 'id="usage-users"' in body and "/api/admin/usage/users" in body and "按用户" in body
    assert "/api/admin/usage" in body
    assert "各产品消耗" in body and "机时(分钟)" in body
    # JS 里的文案经 tojson 输出, 非 ASCII 会被转义成 \uXXXX —— 按同样的写法找
    assert json.dumps(i18n.t("zh", "admin.usage.desktop")) in body
    for key in ("usage_btn", "usage_empty", "usage_desktop", "usage_total", "usage_th", "role_grant"):
        assert f"{key}:" in body, f"JS 文案缺 {key}"
    en = client.get("/console/admin?lang=en").text
    assert "Consumption by product" in en and '"Desktop / Web"' in en
    # 非管理员看不到这页
    signup(client, "pleb@t.local")
    assert client.get("/console/admin", follow_redirects=False).status_code == 303


def test_locked_apps_point_at_the_unlock_flow(client, monkeypatch):
    """上锁的格子: 卡片指向解锁, 不指向工作台 —— 让人点进去才发现进不去, 是把
    "要买"藏起来, 不是把它说清楚 (老板 2026-09-06)。"""
    from app import config, products

    monkeypatch.setattr(config, "WORK_ENABLED", True)
    # 卡片要"已上线"才有链接 —— 目录是愿景, registry 才是事实 (见 apps_catalog)
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    monkeypatch.setattr(config, "WORK_LOCKED_PRODUCTS", "dify")
    live = {p.id for p in products.enabled()}
    assert {"dify", "comfyui"} <= live, live
    assert products.is_locked("dify") and not products.is_locked("comfyui")
    body = client.get("/apps?lang=zh").text
    assert "reason=locked&amp;product_id=dify" in body or "reason=locked&product_id=dify" in body
    assert "需开通" in body and "开通试用" in body
    # 没上锁的格子照旧直接进工作台
    assert "/work?product_id=comfyui" in body


def test_unlock_banner_shows_the_price_with_decimals(client, monkeypatch):
    """9.9 写成 9 就不是那个东西了 —— 套餐卡片是整数单位, 这个横幅按两位小数渲染。"""
    from app import config

    monkeypatch.setattr(config, "WORK_LOCKED_PRODUCTS", "dify")
    body = client.get("/pricing?reason=locked&product_id=dify&cur=CNY&lang=zh").text
    assert 'id="unlock"' in body and 'data-item="pass:dify"' in body
    assert "9.90" in body, "冲动价要显示到分"
    # 那行小字老板 2026-09-06 让去掉了 —— 说什么都容易被读成"还得再买两样"
    assert "另外两份额度" not in body and "不附赠机时和积分" not in body
    assert "Dify" in body
    # 没上锁的产品不给横幅 —— 否则等于卖一个不用买的东西
    monkeypatch.setattr(config, "WORK_LOCKED_PRODUCTS", "")
    assert 'id="unlock"' not in client.get("/pricing?reason=locked&product_id=dify&lang=zh").text


def test_home_tiles_respect_the_lock_too(client, monkeypatch):
    """首页的瓦片是 _app_card.html 之外的**第二份拷贝**。漏了它的表现是: /apps 上锁了,
    首页照旧直进工作台, 点进去才被弹回来 (2026-09-06 老板实测到的正是这个)。"""
    from app import config

    monkeypatch.setattr(config, "WORK_ENABLED", True)
    monkeypatch.setattr(config, "DIFY_DOMAIN", "dify.test.local")
    monkeypatch.setattr(config, "COMFY_IMAGE", "comfy:test")
    monkeypatch.setattr(config, "COMFY_DOMAIN", "comfy.test.local")
    monkeypatch.setattr(config, "WORK_LOCKED_PRODUCTS", "dify")
    body = client.get("/?lang=zh").text
    assert "reason=locked&product_id=dify" in body, "首页也得指向解锁"
    assert "/work?product_id=dify" not in body, "首页不能还留着直进工作台的链接"
    assert "/work?product_id=comfyui" in body, "没上锁的照旧直达"


def test_workspace_pass_reaches_the_payment_provider():
    """通行证要能换出支付通道认的商品号。seats 与 pass 都曾掉进 order['pack'] 那条
    兜底路径直接 KeyError —— 表现是点"开通"报 500 (2026-09-06 老板实测到)。"""
    from app.payments import waffo_provider

    assert waffo_provider._item_of({"kind": "pass", "product_id": "coze"}) == "pass:coze"
    assert waffo_provider._item_of({"kind": "plan", "tier": "pro", "cycle": "monthly"}) == "plan:pro:monthly"
    assert waffo_provider._item_of({"kind": "seats", "seats": 5}) == "seats:5"
    assert waffo_provider._item_of({"kind": "pack", "pack": "pack1000"}) == "pack:pack1000"


def test_landing_shows_the_local_workspace_path(client, monkeypatch):
    """主页要把「工作台搬到自己机器上」那条路摆出来 —— README 里有, 主页一直没有。

    钉的是**入口存在**, 不是文案: i18n 键写错时 t() 返回空串, 页面照样 200 ——
    所以顺带断言标题不是空的。

    **认那张卡本身, 不认它的链接。** 原先这里断言的是 README 锚点, 而这张卡讲的
    是本机算力, 一个字都没提开源 —— 源码露出一关 (SHOW_SOURCE_LINKS), CTA 改指
    下载页, 这条断言当场红, 可卡片明明还在。反过来更糟: 隔壁那条"自部署实例要
    把卡收起来"的断言也只认锚点, 露出一关它就恒绿, 从此什么也测不到。
    """
    from app import config, i18n

    monkeypatch.setattr(config, "WORK_ENABLED", True)
    title = i18n.t("zh", "home.two.local_title")

    monkeypatch.setattr(config, "SHOW_SOURCE_LINKS", True)
    body = client.get("/", headers={"accept-language": "zh"}).text
    assert title in body, "主页没有本机工作台入口"
    assert "#run-the-workspaces-on-your-own-machine" in body

    monkeypatch.setattr(config, "SHOW_SOURCE_LINKS", False)
    body = client.get("/", headers={"accept-language": "zh"}).text
    assert title in body, "源码露出一关, 整张本机卡也跟着没了 —— 它讲的不是开源"
    assert "#run-the-workspaces-on-your-own-machine" not in body

    for key in ("home.two.local_title", "home.two.local_body", "home.two.local_cta"):
        for lang in ("zh", "en"):
            assert i18n.t(lang, key), f"{key} 缺 {lang} 译文, 卡片会渲染成空白"


def test_landing_hides_the_local_path_on_selfhost(client, monkeypatch):
    """自部署实例没有"托管网关"这个前提, 那张卡整张都不成立, 跟云卡一起收起来。"""
    from app import config, i18n

    monkeypatch.setattr(config, "WORK_ENABLED", False)
    body = client.get("/", headers={"accept-language": "zh"}).text
    assert i18n.t("zh", "home.two.local_title") not in body
