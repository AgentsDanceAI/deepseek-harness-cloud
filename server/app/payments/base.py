"""Order kernel shared by every payment provider.

Three payment invariants:
  1. Amounts come from config/pricing.json only; the client picks an item id.
  2. A webhook is verified first, then the provider is queried for the order
     state before any fulfilment ("verify, then confirm").
  3. Idempotence: only the first pending->paid transition fulfils. Terminal
     states never regress; refunded is the only exit from paid.

Order ids: DHS... Stripe, DHA... Alipay, DHW... WeChat Pay.
Item encoding: "plan:<tier>:<cycle>" or "pack:<pack_id>".
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import HTTPException

from .. import config, credits, db, plans, teams, work_access

logger = logging.getLogger("dhc.pay")

ORDER_PREFIX = {"stripe": "DHS", "alipay": "DHA", "wechat": "DHW", "waffo": "DHF"}


def team_terms(cur: str | None = None) -> dict:
    """Seat terms from the price table the buyer was quoted in.

    Deliberately not a flat env default: a seat must never undercut the cheapest
    individual plan, and that threshold is a different number in ¥ than in $.
    An organisation buys governance — SSO, one invoice, member budgets, usage
    visibility — not a bulk discount on the personal product; sell it cheaper and
    buyers will simply expense personal plans instead.
    """
    p = plans.pricing(cur)
    t = dict(p.get("team") or {})
    t.setdefault("seat_cents", config.TEAM_SEAT_PRICE)
    t.setdefault("seat_credits", config.TEAM_SEAT_CREDITS)
    t.setdefault("seat_minutes", config.TEAM_SEAT_MINUTES)
    t.setdefault("min_seats", config.TEAM_SEAT_MIN)
    t.setdefault("volume_tiers", config.TEAM_SEAT_TIERS)
    return t


def seat_unit_price(seats: int, cur: str | None = None) -> int:
    """Per-seat price at this volume. Bands are (min_seats, percent_off), and the
    discount applies to the seat fee only."""
    terms = team_terms(cur)
    price = int(terms["seat_cents"])
    for min_seats, off in sorted(terms["volume_tiers"], key=lambda b: -b[0]):
        if seats >= int(min_seats):
            return max(1, round(price * (100 - int(off)) / 100))
    return price


def resolve_item(item: str, cur: str | None = None) -> dict:
    """Validates an item id against the price table. Returns {kind, amount_cents,
    currency, description, ...} — the ONLY place order amounts come from.

    `cur` is the currency the visitor was QUOTED in, resolved server-side from
    their request (never sent by the client — that would let a caller shop the
    six tables for the cheapest one). Passing None keeps the default table,
    which is what fulfilment uses: quotas are identical across currencies, so
    only the amount depends on this.
    """
    p = plans.pricing(cur)
    parts = item.split(":")
    if parts[0] == "pass" and len(parts) == 2:
        # 一格一张的通行证 (老板 2026-09-06: "9.9 才给开通试用")。价与天数在价目表的
        # passes 里, 一格一个键; 表里没有这一格就不是可买的东西。
        from .. import apps_catalog as _catalog
        from .. import products as _products

        pid = parts[1]
        pdef = (p.get("passes") or {}).get(pid)
        if not pdef or not _products.is_locked(pid):
            raise HTTPException(400, "unknown_item")
        # 名字从**目录**取: 数字人没有工作台产品, 但它是这十六格之一, 一样能上锁售卖。
        name = _catalog.name_of(pid) or pid
        return {
            "kind": "pass",
            "product_id": pid,
            "days": int(pdef["days"]),
            "amount_cents": int(pdef["cents"]),
            "currency": p["currency"],
            "description": f"{name} 试用 {int(pdef['days'])} 天",
        }
    if parts[0] == "plan" and len(parts) == 3:
        tier, cycle = parts[1], parts[2]
        tdef = p["tiers"].get(tier)
        if not tdef or tier == "free" or cycle not in ("monthly", "yearly"):
            raise HTTPException(400, "unknown_item")
        cents = tdef.get(f"{cycle}_cents")
        if not cents:
            raise HTTPException(400, "unknown_item")
        # `intro_cents` is the ADVERTISED first-month price, carried alongside the
        # standard one so price_for can decide between them from stored history.
        # It is never the amount on its own: an item nobody is eligible for still
        # has to resolve to something chargeable.
        intro = int(tdef.get("monthly_intro_cents") or 0) if cycle == "monthly" else 0
        return {
            "kind": "plan",
            "tier": tier,
            "cycle": cycle,
            "amount_cents": int(cents),
            "intro_cents": intro if 0 < intro < int(cents) else 0,
            "currency": p.get("currency", "CNY"),
            "description": f"AI Store {tdef['name']} ({'年付' if cycle == 'yearly' else '月付'})",
        }
    if parts[0] == "pack" and len(parts) == 2:
        pdef = p["packs"].get(parts[1])
        if not pdef:
            raise HTTPException(400, "unknown_item")
        return {
            "kind": "pack",
            "pack": parts[1],
            "credits": int(pdef["credits"]),
            "valid_days": int(pdef.get("valid_days", 365)),
            "amount_cents": int(pdef["cents"]),
            "currency": p.get("currency", "CNY"),
            "description": f"AI Store {pdef['name']}",
        }
    # Team seats: N seats for a month. The pool credits scale with the seat
    # count, so a bigger team gets a bigger shared balance, not just more logins.
    if parts[0] == "seats" and len(parts) == 2 and parts[1].isdigit():
        terms = team_terms(cur)
        n = max(int(terms["min_seats"]), min(int(parts[1]), 500))
        # Volume discount applies to the SEAT FEE only. The included credits and
        # minutes are real cost, so discounting them would be giving away
        # margin rather than rewarding commitment.
        unit = seat_unit_price(n, cur)
        return {
            "kind": "seats",
            "seats": n,
            "cycle": "monthly",
            "amount_cents": unit * n,
            "unit_cents": unit,
            "credits": int(terms["seat_credits"]) * n,
            "minutes": int(terms["seat_minutes"]) * n,
            "currency": p.get("currency", "CNY"),
            "description": f"AI Store 团队席位 × {n}（月付）",
        }
    raise HTTPException(400, "unknown_item")


def intro_eligible(user_id: str, tier: str | None = None) -> bool:
    """这个账号还能不能拿首月价。

    **按账号终身一次** (老板 2026-09-16 定, 原先是按档位各给一次)。改口径的原因:
    按档位算时, 买过 Plus 首月的人还能再拿 Pro、Max 的首月价, 一个账号能薅三次;
    而这是"首次购买"的招徕价, 招徕只发生一次。

    只有 'paid' 算消耗。退款把优惠还回去 —— 那个月我们最终没卖出去, 拿折扣扣着
    买家等于让退款变成第二次惩罚。'pending'/'expired' 是没付掉的结账, 不算消耗,
    否则关个标签页就烧掉自己的优惠 (tier 参数只为兼容旧调用, 现在不参与判定)。
    """
    row = db.query_one(
        "SELECT 1 FROM orders WHERE user_id=? AND item LIKE 'plan:%:monthly' AND status='paid' LIMIT 1",
        (user_id,),
    )
    return row is None


def _release_outstanding_intro(user_id: str, keep_order_id: str = "") -> None:
    """把这个账号名下**没付掉的**首月价订单改回标准价。

    只改判据不够: 优惠在付款时才消耗, 于是够资格时批量建 N 张待付单、每张都把首月价
    快照进去, 再逐张付 —— 每张都按折扣成交, "终身一次"形同虚设。
    这里让**同时只有一张**订单持有首月价: 新建一张就把旧的那些改回标准价。

    为什么是改价而不是作废: 作废掉的单如果用户正好去付了, 就是钱收了东西没给
    (mark_paid 刻意接受 expired 正是为了防这个)。改价后它仍然可付, 只是按标准价。
    """
    rows = db.query(
        "SELECT id, item, currency, amount_cents FROM orders "
        "WHERE user_id=? AND item LIKE 'plan:%:monthly' AND status IN ('pending','expired')",
        (user_id,),
    )
    for r in rows:
        if keep_order_id and r["id"] == keep_order_id:
            continue
        std = int(resolve_item(str(r["item"]), str(r["currency"]))["amount_cents"])
        if int(r["amount_cents"]) < std:
            db.query("UPDATE orders SET amount_cents=? WHERE id=?", (std, r["id"]))
            logger.info("首月价回收: 订单 %s %s -> %s (同时只留一张)", r["id"], r["amount_cents"], std)


def intro_eligibility(user_id: str, cur: str | None = None) -> dict[str, bool]:
    """Per-tier eligibility for the pricing page, so a repeat buyer is shown the
    price they will actually be charged. The page defaults to eligible (the
    common case, and what a logged-out visitor is quoted); this narrows it."""
    tiers = plans.pricing(cur).get("tiers") or {}
    # 契约仍是"每档一个布尔" (app.js 按 data-tier 取), 但口径改成账号终身一次后
    # 各档共用同一个答案 —— 前端不用改。
    ok = intro_eligible(user_id)
    return {t: ok for t in tiers if t != "free"}


def price_for(user_id: str, info: dict) -> int:
    """The amount this user actually owes. Only a monthly plan varies: the intro
    price is a first-purchase offer, so it is decided here from stored history —
    never from anything the client sent.

    This function is the reason the pricing page may advertise a first-month
    price at all. Without it the card struck through the standard price, showed
    the intro one, and the order still charged the standard one.
    """
    amount = int(info["amount_cents"])
    intro = int(info.get("intro_cents") or 0)
    if not intro or info.get("kind") != "plan" or info.get("cycle") != "monthly":
        return amount
    return intro if intro_eligible(user_id) else amount


# 每个 provider 能结算哪些币种。空集 = 不限 (自己按订单币种下单, 如 stripe/waffo)。
#
# 为什么要有这张表: 支付宝与微信**只结人民币**, 而它们原来把订单的 amount_cents
# 直接当分提交 (微信更是把 currency 写死 "CNY")。结账币种是访客用 ?cur= 选的,
# 于是选个 GBP 下单、按人民币分收钱 —— 拿真实价目表算, Max 档 ¥700 实收 ¥78
# (-88.9%); 反方向同样成立: 日元档 ¥700 会收成 ¥15000, 把客户多扣 20 倍。
# 键名与 ORDER_PREFIX 那套一致 —— 第一版写成 "wechatpay", 与 active_providers()
# 返回的 "wechat" 对不上, 结果是矫正不生效、反倒撞上兜底闸, 微信支付整条 500。
SETTLEMENT_CURRENCIES = {"alipay": {"CNY"}, "wechat": {"CNY"}}


def settles(provider: str, cur: str) -> bool:
    allowed = SETTLEMENT_CURRENCIES.get(provider)
    return not allowed or cur in allowed


def order_currency(provider: str, quoted: str | None) -> str | None:
    """这个 provider 该按哪个币种建单。

    **不是拒绝而是矫正**: 站点默认报价币种是 USD (currency.DEFAULT), 没带 ?cur=
    的访客拿到的就是美元报价; 直接拒掉外币会把「美元报价 + 选支付宝」这条最常见的
    路径整条打断 —— 那比漏洞本身更糟。矫正成人民币后, 金额取的是人民币价目表里的
    正确价, 套利和断流两头都不占。
    """
    allowed = SETTLEMENT_CURRENCIES.get(provider)
    if not allowed:
        return quoted
    if quoted in allowed:
        return quoted
    return sorted(allowed)[0]


def assert_settles(provider: str, order: dict) -> None:
    """收款前的最后一道闸。checkout 已经矫正过币种, 这里兜住绕过 checkout 直接
    走 provider 的任何路径 —— 金额是按订单币种的价目表算的, 币种一旦对不上,
    收到的就不是那个数 (微信把 currency 写死 CNY, 支付宝按分当元)。"""
    cur = str(order.get("currency") or "")
    if not settles(provider, cur):
        raise HTTPException(500, "currency_not_settleable")


def create_order(user_id: str, provider: str, item: str, cur: str | None = None) -> dict:
    info = resolve_item(item, cur)
    standard = int(info["amount_cents"])
    info["amount_cents"] = price_for(user_id, info)
    took_intro = info["amount_cents"] < standard
    if took_intro:
        # 这张拿了首月价 -> 之前没付掉的首月单一律回到标准价, 全账号同时只留一张。
        _release_outstanding_intro(user_id)
    order_id = ORDER_PREFIX[provider] + time.strftime("%y%m%d") + secrets.token_hex(5).upper()
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO orders (id, user_id, provider, item, amount_cents, currency, status, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (order_id, user_id, provider, item, info["amount_cents"], info["currency"], "pending", now),
        )
    return {"order_id": order_id, **info}


# A checkout the buyer walked away from stays 'pending' forever otherwise, and
# their order list fills up with rows that will never resolve. Well past any
# provider's session lifetime, so nothing still payable is swept.
PENDING_TTL_S = 24 * 3600


def expire_stale_pending(user_id: str) -> int:
    """Retire abandoned checkouts. Safe because mark_paid accepts 'expired'
    too — a webhook that arrives after the sweep still fulfils."""
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE orders SET status='expired' WHERE user_id=? AND status='pending' AND created < ?",
            (user_id, time.time() - PENDING_TTL_S),
        )
        return cur.rowcount


def mark_paid(order_id: str, provider_ref: str = "") -> bool:
    """First transition into paid returns True and the caller MUST fulfil
    exactly then. Repeat webhooks return False and change nothing.

    'expired' is accepted alongside 'pending' on purpose: expiry is our own
    housekeeping guess, and a provider confirming a payment always outranks it.
    Refusing here would mean money taken with nothing delivered.
    """
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE orders SET status='paid', provider_ref=?, paid_at=? "
            "WHERE id=? AND status IN ('pending','expired')",
            (provider_ref, time.time(), order_id),
        )
        return cur.rowcount > 0


def mark_refunded(order_id: str) -> bool:
    with db.tx() as conn:
        cur = conn.execute("UPDATE orders SET status='refunded' WHERE id=? AND status='paid'", (order_id,))
        return cur.rowcount > 0


def fulfil(order_id: str) -> None:
    """Deliver what the order bought. Call only after mark_paid returned True."""
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if order is None:
        raise ValueError(f"order {order_id} not found")
    # Priced in the order's OWN currency, not today's default table: the row
    # is the record of what was sold, and re-resolving it in another currency
    # would make a refund or an audit disagree with the receipt.
    info = resolve_item(order["item"], order["currency"])
    if info["kind"] == "plan":
        plans.apply_plan(order["user_id"], info["tier"], info["cycle"], order_id=order_id)
    elif info["kind"] == "pass":
        work_access.grant_pass(
            order["user_id"],
            info["product_id"],
            info["days"],
            price=int(order["amount_cents"]),
            currency=order["currency"],
            ref=order_id,
        )
    elif info["kind"] == "seats":
        # Seats are org-scoped: create the org on first purchase so the buyer
        # never lands on "you bought seats but have nowhere to put them".
        org = teams.org_of(order["user_id"])
        if org is None:
            org_id = teams.create_org(order["user_id"], "我的团队", seats=info["seats"])
        else:
            org_id = org["id"]
        teams.set_seats(org_id, info["seats"], time.time() + 31 * 86400)
        # Seats buy BOTH resources: tokens (credits) and machine time (minutes).
        teams.grant_pool(org_id, info["credits"], 31 * 86400, ref=order_id)
        teams.grant_minute_pool(org_id, info["minutes"], 31 * 86400, ref=order_id)
        # Seed per-member ceilings so a fresh org is protected by default; the
        # owner can raise, lower, or clear them.
        terms = team_terms(order["currency"])
        teams.set_default_caps(
            org_id,
            credit_cap=int(int(terms["seat_credits"]) * config.TEAM_DEFAULT_CREDIT_CAP_X),
            minute_cap=int(int(terms["seat_minutes"]) * config.TEAM_DEFAULT_MINUTE_CAP_X),
        )
    else:
        credits.grant(
            order["user_id"], info["credits"], info["valid_days"] * 86400, kind="grant_topup", ref=order_id
        )


# 额度类凭据 (积分券 / 机时券) 两张表形状相同, 都按 ref 记着是哪一单发的。
_GRANT_TABLES = {"credit_grants": "积分", "minute_grants": "机时"}


def _zero_grants(table: str, order_id: str) -> tuple[int, int]:
    """把这一单发出的额度余量清零。返回 (收回, 已花掉)。"""
    if table not in _GRANT_TABLES:  # 表名只能来自上面那张白名单
        raise ValueError(table)
    rows = db.query(f"SELECT amount, remaining FROM {table} WHERE ref=?", (order_id,))
    if not rows:
        return 0, 0
    took = sum(int(r["remaining"]) for r in rows)
    spent = sum(int(r["amount"]) - int(r["remaining"]) for r in rows)
    with db.tx() as conn:
        conn.execute(f"UPDATE {table} SET remaining=0 WHERE ref=?", (order_id,))
    return took, spent


def revoke(order_id: str) -> dict:
    """`fulfil` 的逆操作 —— 退款时把发出去的东西收回来。

    在此之前退款只翻订单状态, 发出去的通行证/订阅/积分/席位**一样都不收**
    (2026-09-10 用真钱走完一轮才发现: 钱退了, 7 天通行证还在)。

    口径是老板定的**只收回没花掉的**: 凭据类 (通行证) 直接撤; 额度类 (积分、
    机时) 只把该订单那笔的余量清零, 已经花掉的部分不追、**不让余额变负** ——
    一个正常退款的人不该被锁到必须先充值才能用。缺口记进日志等人看。

    只有 mark_refunded 那次真转移才会调到这里, 所以天然只跑一次。
    """
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if order is None:
        raise ValueError(f"order {order_id} not found")
    info = resolve_item(order["item"], order["currency"])
    out: dict = {"order": order_id, "kind": info["kind"]}

    for table, label in _GRANT_TABLES.items():
        took, spent = _zero_grants(table, order_id)
        if took or spent:
            out[f"{label}收回"] = took
            if spent:
                out[f"{label}已花掉"] = spent

    if info["kind"] == "pass":
        with db.tx() as conn:
            out["通行证撤销"] = conn.execute("DELETE FROM work_passes WHERE ref=?", (order_id,)).rowcount
    elif info["kind"] == "plan":
        # 订阅只有一行 expires 被往后推过, 没有按 ref 标记的凭据可撤 —— 只能把
        # 这一单加的天数减回去。注意 apply_plan 的升级分支是从"现在"重新起算的,
        # 所以升级后再退款会连原套餐的剩余时间一起减掉。退款路径上宁可多收一点,
        # 也别让人白拿; 真出现了看日志人工补。
        days = 366 if info["cycle"] == "yearly" else 31
        with db.tx() as conn:
            row = conn.execute(
                "SELECT expires FROM subscriptions WHERE user_id=?", (order["user_id"],)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE subscriptions SET expires=?, updated=? WHERE user_id=?",
                    (float(row["expires"]) - days * 86400, time.time(), order["user_id"]),
                )
                out["订阅回退天数"] = days
    elif info["kind"] == "seats":
        # 席位数是**绝对值**写进 orgs 的, 没人记过改之前是几个 —— 按 ref 收不回来。
        # 上面已经把团队的积分池和机时池清了, 席位数只能留给人工。
        out["席位待人工处理"] = info["seats"]

    logger.warning("[pay] 退款回收 %s", out)
    return out


def get_order(order_id: str, user_id: str | None = None):
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if order is None or (user_id is not None and order["user_id"] != user_id):
        return None
    return dict(order)
