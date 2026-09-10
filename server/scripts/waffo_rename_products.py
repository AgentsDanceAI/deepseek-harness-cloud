"""Re-sync the live Waffo product names with our catalog text.

    python server/scripts/waffo_rename_products.py          # report
    python server/scripts/waffo_rename_products.py --apply  # rename

The name on a Waffo product is what the buyer reads on the hosted checkout
page, and it is frozen at create-product time. So a rename in the price table
or a brand change does NOT reach the checkout page on its own — this script is
how the two are brought back together.

Why it matters beyond cosmetics: ensure_product_id falls back to "reuse an
existing product with the same NAME" whenever the kv id cache misses. Once the
catalog text and the live product name disagree, that fallback stops matching
and quietly creates a duplicate product in the production catalog. Renaming
here keeps the fallback honest.

Only products the kv cache actually points at are touched — the ones checkout
uses. Retired records keep the name they were sold under (see
waffo_retire_products.py, which owns that half).

Safe against order history: an order references a productVersion that snapshots
the name at purchase time, so past receipts keep reading what the buyer agreed
to pay for.
"""

import asyncio
import json
import sys

from app import db
from app.payments import base
from app.payments import waffo_provider as w

LIST = (
    "query($s:String!){ onetimeProducts(storeId:$s, limit:200)"
    "{ id name description status prices { currency priceInfo { amount taxCategory } } } }"
)


async def main(apply: bool) -> int:
    store = await w.ensure_store_id()
    _st, d = await w._waffo_request("/v1/graphql", {"query": LIST, "variables": {"s": store}})
    if d.get("errors"):
        print(json.dumps(d["errors"])[:400])
        return 1
    by_id = {p["id"]: p for p in d["data"]["onetimeProducts"]}

    rows = db.query("SELECT k, v FROM kv WHERE k LIKE 'waffo_product:%' ORDER BY k", ())
    done = 0
    for row in rows:
        item, pid = row["k"].split(":", 1)[1], row["v"]
        p = by_id.get(pid)
        if p is None:
            print(f"  !! {item}: cached id {pid} is not in the store any more")
            continue
        want = base.resolve_item(item)["description"]
        if p["name"] == w.fit_name(want):
            continue
        print(f"  {item}\n      {p['name']!r}\n   -> {w.fit_name(want)!r}")
        done += 1
        if not apply:
            continue
        # prices must be echoed back in full: update-product replaces the price
        # map, and a currency that drops out makes create-session fail for
        # exactly the visitors who were quoted in it.
        prices = {
            x["currency"]: {
                "amount": (x.get("priceInfo") or {}).get("amount"),
                "taxIncluded": True,
                "taxCategory": (x.get("priceInfo") or {}).get("taxCategory") or "saas",
            }
            for x in (p.get("prices") or [])
        }
        st, r = await w._waffo_request(
            "/v1/actions/onetime-product/update-product",
            {"id": pid, "name": w.fit_name(want), "description": want, "prices": prices},
        )
        if st >= 300:
            print(f"    FAILED {st} {json.dumps(r, ensure_ascii=False)[:200]}")
            return 1

    print(f"\n{len(rows)} products in use: {done} renamed" + ("" if apply else "  [dry run — pass --apply]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--apply" in sys.argv)))
