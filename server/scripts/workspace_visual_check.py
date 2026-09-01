"""逐个打开云工作台产品, 截图并检查有没有登录墙。**看渲染后的页面, 不看 HTML。**

为什么要有这个脚本
------------------
我们的产品全躲在 forward_auth 后面, 而这些产品几乎都自带账号体系 —— 我们靠注入
配置或会话把它们的登录墙拆掉。这类拆除**只有真渲染出来才看得见是否成功**:
  * Dify 会话失效但 cookie 还在 → 先是整页渲染错误, 再是登录页;
  * OpenClaw 来源未放行 → 弹出要填 WebSocket URL/令牌/密码的连接表单;
  * OpenClaw 设备配对 → 让你去主机上跑 `openclaw devices approve <id>`;
  * CloudCLI 的向导 → "Git 配置(必填)";
  * code-server 自带的 Chat 面板 → 右下角一个 GitHub "Sign In"。
以上没有一个能靠 HTTP 状态码或 HTML 源码发现: 它们都是 200, 而 SPA 的 HTML
在登录前后长得一模一样。2026-08-31 一天之内我在六个产品上重复踩了同一类坑,
每次都是"接口全绿 → 老板打开一看是墙"。

**源 IP 的坑 (踩过)**: 别直连实例的内网 IP —— OpenClaw 这类走 trusted-proxy 的
产品只信任应用机那个网段, 直连一律 403, 而那是**防护在正常工作**, 不是产品坏了。
必须走公网域名, 让 Caddy 去做那一跳。

用法::

    python3 -m scripts.workspace_visual_check                 # 全部已启用产品
    python3 -m scripts.workspace_visual_check openclaw dify   # 只看这几个

退出码 0 = 都没有墙; 1 = 至少一个可疑, 截图在 --out 目录里。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

#: 渲染后出现这些**可见文字**就当作可疑。
#:
#: 只匹配整词/短语, 不匹配 SPA bundle 里的标识符 —— 这正是"看渲染不看源码"的
#: 意义: `token` 出现在 JS 里毫无意义, 出现在屏幕上就是要用户填东西。
WALL_PHRASES = [
    "sign in",
    "log in",
    "login",
    "welcome back",
    "登录",
    "登陆",
    "password",
    "密码",
    "passphrase",
    "devices approve",
    "pair this device",
    "配对",
    "api key",
    "access token",
    "enter your",
    "git configuration",  # CloudCLI 的首次向导
    "required",  # 向导里的"必填"
]

#: 这些出现在屏幕上说明产品**确实起来了**但我们看到的是错误页。
BROKEN_PHRASES = [
    "渲染此组件时发生了意外错误",
    "unexpected error",
    "internal server error",
    "502 bad gateway",
    "cannot get /",
    "application error",
]

DRIVER = r"""
import json, pathlib, sys
from playwright.sync_api import sync_playwright

spec = json.loads(pathlib.Path("/work/spec.json").read_text())
out = pathlib.Path("/work/out"); out.mkdir(parents=True, exist_ok=True)
results = []

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
    # 会话 cookie 一次性种在顶级域上, 所有产品子域共用 —— 与真实用户一样。
    ctx.add_cookies([{
        "name": spec["cookie_name"], "value": spec["token"],
        "domain": "." + spec["base_domain"], "path": "/", "secure": True,
    }])
    for prod in spec["products"]:
        page = ctx.new_page()
        entry = {"id": prod["id"], "url": prod["url"]}
        try:
            page.goto(prod["url"], timeout=60000, wait_until="domcontentloaded")
            # 冷启动: 我们自己的启动页会轮询, 等它跳走。最多等 3 分钟 ——
            # Coze 那种十容器栈实测 90 秒才起得来。
            for _ in range(90):
                if "/work/starting" not in page.url and "启动中" not in page.title():
                    break
                page.wait_for_timeout(2000)
            # SPA 还要时间渲染, 光 domcontentloaded 不够 —— 这正是漏掉登录墙的
            # 那一步。
            page.wait_for_timeout(6000)
            entry["title"] = page.title()
            entry["final_url"] = page.url
            entry["text"] = (page.inner_text("body") or "")[:4000]
            # **可见的密码框**是最硬的信号: 关键词会误伤 (待办清单里的
            # "Add LLM API key"、页脚的"登录"), 一个真在等你输密码的框不会。
            entry["pw"] = page.locator("input[type=password]").count()
            shot = out / (prod["id"] + ".png")
            page.screenshot(path=str(shot), full_page=False)
            entry["shot"] = shot.name
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"[:300]
        results.append(entry)
        page.close()
    browser.close()

