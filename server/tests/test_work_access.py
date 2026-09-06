"""The cloud-workspace gate: machine time is its own resource, not credits.

The rules that must hold no matter what the client sends:
  * a workspace minute NEVER costs credits, and a token call never costs
    minutes — the two meters are independent (GitHub-Actions shape);
  * the included allowance comes from the plan tier;
  * purchased minute packs extend past the allowance;
  * the intro pass price is a first-purchase offer, decided server-side;
  * a renewal bought early extends, it never burns the remainder.
"""

import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="dhc-wa-")
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

from app import config, credits, db, plans, work_access  # noqa: E402

db.ensure_schema()


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    # Included minutes now come from the price table (the real source), so the
    # tests pin a small table rather than the config fallback — otherwise every
    # allowance change in pricing.json would rewrite these numbers.
    table = dict(plans.pricing())
    tiers = {k: dict(v) for k, v in table["tiers"].items()}
    tiers["free"]["work_minutes"] = 120
    tiers["pro"]["work_minutes"] = 3600
    table["tiers"] = tiers
    monkeypatch.setattr(plans, "pricing", lambda: table)
    monkeypatch.setattr(config, "WORK_FREE_MINUTES", 120)


def _user(uid):
    with db.tx() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.execute("DELETE FROM usage_log WHERE user_id=?", (uid,))
        c.execute("DELETE FROM work_passes WHERE user_id=?", (uid,))
        c.execute("DELETE FROM minute_grants WHERE user_id=?", (uid,))
        c.execute("DELETE FROM credit_grants WHERE user_id=?", (uid,))
        c.execute("DELETE FROM subscriptions WHERE user_id=?", (uid,))
        c.execute(
            "INSERT INTO users (id,email,session_epoch,created) VALUES (?,?,0,0)", (uid, uid + "@t.local")
        )
    return uid


def _burn(uid, minutes):
    """Consume active workspace minutes the same way the reaper meters them."""
    for _ in range(minutes):
        credits.spend(uid, 0, kind=work_access.MINUTE_KIND, model="dshwork")
        work_access.consume_minute(uid)


def test_included_allowance_then_paywall():
    uid = _user("u_wa1")
    assert work_access.blocked_reason(uid) is None
    _burn(uid, 119)
    assert work_access.state(uid)["minutes_left"] == 1
    assert work_access.blocked_reason(uid) is None
    _burn(uid, 1)
    assert work_access.state(uid)["minutes_left"] == 0
    assert work_access.blocked_reason(uid) == "work_quota"


def test_machine_time_never_costs_credits():
    """The whole point of the split: a workspace minute is metered in minutes,
    so a long-running container can never drain the token balance."""
    uid = _user("u_wa2")
    credits.grant(uid, 1000, 3600, kind="grant_signup")
    before = credits.balance(uid)
    _burn(uid, 60)
    assert credits.balance(uid) == before
    assert work_access.used_minutes(uid) == 60


def test_token_spend_never_eats_the_minute_allowance():
    """And the reverse: model and search calls must not consume machine time."""
    uid = _user("u_wa2b")
    credits.grant(uid, 5000, 3600, kind="grant_signup")
    for _ in range(50):
        credits.spend(uid, 1, kind="llm", model="deepseek-v4-flash")
        credits.spend(uid, 5, kind="search", model="web_search:zhipu")
    assert work_access.used_minutes(uid) == 0
    assert work_access.state(uid)["minutes_left"] == 120


def test_plan_tier_sets_the_allowance():
    """Included minutes come from the plan, GitHub-Actions style."""
    uid = _user("u_wa2c")
    assert work_access.included_minutes(uid) == 120  # free
    plans.apply_plan(uid, "pro", "monthly")
    assert work_access.included_minutes(uid) == 3600  # pro: 60h
    st = work_access.state(uid)
    assert st["plan_tier"] == "pro" and st["minutes_left"] == 3600


