"""数字人的计费与令牌。

这个产品与其它工作台**结构上不同**: 不起容器, 转发到共享 GPU, 按真实通话分钟
收积分。所以它的错法也不同 —— 全都是钱和隐私上的:
  · 签名不覆盖分钟数 = 任何人都能改大数字重放, 直接从用户账上扣钱;
  · 租户不加前缀 = 两条产品线的用户撞 id, 而撞了就是看到别人的形象;
  · 排队时间计费 = 让用户为我们的容量不足付钱。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import time

import pytest

_TMP = tempfile.mkdtemp(prefix="dhc-avatar-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import avatar, config
from tests._signup import signup


@pytest.fixture()
def secret(monkeypatch):
    monkeypatch.setattr(config, "AVATAR_TOKEN_SECRET", "test-avatar-secret")
    return "test-avatar-secret"


def _sign(secret: str, ts: str, tenant: str, minutes: str) -> str:
    return hmac.new(secret.encode(), f"{ts}|{tenant}|{minutes}".encode(), hashlib.sha256).hexdigest()


def test_report_signature_covers_the_minutes(secret):
    """签名**必须覆盖分钟数**。

    只签租户的话, 任何拿到一个合法回报的人都能把 minutes 改大再重放 —— 那是直接
    从用户账上扣钱, 而且看起来完全合法。
    """
    ts, tenant = str(int(time.time())), "d-u_abc"
    good = _sign(secret, ts, tenant, "3")
    assert avatar._verify_report(ts, tenant, "3", good)
    # 拿着为 3 分钟签的名去报 30 分钟 —— 必须验不过
    assert not avatar._verify_report(ts, tenant, "30", good)


def test_stale_report_is_rejected(secret):
    """过期的回报不认 —— 否则一个旧签名可以被无限重放。"""
    old = str(int(time.time()) - avatar.TOKEN_TTL - 60)
    assert not avatar._verify_report(old, "d-u_abc", "1", _sign(secret, old, "d-u_abc", "1"))


def test_no_secret_means_no_trust(secret, monkeypatch):
    """没配密钥时一律不信 —— 空密钥不能变成"人人都能签"。"""
    monkeypatch.setattr(config, "AVATAR_TOKEN_SECRET", "")
    ts = str(int(time.time()))
    assert not avatar._verify_report(ts, "d-u_abc", "1", _sign("", ts, "d-u_abc", "1"))


def test_tenant_is_namespaced(secret):
    """DSH 用户的租户要带前缀。

    这张卡上同时跑着口袋专家, 两条产品线的用户 id 各自独立 —— 不加前缀就可能
    撞上, 而撞了的后果是**看到别人上传的脸**。
    """
    assert avatar._tenant("u_abc").startswith(avatar.TENANT_PREFIX)


def test_signed_token_matches_gpu_side_format(secret):
    """令牌格式必须与 GPU 侧 _check_token 的 v2 对齐 (ts.tenant.sig)。

    对不上的表现是每一通电话都 bad token, 而两边日志都只说"验签失败", 看不出
    是格式问题。
    """
    tok = avatar.sign_token("u_abc")
    ts, tenant, sig = tok.split(".")
    assert tenant == "d-u_abc"
    want = hmac.new(secret.encode(), f"{ts}|{tenant}".encode(), hashlib.sha256).hexdigest()
    assert sig == want


def _charged(client, secret, tenant, ts, minutes):
    r = client.post(
        "/api/avatar/meter",
        params={"ts": ts, "tenant": tenant, "minutes": minutes, "sig": _sign(secret, ts, tenant, minutes)},
    )
    assert r.status_code == 200, r.text
    return r.json().get("charged", 0)


def test_back_to_back_calls_are_both_charged(secret):
    """背靠背两通短通话要**收两笔**, 别被去重当成重投吞掉。

    用户打 30 秒挂断、再打 30 秒 = 两笔各 1 分钟。早先把回报时间戳末两位抹掉
    按 ~100 秒分桶, 这两笔落进同一个桶, 第二笔静默不收。少收钱不报错。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    tenant, t0 = "d-u_back2back", int(time.time())
    with TestClient(app) as c:
        first = _charged(c, secret, tenant, str(t0), "1")
        second = _charged(c, secret, tenant, str(t0 + 3), "1")
    assert first > 0 and second > 0, f"两通都该收钱, 实得 {first} / {second}"


def test_a_redelivered_report_is_not_charged_twice(secret):
    """同一份回报投两次, **只扣一次钱**。

    这条测试直接数扣款, 不看幂等键长什么样 —— 先前那版比对的是 request_id 字符串,
    而 usage_log.request_id 根本没有唯一约束: 键"对上了"钱照扣两次, 测试却是绿的。
    断言要落在钱上。
    """
    from fastapi.testclient import TestClient

    from app import credits
    from app.main import app

    tenant, ts = "d-u_dup", str(int(time.time()))
    spent: list[int] = []
    real_spend = credits.spend

    def counting_spend(user_id, amount, **kw):
        spent.append(amount)
        return real_spend(user_id, amount, **kw)

    credits.spend = counting_spend
    try:
        with TestClient(app) as c:
            first = _charged(c, secret, tenant, ts, "2")
            again = _charged(c, secret, tenant, ts, "2")
    finally:
        credits.spend = real_spend
    assert first > 0, "第一次该收钱"
    assert again == 0, f"重投不该再收, 实得 {again}"
    assert len(spent) == 1, f"只该扣一次款, 实际扣了 {len(spent)} 次: {spent}"


