"""管理后台的按产品消耗 (/api/admin/usage)。

老板要的是"每个用户用哪个产品, 积分和时长消耗在哪"。两种资源的归属线索不同
(机时写在 model 里, 积分要经 device 找工作台), 而发放/退款/机时行都不是"消耗" ——
这些混进去数字就对不上, 而对不上的报表比没有报表更糟。
"""

import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="dhc-adm-")
os.environ.update(
    {
        "DHC_DEV": "1",
        "AUTH_SECRET": "test-secret",
        "DHC_DATA_DIR": _TMP,
        "DB_PATH": os.path.join(_TMP, "test.db"),
        "UPSTREAM_API_KEY": "sk-upstream-test",
    }
)

import pytest  # noqa: E402

from app import admin, db, work_access  # noqa: E402

db.ensure_schema()

NOW = time.time()
OLD = NOW - 40 * 86400  # 30 天窗口之外


def _log(uid, kind, *, model="", device="", credits=0, created=NOW):
    with db.tx() as c:
        c.execute(
            "INSERT INTO usage_log (id,user_id,device_id,kind,model,credits,request_id,created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                f"ul_{uid}_{kind}_{model}_{device}_{created}_{os.urandom(3).hex()}",
                uid,
                device,
                kind,
                model,
                credits,
                "",
                created,
            ),
        )


def _device(did, uid, workspace, platform="cloud"):
    with db.tx() as c:
        c.execute(
            "INSERT INTO devices (id,user_id,name,platform,workspace,token_hash,epoch,revoked,last_seen,created) "
            "VALUES (?,?,?,?,?,?,0,0,0,?)",
            (did, uid, did, platform, workspace, f"hash-{did}", NOW),
        )


@pytest.fixture(autouse=True)
def _seed():
    with db.tx() as c:
        for tbl in ("usage_log", "devices", "users"):
            c.execute(f"DELETE FROM {tbl}")
        for uid in ("u_a", "u_b"):
            c.execute(
                "INSERT INTO users (id,email,session_epoch,created) VALUES (?,?,0,0)", (uid, uid + "@t.local")
            )
    # u_a: pi 工作台 (dev1)、默认产品 dsh 的工作台 (dev2, 键里没有 ~)、桌面端 (dev3)
    _device("dev1", "u_a", "u_a~pi")
    _device("dev2", "u_a", "u_a")
    _device("dev3", "u_a", "", platform="desktop")
    _device("dev4", "u_b", "u_b~coze")
    # 机时: 回收器每分钟一行, 产品在 model 里
    for _ in range(3):
        _log("u_a", work_access.MINUTE_KIND, model="work:pi")
    for _ in range(2):
        _log("u_a", work_access.MINUTE_KIND, model="work:dsh")
    _log("u_b", work_access.MINUTE_KIND, model="work:coze")
    # 积分: 经设备归到产品
    _log("u_a", "llm", device="dev1", credits=10)
    _log("u_a", "search", device="dev1", credits=5)
    _log("u_a", "llm", device="dev2", credits=7)
    _log("u_a", "image", device="dev3", credits=4)
    _log("u_a", "video", device="", credits=20)  # 网页里直接发的: 没有设备
    _log("u_a", "llm", device="dev_gone", credits=6)  # 设备行已被删: 产品追不回
    _log("u_b", "llm", device="dev4", credits=9)
    # 不算消耗的行: 发放、退款、以及一条 30 天前的旧调用
    _log("u_a", "grant_admin", device="", credits=1000)
    _log("u_a", "refund", device="dev1", credits=-3)
    _log("u_a", "llm", device="dev1", credits=100, created=OLD)
    _log("u_a", work_access.MINUTE_KIND, model="work:pi", created=OLD)


def _by_id(d):
    return {p["id"]: p for p in d["products"]}


