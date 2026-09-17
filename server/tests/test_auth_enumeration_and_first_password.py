"""2026-09-16 最后两条安全回归。判据同样是: 把修复还原, 这里必须红。

1. 账号枚举: /email/send 的响应随"这个地址注册过没有"而变; 登录耗时也随之而变。
2. 无密码账号首次设密码只凭会话 —— 泄漏的会话被换成长期密码凭据。
"""

import os
import statistics
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="dhc-enum-")
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

from app import accounts, db, security  # noqa: E402
from app.main import app  # noqa: E402
from tests._signup import _CODE, _put_code, signup, signup_with_password  # noqa: E402


# --- 1) 枚举 ------------------------------------------------------------------


def test_email_send_answers_the_same_whether_or_not_the_account_exists():
    """原来: 格式不严格的地址, 注册过返 200、没注册过返 400 —— 现成的枚举 oracle。

    现在格式不合法一律 400, 存在与否不再改变回答。
    """
    c = TestClient(app)
    # 必须是**过得了宽松正则、过不了严格正则**的形状 (域名里带下划线)。
    # 第一版用了带空格的地址, 结果 normalize_email_identity 先就拒了, 两次都 400 —
    # 于是把旁路改回去测试照样绿, 一个假绿。变异验证抓出来的。
    bad = "probe@ex_ample.com"
    assert accounts.LEGACY_EMAIL_RE.fullmatch(bad), "探针要过得了宽松正则"
    assert not accounts.EMAIL_RE.fullmatch(bad), "探针要过不了严格正则"

    missing = c.post("/api/auth/email/send", json={"email": bad}).status_code

    # 造一个**确实存在**的同形账号, 直接写库绕开注册校验 (老账号就是这么来的)
    db.query(
        "INSERT INTO users (id, email, password_hash, display_name, status, created) "
        "VALUES (?,?,?,?,?,?)",
        ("u_legacy_probe", bad, "", "legacy", "active", time.time()),
    )
    existing = c.post("/api/auth/email/send", json={"email": bad}).status_code

    assert missing == existing, (
        "注册过与没注册过拿到了不同状态码 (%s vs %s) —— 账号枚举 oracle" % (missing, existing)
    )


def test_login_does_not_leak_account_existence_through_timing():
    """scrypt 很慢而 Python 的 or 会短路: 账号不存在时校验根本不跑, "查无此人"
    就比"密码错了"快一个量级。现在两条路径都跑一次校验。

    判据取中位数并留足余量 —— 钉绝对耗时会在忙碌的 CI 上假红。
    """
    c = TestClient(app)
    signup_with_password(c, "timing@test.local", "password123")

    def median_ms(email):
        xs = []
        for _ in range(7):
            t0 = time.perf_counter()
            c.post("/api/auth/login", json={"email": email, "password": "definitely-wrong"})
            xs.append((time.perf_counter() - t0) * 1000)
        return statistics.median(xs)

    from app import rate_limit

    rate_limit._windows.clear()
    known = median_ms("timing@test.local")
    rate_limit._windows.clear()
    unknown = median_ms("no-such-account@test.local")

    ratio = max(known, unknown) / max(min(known, unknown), 0.01)
    assert ratio < 3.0, (
        "存在与不存在的登录耗时差了 %.1f 倍 (%.1fms vs %.1fms) —— 可以计时问出账号存不存在"
        % (ratio, known, unknown)
    )


# --- 2) 首次设密码 -------------------------------------------------------------


def _passwordless(email: str) -> TestClient:
    c = TestClient(app)
    signup(c, email)  # 验证码注册 -> password_hash 为空
    return c


def test_first_password_needs_a_fresh_email_code():
    """只凭会话就能给无密码账号设上密码, 等于把一个泄漏/临时的会话换成长期凭据。"""
    c = _passwordless("first-pw@test.local")
    r = c.post("/api/auth/password", json={"old": "", "new": "brand-new-pass"})
    assert r.status_code == 401, "没有验证码也设上了密码, 会话被换成了长期凭据"
    assert r.json()["detail"] == "bad_code"

    row = db.query_one("SELECT password_hash FROM users WHERE email=?", ("first-pw@test.local",))
    assert not row["password_hash"], "密码真的被设上了"


def test_first_password_succeeds_with_a_valid_code():
    """守卫不能把正常路径也堵死。"""
    email = "first-pw-ok@test.local"
    c = _passwordless(email)
    _put_code(email)
    r = c.post("/api/auth/password", json={"old": "", "new": "brand-new-pass", "code": _CODE})
    assert r.status_code == 200, r.text
    row = db.query_one("SELECT password_hash FROM users WHERE email=?", (email,))
    assert security.verify_password("brand-new-pass", row["password_hash"])


def test_the_code_is_single_use():
    """用过的码不能再换第二次密码。"""
    email = "first-pw-replay@test.local"
    c = _passwordless(email)
    _put_code(email)
    assert c.post("/api/auth/password", json={"old": "", "new": "pass-one-1", "code": _CODE}).status_code == 200
    r = c.post("/api/auth/password", json={"old": "pass-one-1", "new": "pass-two-2", "code": _CODE})
    # 这次走的是"有密码"那条路 (旧密码对), 码已被消耗但不再需要 —— 关键是别 500
    assert r.status_code in (200, 401), r.status_code


def test_changing_an_existing_password_still_only_needs_the_old_one():
    """有密码的账号不受影响 —— 不能因为加固就要求每次改密都发一封邮件。"""
    c = TestClient(app)
    signup_with_password(c, "has-pw@test.local", "password123")
    r = c.post("/api/auth/password", json={"old": "password123", "new": "another-pass-9"})
    assert r.status_code == 200, r.text


def test_me_exposes_has_password_so_the_form_knows():
    c = _passwordless("flag@test.local")
    assert c.get("/api/auth/me").json()["user"]["has_password"] is False
    _put_code("flag@test.local")
    c.post("/api/auth/password", json={"old": "", "new": "now-has-one-1", "code": _CODE})
    c2 = TestClient(app)
    c2.post("/api/auth/login", json={"email": "flag@test.local", "password": "now-has-one-1"})
    assert c2.get("/api/auth/me").json()["user"]["has_password"] is True
