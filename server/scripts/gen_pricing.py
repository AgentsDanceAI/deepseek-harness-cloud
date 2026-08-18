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

Discounts are per tier and deliberately deepen as the tier rises: the bigger
commitment earns the better rate.

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

# The master table, in whole US dollars.
TIERS = [
    # id,   name,   $/mo, credits/mo, concurrency, minutes/mo, yearly off %, first-month off %
    ("plus", "Plus",   10,      1000,           2,      10800,           20,                20),
    ("pro",  "Pro",    20,      2000,           5,      21600,           25,                30),
    ("max",  "Max",   100,     10000,          10,      32400,           30,                40),
]
FREE = {"work_minutes": 180, "signup_credits": 500, "concurrency": 1}

# id,        $,  base credits, bonus %
PACKS = [
    ("pack1000",     10,      1000,   0),
    ("pack10000",   100,     10000,  10),
    ("pack100000", 1000,    100000,  25),
]

TEAM = {"seat_usd": 25, "min_seats": 3, "seat_credits": 2500, "seat_minutes": 1200,
        "volume_tiers": [[10, 10], [25, 15], [50, 20]]}

COMMENT = ("由 server/scripts/gen_pricing.py 生成，请勿手改。基准 1 USD = 7 CNY = 100 积分；"
           "其余币种按对美元汇率折算后取整（日元取整到 100）。年付＝12 个月原价打折，"
           "Plus 8 折 / Pro 7.5 折 / Max 7 折；首月特价 Plus 8 折 / Pro 7 折 / Max 6 折。"
           "积分包＝基准积分＋多送，多送 0% / 10% / 25%。")


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
