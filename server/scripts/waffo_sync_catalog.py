#!/usr/bin/env python3
"""Re-point the Waffo catalog at the current price table.

Waffo product prices are placeholders — checkout overrides them with
`priceSnapshot`, resolved server-side per order — so a stale catalog never
overcharges anyone. It does, however, show the wrong number in the merchant
dashboard and on the store page, and after two rounds of repricing every
product was advertising a price we had stopped charging months earlier. Nothing
had told us, because nothing was checking.

Run it after any price change:

    docker exec dhc-server python scripts/waffo_sync_catalog.py         # report
    docker exec dhc-server python scripts/waffo_sync_catalog.py --apply # fix

Only products the kv cache points at are touched: those are the ones checkout
can actually reach. Everything else in the store is a deactivated leftover and
is left alone.
"""
from __future__ import annotations

import asyncio
import sys

from app import db
from app.payments import base, waffo_provider as w

LIST = ("query($s:String!){ onetimeProducts(storeId:$s, limit:200)"
        "{ id name status prices { currency priceInfo { amount } } } }")


def cached_items() -> dict:
    """product id -> item id, from the kv cache checkout resolves through."""
    rows = db.query("SELECT k, v FROM kv WHERE k LIKE 'waffo_product:%'", ())
    return {r["v"]: r["k"].split(":", 1)[1] for r in rows}


async def main(apply: bool) -> int:
    store = await w.ensure_store_id()
    _st, data = await w._waffo_request("/v1/graphql", {"query": LIST, "variables": {"s": store}})
    products = ((data.get("data") or {}).get("onetimeProducts") or []) if isinstance(data, dict) else []
    items = cached_items()

    drift = 0
    for p in products:
        item = items.get(p["id"])
        if not item:
            continue
        try:
            info = base.resolve_item(item)
        except Exception:
            print(f"  ! {item}: no longer in the price table — product {p['id']} is orphaned")
            continue
        want = f"{info['amount_cents'] / 100:.2f}"
        cur = info["currency"]
        have = next((str((x.get("priceInfo") or {}).get("amount"))
                     for x in (p.get("prices") or []) if x.get("currency") == cur), None)
        if have == want and p["name"] == info["description"]:
            continue
        drift += 1
        print(f"  {item}: {have} {cur} -> {want} {cur}")
        if apply:
            st, d = await w._waffo_request("/v1/actions/onetime-product/update-product", {
                "id": p["id"],
                "name": info["description"],
                "description": info["description"],
                "prices": {cur: {"amount": want, "taxIncluded": True, "taxCategory": "saas"}},
            })
            if st >= 300:
                print(f"    FAILED {st} {d}")
                return 1

    print(f"{len(products)} products in store, {len(items)} reachable, {drift} out of date"
          + ("" if apply else " (run with --apply to fix)"))
    return 1 if (drift and not apply) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--apply" in sys.argv)))
