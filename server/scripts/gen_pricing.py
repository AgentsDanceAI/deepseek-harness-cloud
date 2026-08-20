#!/usr/bin/env python3
"""Generate config/pricing.<currency>.json for every supported currency.

USD is the master table; every other currency is derived from it. Six price
files kept in sync by hand is six chances to ship a wrong price, so nothing
here should ever be edited in the JSON — edit this file and re-run it.

Three rules the tables have to obey:

* **Whole units only.** The page renders prices with integer division by 100,
  so a price of €7.20 would display as "€7". Every figure is quantised to a
  whole unit of its currency (and to the nearest ¥100 for JPY — a ¥3,750 price
  reads as a rounding accident rather than a price).

* **Yearly is 12 months at a discount.** The list price struck through on the
  page is twelve times the monthly price; the discount is applied to that. The
  stored per-month figure is exact — `yearly_per_month_cents * 12 ==
  yearly_cents` always — so the "save X%" badge the template computes from
  those two numbers cannot disagree with what the customer is charged. The
  previous tables set yearly to ten months' price and rounded the per-month
  figure separately, which is how the page came to advertise 20% off a 17%
  discount.

* **Packs are a base amount plus a bonus.** `base_credits` is what the money
  buys at the standard rate of 100 credits per US dollar; `bonus_pct` is the
  volume kicker. `credits` — base plus bonus — is the only field the charge
  path reads, so the ladder can be shown on the page without the risk of
  granting a different number than the one advertised.

Discounts are flat across tiers rather than deepening as the tier rises. That is
a commercial choice, not a margin guard: the multiplier system prices every model
at 2.5x its cost (multiplier = ceil2(cost / $4.00), sell = multiplier x $10/M), so
gross margin is 60% at list and no discount on this table comes close to cost.

The nominal percentages here are what the DOLLAR card rounds to; every other
currency derives from the dollar card's actual ratio, not from these numbers.
See effective().

    python3 server/scripts/gen_pricing.py          # rewrite all six tables
    python3 server/scripts/gen_pricing.py --check  # fail if they are stale
"""
from __future__ import annotations

import json
import math
from fractions import Fraction
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config"

# Units of local currency per US dollar. Not live rates: prices are set, not
# converted on the fly, or every FX tick would reprice the site.
# 与另一套自有生产系统定价表的 currencies 同一组数字 —— 两个产品
# 同一张价目表, 汇率分家就等于价目表分家。EUR 0.90 / GBP 0.78 (而不是 0.92 / 0.79)
# 是刻意的: 它们让 $10/$50/$100 落成 €9/€45/€90 和 £8/£39/£78, 是整倍数阶梯;
# 0.92/0.79 落成 €9/€46/€92 和 £8/£40/£79, 看上去像换算残留而不是定出来的价。
RATES = {"USD": 1.0, "CNY": 7.0, "EUR": 0.90, "GBP": 0.78, "HKD": 7.8, "JPY": 150.0}
# Quantisation step, in whole units of the currency.
STEP = {"JPY": 100}

# The master table, in whole US dollars. Aligned with a sibling production system
# (2026-08-19): same ladder, same credits-per-dollar, same discount depth.
#
# $10 / $50 / $100. The old top tier sat at $200, the same price as Claude Max and
# ChatGPT Pro, which is a fight this product does not win. $50 is a price point
# neither of those two occupies, and it fills what used to be a bare fivefold gap
# between the entry tier and the top one. The entry tier stays at $10 to sit under
# every first-party subscription on the market.
# 机时额度是在"工作台跑在自己机器上"的年代定的 —— 那时多跑一小时的边际成本≈0,
# 所以送得很慷慨。切到 ECI 之后每一分钟都是真钱, 而这几个数没跟着改。
# 按 1 vCPU / 2 GiB 经济型算 (实测 0.5vCPU/1GiB = ¥0.0707/h, 线性推得
# ¥0.1414/h), 在**积分也用满**的前提下, 各档的盈亏平衡机时:
#     Plus 月付 272h / 首月 178h / 年付 131h
#     Pro       656h 起
#     Max      1312h 起
# 原来 Plus 含 180h —— 月付尚有余量, 但首月与年付的含量已经**越过平衡点**,
# 重度用户是净亏。Pro/Max 余量以千小时计, 不受影响。
# 所以只降 Plus: 180h -> 100h (每天 3.3 小时, 对 $10 档合理), 年付重新有
# 31 小时余量。超出的人走加油包。
TIERS = [
    # id,   name,   $/mo, credits/mo, concurrency, minutes/mo, yearly off %, first-month off %
    ("plus", "Plus",   10,      1000,           2,       6000,           30,                25),
    ("pro",  "Pro",    50,      5000,           5,      21600,           30,                25),
    ("max",  "Max",   100,     10000,          10,      32400,           30,                25),
]
FREE = {"work_minutes": 180, "signup_credits": 500, "concurrency": 1}

# id,        $,  base credits, bonus %
#
# No volume bonus any more. "100 credits per US dollar" is a promise made on the
# page, and a bonus quietly breaks it in the customer's favour today and in ours
# the moment someone tunes the ladder. What a pack sells instead is that it does
# NOT expire monthly.
#
# Dropping the bonus also removes an inversion that made the top tier unsellable:
# pack10000 used to grant 11,000 credits for $100 while a $100 Max subscription
# granted 10,000 — the pack strictly dominated the plan it was meant to top up.
# A pack must never grant more credits than the same-priced plan; the tests pin it.
PACKS = [
    ("pack1000",    10,     1000,   0),
    ("pack5000",    50,     5000,   0),
    ("pack10000",  100,    10000,   0),
]

