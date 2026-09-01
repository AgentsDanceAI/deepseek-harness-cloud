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

PROBE = ("() => { const v = document.querySelector('#avVideo');"
         " return {t: v.currentTime, w: v.videoWidth, h: v.videoHeight,"
         " paused: v.paused, muted: v.muted, op: getComputedStyle(v).opacity,"
         " ready: v.readyState, net: v.networkState, err: v.error && v.error.code,"
         " buf: v.buffered.length, hasSrc: !!v.currentSrc}; }")


# 等到 want(探针) 为真; 返回最后一次探到的状态。
# (这里只能用注释: 整段 driver 装在外层的 r\"\"\"...\"\"\" 里, 嵌一个三引号
#  docstring 会把外层字符串提前截断 —— 推过一次坏文件, 就栽在这上面。)
def wait_for(page, want, secs=60):
    v = page.evaluate(PROBE)
    for _ in range(secs):
        if want(v):
            return v
        page.wait_for_timeout(1000)
        v = page.evaluate(PROBE)
    return v


with sync_playwright() as p:
    # **必须是真 Chrome, 不能用 Playwright 自带的 Chromium**: 后者把专有编解码
    # 编译掉了, H.264 (avc1) 一律 isTypeSupported=false —— 页面会正确地说"这个
    # 浏览器不支持实时视频", 而那是**测试浏览器**的毛病, 不是产品的。
    browser = p.chromium.launch(channel="chrome",
                                args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
    ctx.add_cookies([{
        "name": spec["cookie_name"], "value": spec["token"],
        "domain": "." + spec["base_domain"], "path": "/", "secure": True,
    }])
    page = ctx.new_page()
    page.on("console", lambda m: res.setdefault("console", []).append(f"{m.type}: {m.text}"[:200]))
    # 4xx/5xx 要**带着是哪个 URL** 记下来 —— 光看到"404"没法判断是我们的东西坏了
    # 还是第三方探针。
    page.on("response", lambda r: r.status >= 400 and res.setdefault("bad", []).append(f"{r.status} {r.url}"[:160]))
    page.on("requestfailed", lambda r: res.setdefault("bad", []).append(f"failed {r.url}"[:160]))
    try:
        page.goto(spec["url"], timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        page.click("#avCall")
        # 打字那一栏出现 = 上游 ready 已到。接通后她会先说一句招呼。
        page.wait_for_selector("#avSay:not([hidden])", timeout=30000)

        # 第一段: 招呼。验"露"。
        res["greet"] = wait_for(page, lambda v: v["t"] > 0.3 and v["w"] > 0, 40)
        page.screenshot(path=str(out / "call.png"))
        # 验"藏" —— 她说完图层要收回去, 漏了就是最后一帧僵在背景上 (重影)。
        res["greet_after"] = wait_for(page, lambda v: v["op"] == "0", 25)

        # 第二段: 真问一句, 验模型那条路 (她的回复要出画、要进字幕)。
        # 分两段是必要的: 只看第一段的话, 招呼是**固定文案**, 模型根本没参与 ——
        # 而模型那条路恰恰是最容易慢/挂的一段。
        before = page.inner_text("#avLog")
        page.fill("#avSay", spec["prompt"])
        page.press("#avSay", "Enter")
        res["reply"] = wait_for(page, lambda v: v["t"] > (res["greet_after"]["t"] + 0.3), 60)
        res["reply_after"] = wait_for(page, lambda v: v["op"] == "0", 30)
        res["log"] = page.inner_text("#avLog")[:400]
        res["log_grew"] = len(res["log"]) > len(before)
        res["status"] = page.inner_text("#avStatus")[:200]
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
    for line in (res.get("bad") or [])[-8:]:
        print(f"    请求失败 | {line}")
    for line in (res.get("console") or [])[-10:]:
        if "cloudflareinsights" not in line:
            print(f"    控制台 | {line}")
    if res.get("error"):
        print(f"  ✗ 打不通: {res['error']}")
        return 1

    bad = []
    for key, label in (("greet", "接通后的招呼"), ("reply", "她对我那句话的回复")):
        v = res.get(key) or {}
        after = res.get(key + "_after") or {}
        print(
            f"    {label}: currentTime={v.get('t')} {v.get('w')}x{v.get('h')} "
            f"opacity={v.get('op')} readyState={v.get('ready')} err={v.get('err')} "
            f"-> 说完 opacity={after.get('op')}"
        )
        if not (v.get("t") or 0) > 0.3 or not (v.get("w") or 0) > 0:
            bad.append(f"{label}: 画面没动 —— 字节可能在收但没在播")
        if v.get("muted"):
            bad.append(f"{label}: 视频是静音的 —— 一通哑巴电话")
        if (v.get("op") or "0") == "0":
            bad.append(f"{label}: 视频层是透明的 —— 用户看不到她")
        if after.get("op") != "0":
            bad.append(f"{label}: 说完了图层没藏 —— 最后一帧僵在背景上就是重影")
    print(f"    状态栏: {res.get('status', '')!r}")
    print(f"    字幕: {(res.get('log') or '')!r}")
    if not res.get("log_grew"):
        bad.append("我说完之后字幕没长 —— 她的回复没进字幕")

    for b in bad:
        print(f"  ✗ {b}")
    if not bad:
        print("  ✓ 招呼与回复都出了画、有声音、说完收回; 字幕在画面上")
    return 1 if bad else 0
