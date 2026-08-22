"""Generate server/config/models.json from the gateway catalog + a price table.

Derived, never hand-written. The catalog comes from the gateway's /models, the
prices come from a price table, and everything else (credit rate, multiplier,
display name) is computed here to prevent configuration drift.

Credit convention:
  * blended price = input * 0.75 + output * 0.25   (USD per 1M tokens)
  * multiplier    = blended / blended(claude-sonnet-5)   ← Sonnet is 1.00x
  * credits / 1M  = round(multiplier * 1000)             ← 1.00x = 1000 credits
  * $1 = 100 credits
The multiplier is a RATIO, so changing the resale markup never moves it.

    python server/scripts/gen_models.py \
        --catalog qm_models.json --prices /path/to/model_pricing.json

Both inputs are optional: with no --catalog the script keeps whatever the
gateway currently lists (requires UPSTREAM_* env), and with no --prices it
refuses rather than invent numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CURATED = Path(__file__).resolve().parents[1] / "config" / "models.curated.txt"

BASELINE_MODEL = "claude-sonnet-5"  # the 1.00x anchor
CREDITS_PER_BASELINE_M = 1000  # 1.00x == 1000 credits / 1M tokens

# Families we never sell as chat models: embeddings/rerankers have no output
# tokens, and media models bill per item rather than per token.
SKIP_SUBSTRINGS = (
    "bge-",
    "reranker",
    "-asr-",
    "transcribe",
    "-tts",
    "embedding",
    "kling-",
    "seedance",
    "seedream",
    "-image",
    "codex-auto-review",
)

PROVIDER_BY_PREFIX = [
    ("claude-", "Anthropic"),
    ("gpt-", "OpenAI"),
    ("o1-", "OpenAI"),
    ("o3-", "OpenAI"),
    ("gemini-", "Google"),
    ("deepseek", "DeepSeek"),
    ("qwen", "Alibaba"),
    ("glm-", "Zhipu"),
    ("kimi", "Moonshot"),
    ("MiniMax", "MiniMax"),
    ("doubao", "ByteDance"),
    ("grok-", "xAI"),
    ("mai-", "Other"),
]


def provider_of(model_id: str, fallback: str = "") -> str:
    low = model_id.lower()
    for prefix, name in PROVIDER_BY_PREFIX:
        if low.startswith(prefix.lower()):
            return name
    return fallback or "Other"


def display_name(model_id: str) -> str:
    """Keep the id recognisable; only tidy separators and casing of known words."""
    name = model_id.replace("_", "-")
    fixes = {
        "gpt": "GPT",
        "glm": "GLM",
        "deepseek": "DeepSeek",
        "qwen": "Qwen",
        "kimi": "Kimi",
        "grok": "Grok",
        "gemini": "Gemini",
        "claude": "Claude",
        "doubao": "Doubao",
        "minimax": "MiniMax",
        "mai": "MAI",
    }
    parts = []
    for seg in name.split("-"):
        parts.append(fixes.get(seg.lower(), seg.capitalize() if seg.isalpha() and seg.islower() else seg))
    return "-".join(parts)


def blended(entry: dict) -> float:
    return (entry.get("input_price") or 0) * 0.75 + (entry.get("output_price") or 0) * 0.25


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", help="JSON array of model ids the gateway serves")
    ap.add_argument("--prices", required=True, help="price table with models[].{id,input_price,output_price}")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "config" / "models.json"))
    ap.add_argument("--default-model", default="deepseek-v4-flash")
    ap.add_argument(
        "--all", action="store_true", help="emit every priced chat model instead of config/models.curated.txt"
    )
    args = ap.parse_args()

    prices_raw = json.loads(Path(args.prices).read_text())
    rows = prices_raw["models"] if isinstance(prices_raw, dict) else prices_raw
    prices = {r["id"]: r for r in rows}

    # Expose a curated shortlist in the file's order. --all is available for
    # auditing the complete upstream catalog.
    curated, names = None, {}
    if not args.all:
        curated = []
        for ln in CURATED.read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            mid, _, label = ln.partition("=")
            mid = mid.strip()
            curated.append(mid)
            if label.strip():
                names[mid] = label.strip()

    if args.catalog:
        served = json.loads(Path(args.catalog).read_text())
    else:
        import httpx

        base = os.environ["UPSTREAM_BASE_URL"].rstrip("/")
        r = httpx.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {os.environ['UPSTREAM_API_KEY']}"},
            timeout=30,
        )
        r.raise_for_status()
        served = [m["id"] for m in r.json()["data"]]

    if BASELINE_MODEL not in prices:
        print(f"price table has no {BASELINE_MODEL}; the 1.00x anchor is undefined", file=sys.stderr)
        return 1
    base_blended = blended(prices[BASELINE_MODEL])

    if curated is not None:
        missing = [m for m in curated if m not in served]
        if missing:
            # Loud, not silent: a curated id the gateway dropped would otherwise
            # vanish from the catalog and only surface as a user's failed request.
            print(f"curated models absent from the gateway: {missing}", file=sys.stderr)
            return 1
        served = curated

    models, skipped, unpriced = [], [], []
    for mid in served:
        if any(s.lower() in mid.lower() for s in SKIP_SUBSTRINGS):
            skipped.append(mid)
            continue
        row = prices.get(mid)
        if row is None or not (row.get("output_price") or 0):
            unpriced.append(mid)
            continue
        mult = blended(row) / base_blended
        models.append(
            {
                "id": mid,
                "upstream_model": mid,
                "display_name": names.get(mid) or display_name(mid),
                "provider": provider_of(mid, row.get("provider", "")),
                "input_usd_per_m": round(row["input_price"], 4),
                "output_usd_per_m": round(row["output_price"], 4),
                # Two decimals is plenty at 1.00x but throws away ~9% at 0.04x, where
                # the displayed multiplier would no longer match what we actually
                # charge. Cheap models get a third decimal so the two agree.
                "multiplier": round(mult, 3 if mult < 0.1 else 2),
                "credits_per_m": round(mult * CREDITS_PER_BASELINE_M),
                "default": mid == args.default_model,
            }
        )

    if curated is None:
        models.sort(key=lambda m: (m["multiplier"], m["id"]))
    if not any(m["default"] for m in models) and models:
        models[0]["default"] = True

    out = {
        "_comment": (
            "由 server/scripts/gen_models.py 生成，请勿手改——手写的价目表迟早会漂。"
            f"倍率以 {BASELINE_MODEL} 为 1.00x 基准（倍率是相对值）；"
            f"1.00x = {CREDITS_PER_BASELINE_M} 积分 / 1M tokens；$1 = 100 积分。"
            "混合单价 = 输入 × 0.75 + 输出 × 0.25。实际扣费再乘 MODEL_PRICE_MARKUP。"
        ),
        "baseline_model": BASELINE_MODEL,
        "credits_per_baseline_m": CREDITS_PER_BASELINE_M,
        "models": models,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")

    print(f"wrote {len(models)} models -> {args.out}")
    print(
        f"  倍率区间 {models[0]['multiplier']}x ({models[0]['id']}) "
        f"… {models[-1]['multiplier']}x ({models[-1]['id']})"
    )
    print(f"  跳过非对话模型 {len(skipped)} 个，缺价格 {len(unpriced)} 个: {unpriced[:6]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