def test_one_users_consumption_is_split_by_product():
    d = admin.usage(user_id="u_a", days=30, _={})
    by = _by_id(d)
    assert by["pi"] == {"id": "pi", "name": "pi", "minutes": 3, "credits": 15, "calls": 2}
    assert by["dsh"]["minutes"] == 2 and by["dsh"]["credits"] == 7 and by["dsh"]["calls"] == 1
    # 不经工作台的调用 (桌面设备 + 网页无设备) 单独一栏, name 留空让前端翻译
    assert by["desktop"] == {"id": "desktop", "name": "", "minutes": 0, "credits": 24, "calls": 2}
    # 设备行没了的不能冒充桌面端 —— 单列"无法归属"
    assert by["unattributed"] == {"id": "unattributed", "name": "", "minutes": 0, "credits": 6, "calls": 1}
    assert "coze" not in by, "别人的消耗不能混进来"
    assert d["totals"] == {"minutes": 5, "credits": 52, "calls": 6}


def test_grants_refunds_and_minute_rows_are_not_consumption():
    d = admin.usage(user_id="u_a", days=30, _={})
    # 1000 的发放和 -3 的退款都没进积分; 机时行不带积分
    assert d["totals"]["credits"] == 52


def test_period_window_and_all_time():
    recent = admin.usage(user_id="u_a", days=30, _={})
    all_time = admin.usage(user_id="u_a", days=0, _={})
    assert _by_id(all_time)["pi"]["credits"] == 115 and _by_id(all_time)["pi"]["minutes"] == 4
    assert _by_id(recent)["pi"]["credits"] == 15


def test_site_wide_when_no_user_given():
    d = admin.usage(user_id="", days=30, _={})
    by = _by_id(d)
    assert by["coze"]["minutes"] == 1 and by["coze"]["credits"] == 9
    assert by["pi"]["credits"] == 15
    assert d["totals"]["credits"] == 61


def test_products_sorted_by_credits_and_non_product_buckets_last():
    d = admin.usage(user_id="u_a", days=30, _={})
    # desktop 24 积分比 pi 还多, 但它不是产品, 固定压在产品后面; 无法归属排最后
    assert [p["id"] for p in d["products"]] == ["pi", "dsh", "desktop", "unattributed"]


def test_days_is_clamped():
    assert admin.usage(user_id="u_a", days=-5, _={})["days"] == 0
    assert admin.usage(user_id="u_a", days=99999, _={})["days"] == 3650


def test_sql_sentinel_survives_the_postgres_placeholder_rewrite():
    """db 层给 Postgres 把 SQL 里所有 ? 改成 %s、% 改成 %% —— 字符串字面量里的也改。
    第一版哨兵是 '?', SQLite 下 753 个测试全绿, 线上一调就 "7 placeholders but 6
    parameters"。SQLite 测不出这个, 只能钉住哨兵本身。"""
    assert "?" not in admin._GONE and "%" not in admin._GONE
    # 真跑一遍经过占位符改写的 SQL: 参数个数必须和 ? 个数一致
    import re

    from app import db as _db

    seen: list[tuple[int, int]] = []
    orig = _db.query

    def spy(sql, params=()):
        seen.append((sql.count("?"), len(params)))
        return orig(sql, params)

    _db.query = spy
    try:
        admin.usage(user_id="u_a", days=30, _={})
    finally:
        _db.query = orig
    assert seen and all(q == n for q, n in seen), seen
    assert not re.search(r"'[^']*\?[^']*'", " ".join(str(x) for x in seen))


def test_per_user_listing_carries_each_users_breakdown():
    d = admin.usage_users(days=30, _={})
    assert [u["id"] for u in d["users"]] == ["u_a", "u_b"], "按积分降序"
    ua, ub = d["users"]
    assert ua["email"] == "u_a@t.local"
    assert (ua["minutes"], ua["credits"], ua["calls"]) == (5, 52, 6)
    assert [p["id"] for p in ua["products"]] == ["pi", "dsh", "desktop", "unattributed"]
    assert ua["products"][0]["credits"] == 15
    assert ub["products"] == [{"id": "coze", "name": "Coze Studio", "minutes": 1, "credits": 9, "calls": 1}]
    assert d["totals"] == {"minutes": 6, "credits": 61, "calls": 7}


