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


def test_back_to_back_calls_are_both_charged(secret, monkeypatch):
    """背靠背两通短通话要**收两笔**, 别被幂等键当成重投吞掉。

    幂等键防的是同一份回报被投递两次 (GPU 侧失败只记日志不重试, 所以窗口不必宽)。
    先前的写法把 ts 末两位抹掉按 ~100 秒分桶, 结果是: 用户打 30 秒挂断、再打
    30 秒, 两笔 (各计 1 分钟) 落进同一个桶, 第二笔静默不收。少收钱不报错, 是
    最不容易发现的那种。
    """
    from app import credits

    seen: list[str] = []
    monkeypatch.setattr(credits, "spend", lambda *a, **kw: seen.append(kw["request_id"]))

    from fastapi.testclient import TestClient

    from app.main import app

    tenant = "d-u_back2back"
    with TestClient(app) as c:
        for ts in (str(int(time.time())), str(int(time.time()) + 3)):
            r = c.post(
                "/api/avatar/meter",
                params={"ts": ts, "tenant": tenant, "minutes": "1", "sig": _sign(secret, ts, tenant, "1")},
            )
            assert r.status_code == 200, r.text
    assert len(set(seen)) == 2, f"两通通话该有两个幂等键, 实得 {seen}"


def test_a_redelivered_report_is_not_charged_twice(secret, monkeypatch):
    """同一份回报重复投递 -> 幂等键相同, 由 credits.spend 去重。"""
    from app import credits

    seen: list[str] = []
    monkeypatch.setattr(credits, "spend", lambda *a, **kw: seen.append(kw["request_id"]))

    from fastapi.testclient import TestClient

    from app.main import app

    ts, tenant = str(int(time.time())), "d-u_dup"
    sig = _sign(secret, ts, tenant, "2")
    with TestClient(app) as c:
        for _ in range(2):
            r = c.post("/api/avatar/meter", params={"ts": ts, "tenant": tenant, "minutes": "2", "sig": sig})
            assert r.status_code == 200, r.text
    assert len(set(seen)) == 1, f"重投该是同一个幂等键, 实得 {seen}"
