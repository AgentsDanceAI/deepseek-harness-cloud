"""Mark withdrawn Waffo products so the store list stays readable.

    python server/scripts/waffo_retire_products.py          # report
    python server/scripts/waffo_retire_products.py --apply  # rename


Waffo has no delete or archive operation: update-status accepts only 'active'
or 'inactive', so inactive is the terminal state. Prefixing inactive products
keeps them distinguishable from the records used by checkout.

Orders do not pin the live name: every order references a productVersion that
snapshots the name at purchase time, so renaming the current record leaves
history intact. What the script will not touch is a product with an order that
is still LIVE — money that has not been refunded stays legible under the name
it was sold as.
"""

import asyncio
import json
import sys

from app import db
from app.payments import waffo_provider as w

TAG = "[RETIRED] "
LIST = (
    "query($s:String!){ onetimeProducts(storeId:$s, limit:200)"
    "{ id name description status prices { currency priceInfo { amount taxCategory } } } }"
)
ORDERS = (
    "query($s:String!){ onetimeOrders(storeId:$s, limit:200)"
    "{ status payments { refundStatus } onetimeProduct { id } } }"
)


async def main(apply: bool) -> int:
    store = await w.ensure_store_id()
    _st, d = await w._waffo_request("/v1/graphql", {"query": LIST, "variables": {"s": store}})
    if d.get("errors"):
        print(json.dumps(d["errors"])[:400])
        return 1
    products = d["data"]["onetimeProducts"]
    live = {r["v"] for r in db.query("SELECT v FROM kv WHERE k LIKE 'waffo_product:%'", ())}

    _st, od = await w._waffo_request("/v1/graphql", {"query": ORDERS, "variables": {"s": store}})
    live_orders = set()
    for o in (od.get("data") or {}).get("onetimeOrders") or []:
        if o.get("status") == "canceled":
            continue
        if all((pay.get("refundStatus") == "refunded") for pay in (o.get("payments") or [])):
            continue  # money went back; the product is free to be marked
        live_orders.add((o.get("onetimeProduct") or {}).get("id"))

    done = skipped = 0
    for p in products:
        if p["status"] == "active" or p["id"] in live:
            continue
        if p["name"].startswith(TAG):
            continue
        if p["id"] in live_orders:
            print(f"  keep as-is (unrefunded order against it): {p['name']}")
            skipped += 1
            continue
        # Waffo caps a product name at 64 characters, and the prefix pushes the
        # longer ones over — a rename that 400s leaves the store half-tagged.
        name = w.fit_name(TAG + p["name"])
        print(f"  {p['id']}  -> {name}")
        done += 1
        if apply:
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
                {
                    "id": p["id"],
                    "name": name,
                    "description": w.fit_name(TAG + (p.get("description") or p["name"])),
                    "prices": prices,
                },
            )
            if st >= 300:
                print(f"    FAILED {st} {json.dumps(r, ensure_ascii=False)[:200]}")
                return 1

    active = sum(1 for p in products if p["status"] == "active")
    print(
        f"\n{len(products)} products: {active} active, {done} renamed, {skipped} left alone (order history)"
        + ("" if apply else "  [dry run — pass --apply]")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--apply" in sys.argv)))
