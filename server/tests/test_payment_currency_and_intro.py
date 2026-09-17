"""2026-09-16 支付面两条守护测试。判据同样是: 把修复还原, 这里必须红。

1. 结算币种: 支付宝/微信只结人民币, 但结账币种是访客用 ?cur= 选的。
   原来两个 provider 都把订单的 amount_cents 直接当分提交 (微信还把 currency
   写死 CNY), 于是 GBP 单的 Max 档 ¥700 实收 ¥78; 反向日元档会收成 ¥15000。
2. 币种矫正: 修法不是拒绝而是按 provider 的结算币种建单 —— 站点默认报价是 USD,
   直接拒会把「美元报价 + 选支付宝」这条最常见的路径整条打断。

(首月价可重复用那条**没有修**: 见 test_payments.py 里刻意写下的
 "An unpaid checkout must not burn the offer" —— 那是有意设计, 堵它会让老实用户
 重开结账页就涨价。需要产品决策, 不是我能顺手改的。)
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-pay-")
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
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.payments import base  # noqa: E402
from tests._signup import signup_with_password  # noqa: E402


def _uid(email: str) -> str:
    c = TestClient(app)
    signup_with_password(c, email, "password123")
    return db.query_one("SELECT id FROM users WHERE email=?", (email,))["id"]


# --- 1) 结算币种 --------------------------------------------------------------


@pytest.mark.parametrize("provider", ["alipay", "wechat"])
@pytest.mark.parametrize("cur", ["GBP", "USD", "EUR", "JPY", "HKD"])
def test_cny_only_providers_refuse_foreign_currency_orders(provider, cur):
    """这两家只结人民币 —— 外币订单必须在收款前被拒, 而不是按分当人民币收走。"""
    with pytest.raises(HTTPException) as e:
        base.assert_settles(provider, {"currency": cur, "amount_cents": 7800})
    # 500 而不是 400: checkout 已经矫正过币种, 走到这儿说明不变量被破坏了,
    # 不是用户输入的问题。
    assert e.value.status_code == 500
    assert e.value.detail == "currency_not_settleable"


@pytest.mark.parametrize("provider", ["alipay", "wechat"])
def test_cny_orders_still_go_through(provider):
    """守卫不能把正常的人民币单也挡了 —— 那等于把收款关了。"""
    base.assert_settles(provider, {"currency": "CNY", "amount_cents": 7000})


def test_multi_currency_providers_are_unrestricted():
    """stripe / waffo 自己按订单币种下单, 不该被这张表限制住。"""
    for provider in ("stripe", "waffo"):
        for cur in ("CNY", "USD", "JPY"):
            base.assert_settles(provider, {"currency": cur, "amount_cents": 1000})


def test_settles_matches_the_table():
    assert base.settles("alipay", "CNY") and not base.settles("alipay", "GBP")
    assert base.settles("wechat", "CNY") and not base.settles("wechat", "JPY")
    assert base.settles("stripe", "GBP") and base.settles("waffo", "JPY")


# --- 2) 矫正而不是拒绝 --------------------------------------------------------


def test_foreign_quote_is_coerced_to_cny_for_cny_only_providers():
    """站点默认报价是 USD —— 直接拒掉外币会把最常见的「美元报价 + 选支付宝」
    整条打断。矫正成人民币, 金额取人民币价目表的正确价。"""
    for cur in ("USD", "GBP", "EUR", "JPY", "HKD", None):
        assert base.order_currency("alipay", cur) == "CNY"
        assert base.order_currency("wechat", cur) == "CNY"
    assert base.order_currency("alipay", "CNY") == "CNY"


def test_multi_currency_providers_keep_the_quoted_currency():
    for cur in ("USD", "CNY", "JPY"):
        assert base.order_currency("stripe", cur) == cur
        assert base.order_currency("waffo", cur) == cur


def test_coerced_order_carries_the_real_cny_price():
    """矫正后收到的必须是人民币价目表的价, 不是外币数字被当成分。"""
    cny = int(base.resolve_item("plan:plus:monthly", "CNY")["amount_cents"])
    gbp = int(base.resolve_item("plan:plus:monthly", "GBP")["amount_cents"])
    assert cny != gbp, "两张价目表一样的话这条用例证明不了什么"

    uid = _uid("coerce@test.local")
    cur = base.order_currency("alipay", "GBP")
    o = base.create_order(uid, "alipay", "plan:plus:monthly", cur)
    assert o["currency"] == "CNY"
    # 首月价可能生效, 所以比对的是"这个币种下该收的数", 不是标准价
    assert o["amount_cents"] == base.price_for(uid, base.resolve_item("plan:plus:monthly", "CNY"))
    assert o["amount_cents"] != gbp, "GBP 的数字被当成人民币分收走了"