def test_a_websocket_can_authenticate_by_cookie(secret):
    """WS 握手要能用会话 cookie 认出人来, 别在检查里崩掉。

    线上实测栽过: `_cookie_write_allowed` 直接读 request.method, 而 WebSocket
    没有这个属性 -> AttributeError -> 连上就断。页面本身好好的, 只有"点了开始
    通话没反应" —— 与网络不好、与 GPU 忙, 从外面看一模一样。
    """
    from app import accounts

    class _FakeWS:  # WebSocket: 有 headers/cookies, 没有 method
        headers = {"origin": ""}
        cookies: dict = {}

    assert accounts._cookie_write_allowed(_FakeWS()) is True


def test_a_websocket_from_another_site_is_refused(secret, monkeypatch):
    """跨站页面发起的 WS 握手要挡掉。

    WS 握手**不受 CORS 约束**, 而建立通话要烧 GPU、按分钟扣积分 —— 与跨源
    POST 同性质, 所以按不安全方法查来源, 不是因为"是 WS"就放行。
    """
    from app import accounts

    class _EvilWS:
        headers = {"origin": "https://evil.example"}
        cookies: dict = {}

    assert accounts._cookie_write_allowed(_EvilWS()) is False


def _fake_upstream(monkeypatch, capture: dict):
    """把上游 chat/completions 换成假的, 把请求体留下来看。"""
    import httpx

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "我在呢。"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 8},
            }

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            capture["url"] = url
            capture["body"] = json
            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _C())


def test_say_replies_and_bills(secret, monkeypatch):
    """她说什么由服务端出, 并且**照常计费** —— 否则通话里的模型消耗是白送的。"""
    from fastapi.testclient import TestClient

    from app import config, credits
    from app.main import app

    monkeypatch.setattr(config, "UPSTREAM_BASE_URL", "https://upstream.test/v1")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "k")
    cap: dict = {}
    _fake_upstream(monkeypatch, cap)
    spent: list[int] = []
    monkeypatch.setattr(credits, "spend", lambda *a, **kw: spent.append(a[1] if len(a) > 1 else 0))

    with TestClient(app) as c:
        signup(c, "avatar-say@example.com")
        r = c.post("/api/avatar/say", json={"text": "在吗"})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "我在呢。"
    assert spent and spent[0] > 0, "出了回复却没计费"


def test_say_caps_the_history_the_client_sends(secret, monkeypatch):
    """历史**在服务端截断**: 前端的边界不可信, 而这段会原样变成账单。"""
    from fastapi.testclient import TestClient

    from app import config, credits
    from app.main import app

    monkeypatch.setattr(config, "UPSTREAM_BASE_URL", "https://upstream.test/v1")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "k")
    cap: dict = {}
    _fake_upstream(monkeypatch, cap)
    monkeypatch.setattr(credits, "spend", lambda *a, **kw: None)

    long_history = [{"role": "user", "content": "x" * 5000} for _ in range(50)]
    with TestClient(app) as c:
        signup(c, "avatar-hist@example.com")
        r = c.post("/api/avatar/say", json={"text": "在吗", "history": long_history})
    assert r.status_code == 200, r.text
    msgs = cap["body"]["messages"]
    assert len(msgs) == 10, f"system + 8 条历史 + 这句, 实得 {len(msgs)}"
    assert all(len(m["content"]) <= 600 for m in msgs[1:]), "单条没截断"


def test_say_needs_something_to_say(secret, monkeypatch):
    """空话不发给模型 —— 识别器偶尔会吐空串, 那是白花钱。"""
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app

    monkeypatch.setattr(config, "UPSTREAM_BASE_URL", "https://upstream.test/v1")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "k")
    with TestClient(app) as c:
        signup(c, "avatar-empty@example.com")
        assert c.post("/api/avatar/say", json={"text": "   "}).status_code == 400


def test_the_call_page_may_load_blob_media_and_open_the_mic():
    """通话页的两处安全头例外**必须在**, 否则通话静默失灵。

    · `media-src blob:` —— 视频源是 MediaSource 的 blob: URL。少了它,
      default-src 'self' 会挡掉, 而表现是"画面一帧不动": WebSocket 照常收字节、
      计时照走、积分照扣, 只有控制台里一行 CSP 违规。线上真栽过。
    · `microphone=(self)` —— 这一页靠说话用。

    其余页面维持全关 —— 这两条是**给这一页开的口子**, 不是全站放开。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        signup(c, "avatar-csp@example.com")  # 未登录会 303 走掉, 那是张没有 CSP 的空响应
        av = c.get("/avatar")
        home = c.get("/")
    assert "media-src 'self' blob:" in av.headers.get("content-security-policy", "")
    assert "microphone=(self)" in av.headers.get("permissions-policy", "")
    # 别的页面不该跟着放开
    assert "blob:" not in home.headers.get("content-security-policy", "")
    assert "microphone=()" in home.headers.get("permissions-policy", "")