TEAM = {"seat_usd": 25, "min_seats": 3, "seat_credits": 2500, "seat_minutes": 1200,
        "volume_tiers": [[10, 10], [25, 15], [50, 20]]}

COMMENT = ("由 server/scripts/gen_pricing.py 生成，请勿手改。基准 1 USD = 7 CNY = 100 积分；"
           "其余币种按对美元汇率折算后取整（日元取整到 100）。三档 $10 / $50 / $100，"
           "年付一律 7 折、首月一律 7.5 折（名义值；美元档要取整到整元，$10 的 7.5 折是 "
           "$7.5 → $8，实际 8 折。其余币种一律按**美元卡上的实际比例**折算，不重新套名义值，"
           "否则同一个促销在不同币种上力度不同）。积分包不再多送——"
           "100 积分/美元是公示承诺，包的价值在于不随月清零，同价的包也绝不能比"
           "同价套餐给得多，否则最高档被自己的加油包压制。")


def _quantise(value: Fraction, cur: str) -> int:
    """Round `value` to a whole step of `cur`, half up, in EXACT arithmetic.

    Exact because the halves are real. €45 at 30% off is 31.5, which must round
    to 32 — but 0.7 has no IEEE754 representation, so `45 * 0.7` evaluates to
    31.499999999999996 and floor(+0.5) hands back 31. That is one euro off, on a
    published price, from nothing but the order of two operations. Fractions
    built from the decimal STRINGS keep the arithmetic exact end to end."""
    step = STEP.get(cur, 1)
    return math.floor(value / step + Fraction(1, 2)) * step


def units(usd: float, cur: str) -> int:
    """Whole units of `cur` for a US-dollar amount, quantised for that currency."""
    return _quantise(Fraction(str(usd)) * Fraction(str(RATES[cur])), cur)


def scale(local_units: int, factor: Fraction, cur: str) -> int:
    """A discounted price, quantised the same way — derived from the LOCAL price
    rather than from USD, so the discount the page shows is honest about the
    number beside it. `factor` is a Fraction, never a float — see _quantise."""
    return _quantise(Fraction(local_units) * factor, cur)


def effective(usd: float, off: int) -> Fraction:
    """The discount that ACTUALLY ends up on the dollar card, as a ratio.

    Not the nominal one. USD prices are whole dollars too, so a nominal 25% off
    $10 is $7.50 and ships as $8 — a real discount of 20%, and $8 is the number
    the customer sees. Every other currency has to be derived from THAT ratio,
    not from the nominal 0.75, or the same promotion advertises a different depth
    depending on which currency you are quoted in: the yuan card said "省 24%"
    beside ¥53 while the dollar card said "20% Off" beside $8, and nobody decided
    to give yuan buyers four extra points — it fell out of the rounding.

    Max is unaffected either way ($100 × 0.75 = $75 exactly); this only moves the
    tiers whose discounted dollar price needed rounding."""
    usd_monthly = units(usd, "USD")
    return Fraction(scale(usd_monthly, Fraction(100 - off, 100), "USD"), usd_monthly)


def table(cur: str) -> dict:
    tiers: dict = {"free": {"name": "Free", "monthly_cents": 0, "monthly_credits": 0,
                            "concurrency": FREE["concurrency"],
                            "work_minutes": FREE["work_minutes"],
                            "signup_credits": FREE["signup_credits"]}}
    for tid, name, usd, credits, conc, minutes, yoff, ioff in TIERS:
        monthly = units(usd, cur)
        # 折扣比例取**美元卡上的实际比例**, 不是名义百分比 —— 见 effective()
        per_month = scale(monthly, effective(usd, yoff), cur)
        tiers[tid] = {
            "name": name,
            "monthly_cents": monthly * 100,
            "monthly_credits": credits,
            "concurrency": conc,
            "work_minutes": minutes,
            # List price is twelve months; the yearly total is exactly twelve
            # times the discounted per-month figure, never rounded apart from it.
            "yearly_list_cents": monthly * 12 * 100,
            "yearly_cents": per_month * 12 * 100,
            "yearly_per_month_cents": per_month * 100,
            "monthly_intro_cents": scale(monthly, effective(usd, ioff), cur) * 100,
        }

    packs = {}
    for pid, usd, base, bonus in PACKS:
        # `name` reaches the customer: it is the Waffo product title and the line
        # on the receipt, so it states the amount actually granted rather than
        # the base — a receipt for "10,000 credits" against an 11,000-credit
        # grant is the kind of mismatch that turns into a support ticket.
        granted = base + base * bonus // 100
        packs[pid] = {
            "name": f"{granted:,} credits" if not bonus
                    else f"{granted:,} credits ({base:,} + {bonus}% bonus)",
            "cents": units(usd, cur) * 100,
            "base_credits": base,
            "bonus_pct": bonus,
            "credits": granted,
            "valid_days": 365,
            "name_key": f"pricing.pack.{pid}",
        }

    return {
        "_comment": COMMENT,
        "currency": cur,
        "tiers": tiers,
        "packs": packs,
        "team": {
            "seat_cents": units(TEAM["seat_usd"], cur) * 100,
            "min_seats": TEAM["min_seats"],
            "seat_credits": TEAM["seat_credits"],
            "seat_minutes": TEAM["seat_minutes"],
            "volume_tiers": TEAM["volume_tiers"],
        },
    }


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    for cur in RATES:
        path = CONFIG / f"pricing.{cur.lower()}.json"
        text = json.dumps(table(cur), ensure_ascii=False, indent=2) + "\n"
        if check:
            if not path.is_file() or path.read_text() != text:
                stale.append(path.name)
            continue
        path.write_text(text)
        print(f"wrote {path.name}")
    if check and stale:
        print("stale: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
