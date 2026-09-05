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
