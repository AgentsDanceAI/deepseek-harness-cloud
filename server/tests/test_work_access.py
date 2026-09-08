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


def test_machine_hours_are_still_the_only_meter():
    """机时是**唯一的计量表**。

    老的"七天通行证"是买工作台使用权的第二条路, 与每月机时并行 —— 两个能互相矛盾的
    计量表比产品需要的多一个, 所以当时删掉了, 使用权只由机时决定。

    2026-09-06 通行证以**另一种东西**回来了 (老板: "16 个是否可以配置化加锁, 比如
    9.9 才给开通试用"): 它回答的是"这一格你能不能开", 不是"你还剩多少时间"。开了之后
    照样按机时计量、照样受机时闸限制。这个测试钉住的就是这条界线 —— 通行证一分钟机时
    都不给, 也绕不过机时耗尽的闸。
    """
    from app import products, work_access

    uid = _user("u_pass_meter")
    _burn(uid, 120)  # 机时耗尽
    assert work_access.blocked_reason(uid) == "work_quota"
    work_access.grant_pass(uid, "coze", 7)
    assert work_access.pass_active(uid, "coze"), "证发下去了"
    assert work_access.blocked_reason(uid) == "work_quota", "**有证也不能绕过机时闸**"
    assert work_access.state(uid)["minutes_left"] == 0, "证不带机时"
    assert work_access.minute_packs_left(uid) == 0
    # 状态里不冒出第二个"能不能用"的判据
    st = work_access.state(uid)
    assert "pass_active" not in st and "next_price" not in st
    assert st["allowed"] == (st["minutes_left"] > 0)
    # 只对买的那一格有效
    assert not work_access.pass_active(uid, "dify")
    assert products.is_locked("coze") is ("coze" in products.locked_ids())


def test_pass_extends_from_the_existing_expiry(monkeypatch):
    """连买两张不该白白损失第一张的剩余时间。"""
    uid = _user("u_pass_ext")
    work_access.grant_pass(uid, "coze", 7)
    first = work_access.pass_expires(uid, "coze")
    work_access.grant_pass(uid, "coze", 7)
    assert work_access.pass_expires(uid, "coze") > first + 6 * 86400


def test_admins_are_never_walled_by_the_trial_gate():
    """**这条是用户报的毛病。** 9.9 的试用墙把管理员自己也拦在了外面。

    服务是他们在运营 —— 出事时第一个要能进去看的就是他们, 而且后台里那些"上锁"
    的格子本来就是他们配的。
    """
    from app import work_access

    uid = _user("u_admin_gate")
    assert not work_access.pass_active(uid, "coze"), "前提: 没买证"
    assert not work_access.can_open_locked({"id": uid}, "coze"), "普通人没证就该被拦"
    assert work_access.can_open_locked({"id": uid, "is_admin": True}, "coze"), "管理员被自己配的试用墙拦住了"


def test_admin_can_exempt_one_user_from_the_whole_wall():
    """管理员能指定哪个用户免墙 —— 发一张通配证, 与买来的证走同一条路。"""
    from app import work_access

    uid = _user("u_exempt")
    assert not work_access.can_open_locked({"id": uid}, "coze")
    assert not work_access.lock_exempt(uid)

    work_access.grant_pass(uid, work_access.PASS_ANY, 30, ref="admin:test")
    assert work_access.lock_exempt(uid)
    # 免的是**整面墙**, 不是某一格
    for pid in ("coze", "dify", "avatar", "随便一个还没上线的"):
        assert work_access.can_open_locked({"id": uid}, pid), f"{pid} 仍被拦"

    n = work_access.revoke_lock_exemption(uid)
    assert n == 1
    assert not work_access.lock_exempt(uid)
    assert not work_access.can_open_locked({"id": uid}, "coze"), "收回之后还免着"


def test_revoking_the_waiver_leaves_bought_passes_alone():
    """收回免墙不该顺手把人家花钱买的那一格也撤了。"""
    from app import work_access

    uid = _user("u_exempt_mix")
    work_access.grant_pass(uid, "coze", 7, price=990, currency="CNY", ref="order_x")
    work_access.grant_pass(uid, work_access.PASS_ANY, 30, ref="admin:test")

    work_access.revoke_lock_exemption(uid)
    assert work_access.pass_active(uid, "coze"), "把买来的证一起删了"
    assert not work_access.pass_active(uid, "dify")


