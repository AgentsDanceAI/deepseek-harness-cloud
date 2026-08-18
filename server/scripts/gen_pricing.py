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

Discounts are flat across tiers. They used to deepen as the tier rose, which
reads well on a page but priced the deepest discount exactly where the risk is:
at full utilisation both the first-month and the yearly price are below cost, and
the people who buy a year of the top tier are the ones most likely to burn their
whole allowance. Flat 30% yearly / 25% first month keeps the offer while bounding
that exposure.

    python3 server/scripts/gen_pricing.py          # rewrite all six tables
    python3 server/scripts/gen_pricing.py --check  # fail if they are stale
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config"

# Units of local currency per US dollar. Not live rates: prices are set, not
# converted on the fly, or every FX tick would reprice the site.
RATES = {"USD": 1.0, "CNY": 7.0, "EUR": 0.92, "GBP": 0.79, "HKD": 7.8, "JPY": 150.0}
# Quantisation step, in whole units of the currency.
STEP = {"JPY": 100}

# The master table, in whole US dollars. Aligned with a sibling production system
# (2026-08-19): same ladder, same credits-per-dollar, same discount depth.
#
# $20 / $50 / $100 rather than $10 / $20 / $100. The old top tier sat at the same
# price as Claude Max and ChatGPT Pro, which is a fight this product does not win;
# and $20 -> $100 left a fivefold gap with nothing in it, so anyone needing more
# than the middle tier's credits had to jump five times the price. $50 is a price
# point neither of those two occupies.
TIERS = [
    # id,   name,   $/mo, credits/mo, concurrency, minutes/mo, yearly off %, first-month off %
    ("plus", "Plus",   20,      2000,           2,      10800,           30,                25),
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
           "其余币种按对美元汇率折算后取整（日元取整到 100）。三档 $20 / $50 / $100，"
           "年付一律 7 折、首月一律 7.5 折（原为最深 7 折 / 6 折：满用时首月与年付价"
           "一律负毛利，而买年付的恰是最可能烧满额度的人）。积分包不再多送——"
           "100 积分/美元是公示承诺，包的价值在于不随月清零，同价的包也绝不能比"
           "同价套餐给得多，否则最高档被自己的加油包压制。")


def units(usd: float, cur: str) -> int:
    """Whole units of `cur` for a US-dollar amount, quantised for that currency."""
    step = STEP.get(cur, 1)
    return int(math.floor(usd * RATES[cur] / step + 0.5)) * step


def scale(local_units: int, factor: float, cur: str) -> int:
    """A discounted price, quantised the same way — derived from the LOCAL price
    rather than from USD, so the discount the page shows is honest about the
    number beside it."""
    step = STEP.get(cur, 1)
    return int(math.floor(local_units * factor / step + 0.5)) * step


def table(cur: str) -> dict:
    tiers: dict = {"free": {"name": "Free", "monthly_cents": 0, "monthly_credits": 0,
                            "concurrency": FREE["concurrency"],
                            "work_minutes": FREE["work_minutes"],
                            "signup_credits": FREE["signup_credits"]}}
    for tid, name, usd, credits, conc, minutes, yoff, ioff in TIERS:
        monthly = units(usd, cur)
        per_month = scale(monthly, (100 - yoff) / 100, cur)
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
            "monthly_intro_cents": scale(monthly, (100 - ioff) / 100, cur) * 100,
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
