"""Claude Code 的请求形状, 经我们这条链路到底通不通 —— 按型号实测通过率。

    python server/scripts/probe_anthropic_face.py                 # 打我们的网关 (默认)
    python server/scripts/probe_anthropic_face.py --upstream      # 直接打上游中继 (定位用)
    python server/scripts/probe_anthropic_face.py -m claude-sonnet-5 -n 10
    python server/scripts/probe_anthropic_face.py --variants      # 同型号按 body 变体拆开看

**为什么要有这个**: 2026-09-11 实测到 `claude-sonnet-5` 经网关约三分之二请求 400。
不是型号不存在, 也不是我们的代码错 —— 上游中继把同一个型号**按请求轮询**到几家
不同的后端, 而 Claude Code 每次都带 thinking + context_management + output_config,
几家里只有一部分全收:

    Bedrock 那路 : ValidationException: "thinking.type.enabled" is not supported
                   for this model. Use "thinking.type.adaptive" …
    另一家       : context_management: Extra inputs are not permitted
    直连 Anthropic: 全收

所以**一次成功不能证明什么**, 必须重复采样 (同 memory thinking-budget-by-model 那条:
探针必须重复采样)。判据也不能只看状态码词表 —— 这里额外按响应 id 的前缀把命中的
后端分开统计, 那是不可伪造的: msg_bdrk_* = Bedrock, gen-* = 另一家中转, msg_01* =
直连 Anthropic, msg_gwhb_* = 上游自己的缓存层。

这份**不进 CI**: 它要真花钱、要真打上游。手动跑, 或挂 cron 当巡检 (同
watch_pricing_drift.sh 的定位)。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import httpx

#: Claude Code 2.1.x 真发的 body 形状 (2026-09-11 用本地 sink 抓的原样, 不是照文档编的):
#: body keys = model,messages,system,tools,metadata,max_tokens,thinking,context_management,
#: output_config,stream
CC_THINKING = {"type": "enabled", "budget_tokens": 1024}
CC_CONTEXT_MANAGEMENT = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
CC_OUTPUT_CONFIG = {"effort": "medium"}
#: 同一次抓包里的 beta 头。我们的网关目前**不转发**它 —— 留在这里是为了让 --upstream
#: 那条路尽量贴近真实客户端。
CC_BETA = (
    "claude-code-20250219,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
    "mid-conversation-system-2026-04-07,effort-2025-11-24"
)

#: body 变体。名字即"带了哪几样", 用来定位是哪一样把请求打掉的。
VARIANTS: dict[str, dict] = {
    "claude-code": {"thinking": True, "cm": True, "oc": True},  # 客户端原样
    "no-cm": {"thinking": True, "cm": False, "oc": True},  # 去掉 context_management
    "adaptive": {"thinking": "adaptive", "cm": False, "oc": False},  # 归一化后的样子
    "plain": {"thinking": False, "cm": False, "oc": False},  # 什么都不带
}


def build_body(model: str, variant: dict, *, stream: bool = False) -> dict:
    body: dict = {
        "model": model,
        "max_tokens": 2048,
        "stream": stream,
        "messages": [{"role": "user", "content": "reply with one word: ok"}],
    }
    if variant["thinking"] == "adaptive":
        body["thinking"] = {"type": "adaptive"}
    elif variant["thinking"]:
        body["thinking"] = dict(CC_THINKING)
    if variant["cm"]:
        body["context_management"] = json.loads(json.dumps(CC_CONTEXT_MANAGEMENT))
    if variant["oc"]:
        body["output_config"] = dict(CC_OUTPUT_CONFIG)
    return body


def backend_of(resp_id: str) -> str:
    """响应 id 的前缀 = 这一发落到了哪家后端。

    **这是判据里不可伪造的那部分**: 状态码会骗人 (同一个型号两发两个结果),
    id 前缀不会 —— 它由真正生成这条消息的那家写。
    """
    if resp_id.startswith("msg_bdrk_"):
        return "bedrock"
    if resp_id.startswith("gen-"):
        return "openrouter-ish"
    if resp_id.startswith("msg_gwhb_"):
        return "upstream-cache"
    if resp_id.startswith("msg_01"):
        return "anthropic-direct"
    return f"other({resp_id[:10]})" if resp_id else "unknown"


def classify_error(payload: dict, text: str) -> str:
    """把错误归到"哪一样不被接受", 而不是原样堆一串。"""
    msg = ""
    err = payload.get("error")
    if isinstance(err, dict):
        msg = str(err.get("message") or "")
    msg = msg or text
    low = msg.lower()
    if "context_management" in low or "extra inputs are not permitted" in low:
        return "rejects:context_management"
    if "thinking" in low and ("not supported" in low or "adaptive" in low):
        return "rejects:thinking.enabled"
    if "output_config" in low:
        return "rejects:output_config"
    if "is not offered" in low:
        return "not-in-catalog"
    return "other:" + msg.strip().replace("\n", " ")[:90]


def one_shot(client: httpx.Client, url: str, headers: dict, body: dict) -> tuple[str, str]:
    """返回 (结果, 细节)。结果是 ok / err / exc。"""
    try:
        r = client.post(url, json=body, headers=headers, timeout=120)
    except Exception as e:  # noqa: BLE001 — 网络抖动要与"被拒"分开, 不能混成一类
        return "exc", f"{type(e).__name__}"
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if r.status_code == 200:
        return "ok", backend_of(str(payload.get("id") or ""))
    return "err", f"{r.status_code} {classify_error(payload, r.text[:200])}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--models", default="", help="逗号分隔; 默认为两格的默认型号 + 对照组")
    ap.add_argument("-n", "--times", type=int, default=6, help="每个组合打几发 (默认 6)")
    ap.add_argument("--upstream", action="store_true", help="绕开我们的网关, 直接打上游中继")
    ap.add_argument("--variants", action="store_true", help="按 body 变体拆开 (定位是哪一样被拒)")
    ap.add_argument("--fail-under", type=float, default=0.0, help="通过率低于它就退出码 1 (巡检用)")
    args = ap.parse_args()

    if args.upstream:
        base = (
            os.environ.get("UPSTREAM_ANTHROPIC_BASE") or os.environ.get("UPSTREAM_BASE_URL") or ""
        ).rstrip("/")
        key = os.environ.get("UPSTREAM_API_KEY", "")
        if not base or not key:
            print("!! --upstream 要 UPSTREAM_BASE_URL + UPSTREAM_API_KEY (在生产容器里跑)", file=sys.stderr)
            return 2
        url = base + "/messages"
        headers = {
            "x-api-key": key,
            "authorization": f"Bearer {key}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": CC_BETA,
        }
        where = f"上游中继 {base}"
    else:
        base = (os.environ.get("DSH_GATEWAY_BASE") or "https://aistore.best").rstrip("/")
        key = os.environ.get("DSH_CLOUD_TOKEN", "")
        if not key:
            print("!! 要 DSH_CLOUD_TOKEN (工作台令牌; 在工作台 pod 里跑最省事)", file=sys.stderr)
            return 2
        url = base + "/llm/anthropic/v1/messages?beta=true"
        headers = {"x-api-key": key, "authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"}
        where = f"我们的网关 {base}"

    models = [m.strip() for m in args.models.split(",") if m.strip()] or ["claude-sonnet-5", "claude-fable-5"]
    variants = VARIANTS if args.variants else {"claude-code": VARIANTS["claude-code"]}

    print(f"打的是: {where}")
    print(f"型号: {', '.join(models)} | 变体: {', '.join(variants)} | 每组 {args.times} 发\n")

    worst = 1.0
    with httpx.Client() as client:
        for model in models:
            for vname, variant in variants.items():
                tally: Counter[str] = Counter()
                oks = 0
                for _ in range(args.times):
                    kind, detail = one_shot(client, url, headers, build_body(model, variant))
                    tally[f"{kind}: {detail}"] += 1
                    oks += kind == "ok"
                rate = oks / args.times
                worst = min(worst, rate)
                flag = "✓" if rate == 1 else ("✗" if rate == 0 else "~")
                print(f"{flag} {model:<22} {vname:<12} 通过 {oks}/{args.times} ({rate:.0%})")
                for k, c in tally.most_common():
                    print(f"      {c}x {k}")
                print()

    if args.fail_under and worst < args.fail_under:
        print(f"!! 最差通过率 {worst:.0%} 低于 {args.fail_under:.0%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