pathlib.Path("/work/results.json").write_text(json.dumps(results, ensure_ascii=False))
"""


def _products(only: list[str]) -> list[dict]:
    from app import apps_catalog, config, products

    base = config.PUBLIC_BASE.rstrip("/").split("//")[-1]
    out = []
    for p in products.enabled():
        if not p.domain or (only and p.id not in only):
            continue
        out.append({"id": p.id, "url": f"https://{p.domain}/"})
    # 住在主站上的产品 (数字人) 没有自己的域名, 但一样要看渲染 —— 它的形象清单
    # 和背景图都是拿会话去 GPU 节点换回来的, 任何一环断了页面照样 200, 只是空的。
    site = apps_catalog.site_apps()
    for a in apps_catalog.CATALOG:
        if a.id in site and a.href and not (only and a.id not in only):
            out.append({"id": a.id, "url": f"https://{base}{a.href}"})
    if not out:
        raise SystemExit("没有可检查的产品 (是不是都没配域名?)")
    return out, base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("products", nargs="*", help="只检查这几个产品 id")
    ap.add_argument("--email", default="qa-verify@dshcloud.online", help="用哪个账号的会话")
    ap.add_argument("--out", default="/tmp/dsh-visual", help="截图与报告放哪")
    ap.add_argument("--emit-spec", help="把规格写到这个目录 (在 dhc-server 容器里跑)")
    ap.add_argument("--read-results", help="从这个目录读浏览器结果并判读")
    args = ap.parse_args()

    from app import config, db, security

    prods, base_domain = _products(args.products)
    user = db.query_one("SELECT * FROM users WHERE email=?", (args.email,))
    if user is None:
        raise SystemExit(f"没有这个账号: {args.email}")
    token = security.sign_token(user["id"], epoch=user["session_epoch"])

    spec = {
        "cookie_name": config.SESSION_COOKIE,
        "token": token,
        "base_domain": base_domain,
        "products": prods,
    }

    print(f"==> {len(prods)} 个产品 (账号 {args.email})")
    for p in prods:
        print(f"      {p['id']:14s} {p['url']}")

    # **dhc-server 容器里没有 docker**, 所以浏览器不在这里跑。这个脚本负责把
    # 规格 (会话令牌 + 产品清单) 写到一个共享目录, 由宿主侧的 visual_check.sh
    # 起 Playwright 容器, 跑完再回来判读。分两段是被迫的, 但也顺带让"判读逻辑"
    # 和"怎么起浏览器"解耦了。
    if args.emit_spec:
        target = pathlib.Path(args.emit_spec)
        target.mkdir(parents=True, exist_ok=True)
        (target / "spec.json").write_text(json.dumps(spec, ensure_ascii=False))
        (target / "driver.py").write_text(DRIVER)
        print(f"规格已写到 {target}")
        return 0

    if not args.read_results:
        raise SystemExit("要么 --emit-spec 写规格, 要么 --read-results 判读 (见 visual_check.sh)")

    work = pathlib.Path(args.read_results)
    results = json.loads((work / "results.json").read_text())
    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-r", str(work / "out") + "/.", str(outdir)], check=False)

    bad = 0
    print()
    for e in results:
        pid = e["id"]
        if e.get("error"):
            print(f"  ✗ {pid:14s} 打不开: {e['error']}")
            bad += 1
            continue
        text = (e.get("text") or "").lower()
        # **整页就是一段 JSON = 后端在答, 但答的是错误报文**, 不是应用。
        # 2026-09-01 漏过一次: OpenHands 首页返回 {"detail":"Not Found"} (工作目录
        # 不对, 静态文件没挂上), 而 "not found" 不在下面的词表里 —— 脚本报了 ✓,
        # 截图上是一行 JSON。词表永远追不全, 但"整页是 JSON"这个形状是硬的。
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}") and len(stripped) < 400:
            print(f"  ✗ {pid:14s} 页面是一段 JSON, 不是应用: {stripped[:80]}")
            bad += 1
            continue
        walls = [w for w in WALL_PHRASES if w in text]
        broken = [b for b in BROKEN_PHRASES if b in text]
        # 命中词**要带上下文打出来**。光报一个 ['api key'] 没法判断是墙还是待办,
        # 每次都得去翻截图 —— 2026-09-01 一轮里三个红有两个是这么白翻的。
        for w in walls:
            i = text.find(w)
            print(f"      「{w}」 …{text[max(0, i - 40) : i + len(w) + 40]}…".replace("\n", " "))
        if e.get("pw"):
            print(f"  ✗ {pid:14s} 页面上有 {e['pw']} 个密码框 —— 这是真墙  ({e.get('shot')})")
            bad += 1
        elif broken:
            print(f"  ✗ {pid:14s} 页面是坏的: {broken}  ({e.get('shot')})")
            bad += 1
        elif walls:
            print(f"  ✗ {pid:14s} 疑似登录墙: {walls}  ({e.get('shot')})")
            bad += 1
        else:
            print(f"  ✓ {pid:14s} {e.get('title', '')[:40]}")
    print(f"\n截图: {outdir}")
    if bad:
        print(f"!! {bad} 个产品可疑 —— **去看截图**, 关键词匹配只是线索不是判决", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
