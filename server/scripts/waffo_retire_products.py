"""Mark withdrawn Waffo products so the store list stays readable.

    docker exec dhc-server python scripts/waffo_retire_products.py          # report
    docker exec dhc-server python scripts/waffo_retire_products.py --apply  # rename


Waffo has no delete and no archive: update-status accepts only 'active' or
'inactive', so inactive IS the terminal state. What is left to fix is that the
25 dead entries are named IDENTICALLY to the 10 live ones — three products
called "deepseek-harness-cloud Plus (月付)" and no way to tell from the name
which one checkout uses. Prefixing the dead ones makes the list scannable.

Never touched: any product with orders against it. The 7-day pass carries the
one real completed payment, and its name is what that receipt refers to.
"""
import asyncio, json, sys

from app import db
from app.payments import waffo_provider as w

TAG = "[RETIRED] "
LIST = ("query($s:String!){ onetimeProducts(storeId:$s, limit:200)"
        "{ id name description status prices { currency priceInfo { amount taxCategory } } "
        "orders { id } } }")


async def main(apply: bool) -> int:
    store = await w.ensure_store_id()
    _st, d = await w._waffo_request("/v1/graphql", {"query": LIST, "variables": {"s": store}})
    if d.get("errors"):
        print(json.dumps(d["errors"])[:400]); return 1
    products = d["data"]["onetimeProducts"]
    live = {r["v"] for r in db.query("SELECT v FROM kv WHERE k LIKE 'waffo_product:%'", ())}

    done = skipped = 0
    for p in products:
        if p["status"] == "active" or p["id"] in live:
            continue
        if p["name"].startswith(TAG):
            continue
        if p.get("orders"):
            print(f"  keep as-is (has {len(p['orders'])} order(s)): {p['name']}")
            skipped += 1
            continue
        name = TAG + p["name"]
        print(f"  {p['id']}  -> {name}")
        done += 1
        if apply:
            prices = {x["currency"]: {"amount": (x.get("priceInfo") or {}).get("amount"),
                                      "taxIncluded": True,
                                      "taxCategory": (x.get("priceInfo") or {}).get("taxCategory") or "saas"}
                      for x in (p.get("prices") or [])}
            st, r = await w._waffo_request("/v1/actions/onetime-product/update-product", {
                "id": p["id"], "name": name,
                "description": TAG + (p.get("description") or p["name"]),
                "prices": prices})
            if st >= 300:
                print(f"    FAILED {st} {json.dumps(r, ensure_ascii=False)[:200]}")
                return 1

    active = sum(1 for p in products if p["status"] == "active")
    print(f"\n{len(products)} products: {active} active, {done} renamed, {skipped} left alone (order history)"
          + ("" if apply else "  [dry run — pass --apply]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--apply" in sys.argv)))