def test_per_user_listing_respects_window():
    d = admin.usage_users(days=0, _={})
    ua = next(u for u in d["users"] if u["id"] == "u_a")
    assert ua["credits"] == 152 and ua["minutes"] == 6
    assert admin.usage_users(days=99999, _={})["days"] == 3650


# --- 免试用墙开关 (/api/admin/set-lock-exempt) -------------------------------
#
# 起因: 9.9 的试用墙把管理员自己也拦在了外面, 而且没有任何办法给个别用户放行。


def _person(uid, *, role="user"):
    with db.tx() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.execute("DELETE FROM work_passes WHERE user_id=?", (uid,))
        c.execute(
            "INSERT INTO users (id,email,role,session_epoch,created) VALUES (?,?,?,0,0)",
            (uid, uid + "@t.local", role),
        )
    return uid


def test_set_lock_exempt_grants_and_revokes():
    boss = _person("u_boss", role="admin")
    target = _person("u_target")

    admin.set_lock_exempt({"user_id": target, "exempt": True}, user={"id": boss})
    assert work_access.lock_exempt(target)
    assert work_access.can_open_locked({"id": target}, "coze")

    admin.set_lock_exempt({"user_id": target, "exempt": False}, user={"id": boss})
    assert not work_access.lock_exempt(target)


def test_set_lock_exempt_records_who_granted_it():
    """免墙是白送的资源, 得能查出是谁放的行。"""
    boss = _person("u_boss2", role="admin")
    target = _person("u_target2")
    admin.set_lock_exempt({"user_id": target, "exempt": True}, user={"id": boss})
    row = db.query_one(
        "SELECT ref, price FROM work_passes WHERE user_id=? AND kind=?",
        (target, f"{work_access.PASS_KIND}:{work_access.PASS_ANY}"),
    )
    assert row["ref"] == f"admin:{boss}", "查不出是谁发的"
    assert int(row["price"]) == 0, "白送的记成了收过钱"


def test_set_lock_exempt_refuses_on_an_admin_instead_of_lying():
    """管理员本来就免墙。默默"成功"会让后台显示"已收回"而人家照样进得去。"""
    from fastapi import HTTPException

    boss = _person("u_boss3", role="admin")
    other = _person("u_admin_target", role="admin")
    for exempt in (True, False):
        with pytest.raises(HTTPException) as e:
            admin.set_lock_exempt({"user_id": other, "exempt": exempt}, user={"id": boss})
        assert e.value.status_code == 400
        assert e.value.detail == "admin_always_exempt"


def test_set_lock_exempt_rejects_unknown_user():
    from fastapi import HTTPException

    boss = _person("u_boss4", role="admin")
    with pytest.raises(HTTPException) as e:
        admin.set_lock_exempt({"user_id": "u_nobody", "exempt": True}, user={"id": boss})
    assert e.value.status_code == 404


def test_user_list_shows_the_effective_waiver_not_the_stored_row():
    """按钮要照实际生效的权限画: 管理员没有那一行, 但照样免墙。"""
    boss = _person("u_boss5", role="admin")
    plain = _person("u_plain5")
    admin.set_lock_exempt({"user_id": plain, "exempt": True}, user={"id": boss})

    rows = {u["id"]: u for u in admin.list_users(q="u_boss5", _={"id": boss})["users"]}
    assert rows[boss]["lock_exempt"] is True
    assert rows[boss]["lock_exempt_from_role"] is True

    rows = {u["id"]: u for u in admin.list_users(q="u_plain5", _={"id": boss})["users"]}
    assert rows[plain]["lock_exempt"] is True
    assert rows[plain]["lock_exempt_from_role"] is False, "普通人被标成了靠身份免墙"