def test_the_waiver_expires_like_any_other_pass():
    """给"先用一个月"的时候, 到期必须真的失效。"""
    from app import work_access

    uid = _user("u_exempt_exp")
    work_access.grant_pass(uid, work_access.PASS_ANY, 1, ref="admin:test")
    assert work_access.can_open_locked({"id": uid}, "coze")
    with db.tx() as c:  # 把到期时间拨到过去
        c.execute(
            "UPDATE work_passes SET expires=? WHERE user_id=? AND kind=?",
            (time.time() - 1, uid, f"{work_access.PASS_KIND}:{work_access.PASS_ANY}"),
        )
    assert not work_access.can_open_locked({"id": uid}, "coze"), "过期的免墙还在生效"


def test_a_waiver_does_not_hand_out_machine_time():
    """免墙只回答"这一格能不能开", 不发机时 —— 与买来的证一个规矩。"""
    from app import work_access

    uid = _user("u_exempt_meter")
    _burn(uid, 120)
    work_access.grant_pass(uid, work_access.PASS_ANY, 30, ref="admin:test")
    assert work_access.can_open_locked({"id": uid}, "coze")
    assert work_access.blocked_reason(uid) == "work_quota", "**免墙绕过了机时闸**"
    assert work_access.state(uid)["minutes_left"] == 0


def test_locked_products_come_from_config(monkeypatch):
    """锁哪几格是运营决定 —— 改 env 部署一次就生效, 不用改代码。"""
    from app import products

    monkeypatch.setattr(config, "WORK_LOCKED_PRODUCTS", "")
    assert products.locked_ids() == set() and not products.is_locked("coze")
    monkeypatch.setattr(config, "WORK_LOCKED_PRODUCTS", " coze , dify ")
    assert products.locked_ids() == {"coze", "dify"}
    assert products.is_locked("coze") and not products.is_locked("pi")


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


# ---- 云空间那页按使用时长排序 (老板 2026-09-06 定) --------------------------


def test_catalog_sorts_by_usage_and_sinks_the_offline_ones():
    """用得多的排前面; 没上线的一律沉底 —— 一张点不进去的卡排在第一屏, 比不排序更碍事。"""
    from app import apps_catalog

    ids = [a.id for a in apps_catalog.CATALOG]
    live = set(ids[:4])
    minutes = {ids[3]: 900, ids[1]: 100, ids[0]: 5}
    out = apps_catalog.entries_with_status(live, minutes)
    assert [a["id"] for a in out[:4]] == [ids[3], ids[1], ids[0], ids[2]], "按时长, 同为 0 的按目录原序"
    assert all(not a["live"] for a in out[4:]), "没上线的沉底"
    assert len(out) == len(ids), "一张卡都不能丢"


def test_catalog_keeps_hand_order_when_there_is_no_usage():
    """没有用量的新站看到的还是手工编排的那个顺序 —— 不能因为都是 0 就洗牌。"""
    from app import apps_catalog

    ids = [a.id for a in apps_catalog.CATALOG]
    assert [a["id"] for a in apps_catalog.entries_with_status(set(ids), {})] == ids
    assert [a["id"] for a in apps_catalog.entries_with_status(set(ids), None)] == ids


def test_minutes_by_product_ignores_credit_rows_and_caches():
    uid = _user("u_pop")
    now = time.time()
    with db.tx() as c:
        c.execute("DELETE FROM usage_log")
        for i in range(3):
            c.execute(
                "INSERT INTO usage_log (id,user_id,device_id,kind,model,credits,request_id,created) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"ul_p{i}", uid, "", work_access.MINUTE_KIND, "work:coze", 0, "", now),
            )
        # 积分行不是时长, 不能混进来
        c.execute(
            "INSERT INTO usage_log (id,user_id,device_id,kind,model,credits,request_id,created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("ul_llm", uid, "", "llm", "gpt-x", 9, "", now),
        )
    work_access._popularity = (0.0, {})
    got = work_access.minutes_by_product()
    assert got == {"coze": 3}, got
    # 缓存: 再插一行也不该立刻变
    with db.tx() as c:
        c.execute(
            "INSERT INTO usage_log (id,user_id,device_id,kind,model,credits,request_id,created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("ul_p9", uid, "", work_access.MINUTE_KIND, "work:coze", 0, "", now),
        )
    assert work_access.minutes_by_product() == {"coze": 3}, "五分钟内应走缓存"
