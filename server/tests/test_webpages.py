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
    assert "deepseek-harness-cloud" in body
    assert "桌面 AI 编程助手" in body
    assert "/static/app.css" in body
    assert "/download" in body
    assert "/legal/terms" in body


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
        "# 服务条款\n\n欢迎使用 **DSH Cloud**。\n\n- 第一条\n- 第二条\n\n"
        "| 项目 | 说明 |\n|---|---|\n| 积分 | 1 积分 = ¥0.01 |\n\n"
        "详见[隐私政策](/legal/privacy)。\n",
        encoding="utf-8",
    )
    try:
        r = client.get("/legal/terms")
        assert r.status_code == 200
        assert "文档整理中" not in r.text
        assert "<h1>服务条款</h1>" in r.text
        assert "<strong>DSH Cloud</strong>" in r.text
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
    # 而且不能再谎称"正在重新构建" —— 自部署只是没配下载地址
    download = client.get("/download").text
    assert "云工作台也未启用" in download

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
    assert "https://dshcloud.online/work" in body
    assert "官方托管版" in body, "必须标明那是托管版, 不能装成本地功能"

    # 托管版自己: hosted_site 与 PUBLIC_BASE 同源时不挂
    monkeypatch.setattr(config, "PUBLIC_BASE", "https://dshcloud.online")
    assert "dshcloud.online/work" not in client.get("/download").text

    # 自部署方想彻底关掉引流: 置空即可
    monkeypatch.setattr(config, "PUBLIC_BASE", "http://localhost:8787")
    monkeypatch.setattr(config, "HOSTED_SITE", "")
    assert "dshcloud.online" not in client.get("/download").text


def test_hero_composer_never_leads_into_the_dead_end(client, monkeypatch):
    """首页那个输入框是最显眼的入口, 不能把人送进 /work → /download 的死胡同。

    2026-08-25 老板看着自部署首页问"点云端体验去哪" —— 一查, 导航按钮和云工作台
    卡片确实已经按开关隐藏了, 但输入框 (回车即执行) 还硬指着本站 /work。
    """
    from app import config

    monkeypatch.setattr(config, "WORK_ENABLED", False)
    body = client.get("/").text
    assert 'data-target="https://dshcloud.online"' in body, "该把任务送去托管版"
    assert "官方托管版" in body, "必须说清任务会在托管版执行, 不能让人以为跑在本机"

    # 没有托管版可去时, 输入框本身就不该出现 —— 没有任何地方能执行任务
    monkeypatch.setattr(config, "HOSTED_SITE", "")
    assert "hero-composer" not in client.get("/").text

    # 本地工作台开着: 一切照旧, 走本站
    monkeypatch.setattr(config, "WORK_ENABLED", True)
    body = client.get("/").text
    assert 'data-target=""' in body and "hero-composer" in body
