"""Shape of the six currency price tables.

These are money, and they are generated: the failure mode is not a crashing
test but a page that advertises one number and charges another. Two such bugs
already shipped — a yearly price set to ten months while the badge claimed 20%
off, and a per-month figure rounded independently of the yearly total it was
supposed to be a twelfth of. Both are pinned here.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parent.parent / "config"
TABLES = sorted(CONFIG.glob("pricing.*.json"))
PAID = ("plus", "pro", "max")


def table(path: Path) -> dict:
    return json.loads(path.read_text())


def test_every_supported_currency_has_a_table():
    sys.path.insert(0, str(CONFIG.parent))
    from app import currency

    have = {p.name for p in TABLES}
    assert have == {currency.price_file(c) for c in currency.SUPPORTED}


@pytest.mark.parametrize("path", TABLES, ids=lambda p: p.stem)
def test_yearly_is_twelve_months_at_a_discount(path):
    for tier in PAID:
        t = table(path)["tiers"][tier]
        monthly, yearly = t["monthly_cents"], t["yearly_cents"]
        # The struck-through list price is a full twelve months, not ten.
        assert t["yearly_list_cents"] == monthly * 12
        # The per-month figure the card shows is exactly a twelfth of what is
        # charged, so the "save X%" badge computed from it cannot lie.
        assert t["yearly_per_month_cents"] * 12 == yearly
        assert 0 < yearly < t["yearly_list_cents"]
        # Paying yearly must beat paying monthly, in every currency, at every
        # tier — rounding to whole units must never invert that.
        assert t["yearly_per_month_cents"] < monthly


@pytest.mark.parametrize("path", TABLES, ids=lambda p: p.stem)
def test_intro_price_is_below_standard(path):
    for tier in PAID:
        t = table(path)["tiers"][tier]
        assert 0 < t["monthly_intro_cents"] < t["monthly_cents"]


@pytest.mark.parametrize("path", TABLES, ids=lambda p: p.stem)
def test_pack_credits_are_base_plus_bonus(path):
    for pid, p in table(path)["packs"].items():
        base, bonus = p["base_credits"], p["bonus_pct"]
        # `credits` is the only field the charge path grants; the other two
        # exist so the page can show the ladder. They have to agree.
        assert p["credits"] == base + base * bonus // 100, pid
        assert p["cents"] > 0 and p["valid_days"] > 0


@pytest.mark.parametrize("path", TABLES, ids=lambda p: p.stem)
def test_prices_are_whole_units(path):
    """The page renders prices with integer division by 100, so a price with a
    fractional unit would silently display as less than it charges."""
    t = table(path)
    amounts = [v for tier in t["tiers"].values() for k, v in tier.items() if k.endswith("_cents")]
    amounts += [p["cents"] for p in t["packs"].values()] + [t["team"]["seat_cents"]]
    assert all(a % 100 == 0 for a in amounts)


def test_tables_match_the_generator():
    """Six hand-edited tables is six chances to ship a wrong price."""
    gen = CONFIG.parent / "scripts" / "gen_pricing.py"
    r = subprocess.run([sys.executable, str(gen), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, f"price tables are stale, re-run gen_pricing.py:\n{r.stderr}"