def test_machine_hours_are_the_only_gate():
    """The 7-day pass was a second way to buy workspace access, parallel to the
    monthly hours. Two meters that can disagree is one more than the product
    needs, so access is now decided by hours alone."""
    import inspect

    from app import work_access

    src = inspect.getsource(work_access)
    for gone in ("active_pass", "grant_pass", "next_price", "PASS_INTRO"):
        assert gone not in src, f"{gone} survived the pass removal"
    st = work_access.state("u_nobody")
    assert "pass_active" not in st and "next_price" not in st
    assert st["allowed"] == (st["minutes_left"] > 0)


def test_purchased_minutes_extend_beyond_the_plan():
    uid = _user("u_wa2d")
    _burn(uid, 120)  # allowance spent
    assert work_access.blocked_reason(uid) == "work_quota"
    work_access.grant_minutes(uid, 300, 30 * 86400, kind="pack")
    assert work_access.blocked_reason(uid) is None
    assert work_access.state(uid)["minutes_left"] == 300
    _burn(uid, 10)
    assert work_access.minute_packs_left(uid) == 290


# ---- 机时按格子大小折算 (老板 2026-09-06 定) --------------------------------


def test_minute_units_scale_with_memory():
    """原先一分钟就是一分钟, 与格子多大无关 —— 那是 0.5 核 1G 时代的口径, 而 Coze
    占的内存是 dsh 的十六倍, 花的却是同样的额度。"""
    from app import products

    assert products.minute_units("dsh") == 1, "1G 不足一份, 按一份算"
    assert products.minute_units("pi") == 1, "2G 正好一份"
    assert products.minute_units("openmausbot") == 2, "4G 两份"
    assert products.minute_units("coze") == 8, "16G 八份"
    assert products.minute_units("这个产品不存在") >= 1, "认不出来也要有个下限, 不能白用"


def test_used_minutes_counts_units_and_treats_legacy_rows_as_one():
    """加权是从上线那一刻起生效的 —— 老行没有 units 列, 一律当一份。追溯改写的话
    会有人在毫无动作的情况下突然超额。"""
    uid = _user("u_units")
    now = time.time()
    with db.tx() as c:
        # 老行: units 为空
        c.execute(
            "INSERT INTO usage_log (id,user_id,device_id,kind,model,credits,request_id,created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("ul_old", uid, "", work_access.MINUTE_KIND, "work:coze", 0, "", now),
        )
        # 新行: 八份
        c.execute(
            "INSERT INTO usage_log (id,user_id,device_id,kind,model,credits,request_id,created,units) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("ul_new", uid, "", work_access.MINUTE_KIND, "work:coze", 0, "", now, 8),
        )
    assert work_access.used_minutes(uid, since=0) == 9, "老行 1 份 + 新行 8 份"
    assert work_access.wall_clock_minutes(uid, since=0) == 2, "实际只开了两分钟"


def test_consume_minute_drains_across_grants():
    """一张券只剩 3 份而这一分钟要 8 份 —— 得接着扣下一张。原先一次只扣一份, 加权
    之后那样会少扣。"""
    uid = _user("u_drain")
    work_access.grant_minutes(uid, 3, ttl_s=86400, kind="grant_admin")
    work_access.grant_minutes(uid, 10, ttl_s=86400, kind="grant_admin")
    before = work_access.minute_packs_left(uid)
    assert before == 13
    # 先把套餐额度耗掉, 否则不动券
    now = time.time()
    with db.tx() as c:
        for i in range(work_access.included_minutes(uid) + 1):
            c.execute(
                "INSERT INTO usage_log (id,user_id,device_id,kind,model,credits,request_id,created,units) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"ul_d{i}", uid, "", work_access.MINUTE_KIND, "work:dsh", 0, "", now, 1),
            )
    work_access.consume_minute(uid, 8)
    assert work_access.minute_packs_left(uid) == 5, "3 + 10 扣掉 8 应剩 5"
