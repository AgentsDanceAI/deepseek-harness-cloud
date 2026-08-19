"""Shape of the six currency price tables.

These are money, and they are generated: the failure mode is not a crashing
test but a page that advertises one number and charges another. Two such bugs
already shipped — a yearly price set to ten months while the badge claimed 20%
off, and a per-month figure rounded independently of the yearly total it was
supposed to be a twelfth of. Both are pinned here.
"""
from __future__ import annotations

import json
import math
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
def test_discounts_derive_from_the_dollar_card_not_the_nominal_percent(path):
    """每个币种的首月价/年付折月价 = 本币月价 × **美元卡上的实际折扣比例**。

    美元档也要取整到整元, 所以名义 7.5 折在 $10 上落成 $8 = 实际 8 折。若各币种各自
    重新套名义 0.75, 同一个促销在不同币种上的力度就不一样 —— 人民币卡曾写「省 24%」
    配 ¥53, 而美元卡写「20% Off」配 $8, 没有人决定过要多让人民币买家 4 个点, 那是
    取整漏出来的。这条测试钉的是方法, 不是某个数字: 换回名义折扣, 表就会变, 它就会红。

    注意残留漂移是**物理下限**不是 bug: £8 这种小额下一整英镑就占 12.5%, GBP Plus
    的角标必然和美元卡差几个点。能保证的是"用同一个比例算", 不是"角标处处相等"。
    """
    usd = table(CONFIG / "pricing.usd.json")["tiers"]
    t = table(path)
    step = 100 if t["currency"] == "JPY" else 1          # 与 gen_pricing.STEP 同源
    for tier in PAID:
        u, loc = usd[tier], t["tiers"][tier]
        monthly = loc["monthly_cents"] // 100
        for field in ("monthly_intro_cents", "yearly_per_month_cents"):
            ratio = u[field] / u["monthly_cents"]         # 美元卡上的实际比例
            want = math.floor(monthly * ratio / step + 0.5) * step
            assert loc[field] // 100 == want, (
                f"{t['currency']} {tier} {field}: 表里是 {loc[field] // 100}, "
                f"按美元卡比例 {ratio:.4f} 应为 {want} —— 折扣比例是不是又按名义值各算各的了?")


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
def test_a_pack_never_beats_the_plan_it_costs_the_same_as(path):
    """A $100 pack that granted 11,000 credits sat beside a $100 Max subscription
    granting 10,000 — the top-up strictly dominated the top plan, which is a good
    way to make the top plan unsellable. Packs sell "does not expire monthly",
    never "more credits for the same money"."""
    t = table(path)
    for pid, pack in t["packs"].items():
        for tier, tdef in t["tiers"].items():
            if tdef.get("monthly_cents") == pack["cents"]:
                assert pack["credits"] <= tdef["monthly_credits"], f"{pid} vs {tier}"


def _rates(t: dict) -> dict[str, float]:
    out = {tier: t["tiers"][tier]["monthly_credits"] / t["tiers"][tier]["monthly_cents"]
           for tier in PAID}
    out.update({pid: p["credits"] / p["cents"] for pid, p in t["packs"].items()})
    return out


def test_usd_buys_credits_at_exactly_one_rate():
    """One promise, one number: in the currency the table is authored in, every paid
    tier and every pack buys credits at 100 per dollar. A tier that quietly gives a
    better rate is a discount nobody decided on — it moves the gross margin without
    moving a price."""
    rates = _rates(table(CONFIG / "pricing.usd.json"))
    assert set(round(r, 9) for r in rates.values()) == {1.0}, rates   # 100 积分 / 100 分


@pytest.mark.parametrize("path", TABLES, ids=lambda p: p.stem)
def test_other_currencies_stay_within_rounding_of_that_rate(path):
    """Elsewhere the rate cannot be exact: prices are quantised to whole units of
    the currency while the credit counts are fixed, so a €18.40 price shown as €18
    buys marginally more per euro. That is rounding, and it has to stay rounding —
    a spread bigger than this means someone changed a price without its credits."""
    rates = list(_rates(table(path)).values())
    assert max(rates) / min(rates) <= 1.05, dict(_rates(table(path)))


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
