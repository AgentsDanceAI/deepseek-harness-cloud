"""真打一通数字人电话, 看**画面动没动**。

为什么光有 workspace_visual_check 不够
------------------------------------
那个脚本开页面截图查登录墙 —— 数字人页在它眼里是全绿的, 而当时这个产品根本
打不通电话。这条链路上"看起来正常"的失败方式特别多, 每一种都不改变 HTTP 状态码:

  * WebSocket 握手崩在鉴权里 (WS 没有 .method) -> 连上就断, 表现是"点了没反应";
  * MediaSource 没设 duration=Infinity -> 首块播 0.18s 就派发 ended, 之后**分片
    只进缓冲不再播**。字节在收、计时在走、积分在扣, 画面一帧不动;
  * <video muted> -> 一通哑巴电话, 视觉上完全正常;
  * SourceBuffer 被半截 fMP4 弄进错误态 -> 第一句好好的, 从第二句起再不出画。

所以这里的判据只有一个: **video.currentTime 真的在往前走**, 且解码出了尺寸。

用法::

    bash scripts/avatar_call_check.sh          # 在应用机上跑

退出码 0 = 她真的说了话且画面在动。
"""

from __future__ import annotations

import argparse
import json
import pathlib

#: 她要说的那句。短 —— 这是验收不是聊天, 而每分钟都在烧 GPU 和积分。
PROMPT = "你好，请只回我两个字。"

DRIVER = r"""
import json, pathlib
from playwright.sync_api import sync_playwright

spec = json.loads(pathlib.Path("/work/spec.json").read_text())
out = pathlib.Path("/work/out"); out.mkdir(parents=True, exist_ok=True)
res = {}

with sync_playwright() as p:
    # **必须是真 Chrome, 不能用 Playwright 自带的 Chromium**: 后者把专有编解码
    # 编译掉了, H.264 (avc1) 一律 isTypeSupported=false —— 页面会正确地说"这个
    # 浏览器不支持实时视频", 而那是**测试浏览器**的毛病, 不是产品的。
    # 用 Chromium 跑这个脚本只会得到一个永远红的假故障。
    browser = p.chromium.launch(channel="chrome",
                                args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
    ctx.add_cookies([{
        "name": spec["cookie_name"], "value": spec["token"],
        "domain": "." + spec["base_domain"], "path": "/", "secure": True,
    }])
    page = ctx.new_page()
    page.on("console", lambda m: res.setdefault("console", []).append(f"{m.type}: {m.text}"[:200]))
    try:
        page.goto(spec["url"], timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        page.click("#avCall")
        # 接通后打字那一栏才出现 —— 它同时也是"上游 ready 已到"的证据。
        page.wait_for_selector("#avSay:not([hidden])", timeout=30000)
        page.fill("#avSay", spec["prompt"])
        page.press("#avSay", "Enter")
        # 她要先想 (模型) 再合成 (GPU)。实测首帧 ~2s, 给足 60s。
        for _ in range(60):
            page.wait_for_timeout(1000)
            v = page.evaluate("() => { const v = document.querySelector('#avVideo');"
                              " return {t: v.currentTime, w: v.videoWidth, h: v.videoHeight,"
                              " paused: v.paused, muted: v.muted, op: getComputedStyle(v).opacity}; }")
            if v["t"] > 0.3 and v["w"] > 0:
                break
        res["video"] = v
        res["status"] = page.inner_text("#avStatus")[:200]
        res["timer"] = page.inner_text("#avTimer")
        page.screenshot(path=str(out / "call.png"))
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:400]
        try:
            page.screenshot(path=str(out / "call.png"))
        except Exception:
            pass
    browser.close()

pathlib.Path("/work/results.json").write_text(json.dumps(res, ensure_ascii=False))
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="qa-verify@dshcloud.online", help="用哪个账号打这通电话")
    ap.add_argument("--emit-spec", help="把规格写到这个目录 (在 dhc-server 容器里跑)")
    ap.add_argument("--read-results", help="从这个目录读结果并判读")
    args = ap.parse_args()

    from app import config, db, security

    if args.emit_spec:
        user = db.query_one("SELECT * FROM users WHERE email=?", (args.email,))
        if user is None:
            raise SystemExit(f"没有这个账号: {args.email}")
        base = config.PUBLIC_BASE.rstrip("/")
        spec = {
            "cookie_name": config.SESSION_COOKIE,
            "token": security.sign_token(user["id"], epoch=user["session_epoch"]),
            "base_domain": base.split("//")[-1],
            "url": f"{base}/avatar",
            "prompt": PROMPT,
        }
        target = pathlib.Path(args.emit_spec)
        target.mkdir(parents=True, exist_ok=True)
        (target / "spec.json").write_text(json.dumps(spec, ensure_ascii=False))
        (target / "driver.py").write_text(DRIVER)
        print(f"规格已写到 {target} (账号 {args.email})")
        return 0

    if not args.read_results:
        raise SystemExit("要么 --emit-spec 写规格, 要么 --read-results 判读 (见 avatar_call_check.sh)")

    res = json.loads((pathlib.Path(args.read_results) / "results.json").read_text())
    if res.get("error"):
        print(f"  ✗ 打不通: {res['error']}")
        return 1
    v = res.get("video") or {}
    print(f"    状态栏: {res.get('status', '')!r}   计时: {res.get('timer')}")
    print(
        f"    video: currentTime={v.get('t')} {v.get('w')}x{v.get('h')} "
        f"paused={v.get('paused')} muted={v.get('muted')} opacity={v.get('op')}"
    )
    bad = []
    if not (v.get("t") or 0) > 0.3:
        bad.append("画面没动 (currentTime 没往前走) —— 字节可能在收但没在播")
    if not (v.get("w") or 0) > 0:
        bad.append("没解出视频尺寸 (SourceBuffer 里的东西没被解码)")
    if v.get("muted"):
        bad.append("视频是静音的 —— 一通哑巴电话")
    if (v.get("op") or "0") == "0":
        bad.append("视频层是透明的 —— 用户看不到她")
    for b in bad:
        print(f"  ✗ {b}")
    if not bad:
        print("  ✓ 她说了话, 画面在动, 有声音")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
