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

# 我们转售的向量化模型。**价格仍然来自价目表**, 这里只钉表里没有的两件事:
# 原生维度, 以及上游认不认 `dimensions` 参数 —— 网关的 /models 只给
# id/object/created/owned_by 四个字段, 控制台的牌价接口也不带这两项, 只能实测。
# 实测日期 2026-08-29, 方法是发一条最短的 /embeddings 请求数返回向量的长度,
# 再带 dimensions=512 复发一次看是 200 还是 400。
#
# 为什么不能跟对话模型走同一条路: 它们没有输出 token, 混合单价
# (输入 x 0.75 + 输出 x 0.25) 对它们没有意义, 而 charge_credits 对**不在对话
# 目录里**的 id 会按"最贵条目"兜底 —— 拿对话目录去算一次向量化请求, 用户会被
# 按 claude-fable-5 的输出价收钱。所以另立一节, 由 charge_embedding_credits 算。
#
# (id, 展示名, 原生维度, 上游是否接受 dimensions)
EMBEDDING_MODELS = (
    ("Qwen/Qwen3-Embedding-0.6B", "Qwen3-Embedding-0.6B", 1024, True),
    ("Qwen/Qwen3-Embedding-4B", "Qwen3-Embedding-4B", 2560, True),
    ("Qwen/Qwen3-Embedding-8B", "Qwen3-Embedding-8B", 4096, True),
    # 上游 0 元, 但仍按每次请求收底价 1 积分 (charge_embedding_credits 的下限)。
    # 唯一一个**拒收** dimensions 的 (400 code=20015), 所以别把它设成默认 ——
    # 客户端普遍会带上 dimensions。
    ("BAAI/bge-m3", "BGE-M3", 1024, False),
    ("text-embedding-3-small", "Text-Embedding-3-Small", 1536, True),
    ("text-embedding-3-large", "Text-Embedding-3-Large", 3072, True),
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
    ap.add_argument("--default-embedding-model", default="Qwen/Qwen3-Embedding-0.6B")
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

    # curated 会把 served 换成清单本身, 而向量化模型不在那份清单里 —— 先留一份
    # 完整的, 否则下面那道"网关是否真的在售"的检查会把每个模型都判成缺货。
    served_all = set(served)

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

    embeddings = []
    for mid, label, dims, req_dims in EMBEDDING_MODELS:
        if mid not in served_all:
            print(f"embedding model absent from the gateway: {mid}", file=sys.stderr)
            return 1
        row = prices.get(mid)
        # 0 元是合法价 (bge-m3 上游免费), 缺价才是问题 —— 用 is None 分开这两件事,
        # 真值判断会把免费模型当成没价格而静默丢掉。
        if row is None or row.get("input_price") is None:
            print(f"price table has no input price for {mid}", file=sys.stderr)
            return 1
        embeddings.append(
            {
                "id": mid,
                "upstream_model": mid,
                "display_name": label,
                "provider": provider_of(mid, row.get("provider", "")),
                "input_usd_per_m": round(row["input_price"], 4),
                "dimensions": dims,
                "supports_dimensions": req_dims,
                "default": mid == args.default_embedding_model,
            }
        )
    if embeddings and not any(m["default"] for m in embeddings):
        print(f"--default-embedding-model {args.default_embedding_model} is not offered", file=sys.stderr)
        return 1

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
        "_comment_embeddings": (
            "向量化模型。只按**输入** token 计价 (它们没有输出 token), 由 "
            "model_catalog.charge_embedding_credits 结算; dimensions 是原生维度, "
            "supports_dimensions 说的是上游认不认这个参数 (BGE-M3 不认)。"
        ),
        "embedding_models": embeddings,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")

    print(f"wrote {len(models)} models -> {args.out}")
    print(
        f"  倍率区间 {models[0]['multiplier']}x ({models[0]['id']}) "
        f"… {models[-1]['multiplier']}x ({models[-1]['id']})"
    )
    print(f"  跳过非对话模型 {len(skipped)} 个，缺价格 {len(unpriced)} 个: {unpriced[:6]}")
    print(f"  向量化模型 {len(embeddings)} 个，默认 {args.default_embedding_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
