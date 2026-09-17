"""三个 2026-09-16 安全回归的守护测试。

都遵循同一条判据: **把修复还原, 这里必须红**。三条都变异验过。

1. 伪造 X-Forwarded-For 绕过按 IP 的限流/锁定
2. /api/live/incidents 只认登录不认管理员
3. 同一 request_id 重复计费 (工作台 reaper 的分钟桶)
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-sec-")
os.environ.update(
    {
        "DHC_DEV": "1",
        "AUTH_SECRET": "test-secret",
        "DHC_DATA_DIR": _TMP,
        "DB_PATH": os.path.join(_TMP, "test.db"),
        "UPSTREAM_API_KEY": "sk-upstream-test",
        "FREE_SIGNUP_CREDITS": "500",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import accounts, config, credits, db  # noqa: E402
from app.main import app  # noqa: E402
from tests._signup import signup_with_password  # noqa: E402


class _Req:
    """够 _client_ip 用的最小请求替身 (只读 headers 和 client)。"""

    class _C:
        host = "10.0.0.9"

    def __init__(self, headers):
        self.headers = headers
        self.client = self._C()


# --- 1) XFF 伪造 --------------------------------------------------------------


def test_leftmost_xff_is_not_trusted():
    """最左段由调用方写。线上是 CF -> Caddy 两跳, 真实客户端在倒数第二位。

    还原成 fwd.split(",")[0] 的话, 这里拿到的是 1.2.3.4 —— 攻击者每次换一个,
    按 IP 的限流就永远不触发。
    """
    assert config.TRUSTED_PROXY_HOPS == 2
    ip = accounts._client_ip(_Req({"x-forwarded-for": "1.2.3.4, 203.0.113.7, 172.68.0.1"}))
    assert ip == "203.0.113.7", "应取倒数第 2 段(真实客户端), 不是最左的伪造段"


def test_cf_connecting_ip_wins():
    """Cloudflare 覆盖写入这个头, 调用方改不了; 源站 iptables 只放行 CF 网段。"""
    ip = accounts._client_ip(
        _Req({"cf-connecting-ip": "198.51.100.5", "x-forwarded-for": "1.2.3.4, 9.9.9.9, 172.68.0.1"})
    )
    assert ip == "198.51.100.5"


def test_short_chain_falls_back_instead_of_indexing_off_the_end():
    """链比跳数短时不能取到空 —— 宁可粒度粗, 也不要把所有人并进一个桶。"""
    assert accounts._client_ip(_Req({"x-forwarded-for": "203.0.113.9"})) == "203.0.113.9"
    assert accounts._client_ip(_Req({})) == "10.0.0.9"


def test_rotating_forged_xff_still_hits_the_per_ip_login_cap():
    """端到端: 换着伪造 XFF 撞库, 按 IP 的 30 次/15 分仍然要拦下来。

    修复前实测 35 次全是 401、从不 429 (Strix 的 PoC 也是这个形状)。
    """
    from app import rate_limit

    rate_limit._windows.clear()
    c = TestClient(app)
    signup_with_password(c, "victim@test.local", "password123")
    c.post("/api/auth/logout")

    fresh = TestClient(app)
    got429 = False
    for i in range(40):
        r = fresh.post(
            "/api/auth/login",
            json={"email": "victim@test.local", "password": "wrong-%d" % i},
            # 每次换一个最左段, 右边两跳固定 —— 模拟真实链路下的伪造
            headers={"x-forwarded-for": "9.9.%d.%d, 203.0.113.7, 172.68.0.1" % (i, i)},
        )
        if r.status_code == 429:
            got429 = True
            break
    assert got429, "伪造最左段 XFF 绕过了按 IP 的登录限流"


# --- 2) incidents 管理员闸 -----------------------------------------------------


@pytest.fixture
def live_on(monkeypatch):
    """让 live._enabled() 为真。

    不走环境变量: config 在 **import 时**读 env, 全量跑时谁先导入 config 谁说了算,
    单跑绿、全量红 (踩过)。直接改模块属性, _enabled() 是调用时读的。
    """
    monkeypatch.setattr(config, "LIVE_GPU_URL", "http://127.0.0.1:9/live-stub")
    yield


def test_live_incidents_requires_admin(live_on):
    """同文件其它运维读口都调了 _require_admin, 这条原来漏了。

    判据必须落在 **403** 上, 不能写成 "不是 200": live 关着时任何人都拿 404,
    那样拿掉闸门测试照样绿 (第一版就是这么假绿的, 变异验证抓出来的)。
    所以上面把 LIVE_GPU_URL 配上, 让 _enabled() 为真。
    """
    assert config.LIVE_GPU_URL, "没开 live 的话这个用例对闸门不敏感"
    c = TestClient(app)
    signup_with_password(c, "plain@test.local", "password123")
    r = c.get("/api/live/incidents")
    assert r.status_code == 403, "普通登录用户不该读到运营事件数据, 期望 403, 实得 %s" % r.status_code


def test_live_incidents_still_open_to_admin(live_on):
    """闸门不能把管理员也挡了 —— 否则这条修复等于把功能删了。"""
    c = TestClient(app)
    signup_with_password(c, "boss@test.local", "password123")
    db.query("UPDATE users SET role='admin' WHERE email=?", ("boss@test.local",))
    r = c.get("/api/live/incidents")
    assert r.status_code == 200, r.status_code


# --- 3) 计费幂等 ---------------------------------------------------------------


def _usage_rows(user_id: str, request_id: str) -> int:
    rows = db.query("SELECT id FROM usage_log WHERE user_id=? AND request_id=?", (user_id, request_id))
    return len(rows)


def test_same_request_id_bills_once():
    """工作台 reaper 用 ws-{product}-{分钟桶} 当幂等键, 但表上没有唯一约束。

    多进程下同一分钟会被记两次 = 用户被多扣。spend 现在在持锁事务内先查后写。
    """
    c = TestClient(app)
    signup_with_password(c, "meter@test.local", "password123")
    uid = db.query_one("SELECT id FROM users WHERE email=?", ("meter@test.local",))["id"]

    rid = "ws-dsh-29000001"
    credits.spend(uid, 3, kind="work", model="work:dsh", request_id=rid, units=1)
    credits.spend(uid, 3, kind="work", model="work:dsh", request_id=rid, units=1)  # 重放
    assert _usage_rows(uid, rid) == 1, "同一 request_id 记了多行 —— 用户被重复计费"


def test_distinct_request_ids_still_bill_each_time():
    """幂等不能把正常计费也吞掉: 不同分钟桶必须各记一次。"""
    c = TestClient(app)
    signup_with_password(c, "meter2@test.local", "password123")
    uid = db.query_one("SELECT id FROM users WHERE email=?", ("meter2@test.local",))["id"]

    for minute in (29000010, 29000011, 29000012):
        credits.spend(uid, 2, kind="work", model="work:dsh", request_id="ws-dsh-%d" % minute, units=1)
    total = db.query("SELECT id FROM usage_log WHERE user_id=? AND kind=?", (uid, "work"))
    assert len(total) == 3, "不同 request_id 被误判成重复, 会少收钱"


def test_empty_request_id_is_not_deduped():
    """没给 request_id 的调用不参与幂等 —— 否则所有空 id 的计费只会记第一条。"""
    c = TestClient(app)
    signup_with_password(c, "meter3@test.local", "password123")
    uid = db.query_one("SELECT id FROM users WHERE email=?", ("meter3@test.local",))["id"]

    before = len(db.query("SELECT id FROM usage_log WHERE user_id=?", (uid,)))
    credits.spend(uid, 1, kind="llm", model="m")
    credits.spend(uid, 1, kind="llm", model="m")
    after = len(db.query("SELECT id FROM usage_log WHERE user_id=?", (uid,)))
    assert after - before == 2
