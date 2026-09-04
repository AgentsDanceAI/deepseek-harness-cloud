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
    "必填",  # 向导里的必填项
]

#: 某个产品里**已查明无害**的命中: 产品 id -> {命中词: (必须同时出现的锚点, 为什么)}。
#:
#: 只在锚点也在场时才豁免 —— 同一个词换个地方出现照样报。无锚点的白名单等于
#: 把这个词从那个产品上永久删掉, 那才是真会漏掉墙的做法。
IGNORE = {}

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
            resp = page.goto(prod["url"], timeout=60000, wait_until="domcontentloaded")
            entry["status"] = resp.status if resp else None
            # 冷启动有**两种**长相, 都要等:
            #  · 我们自己的启动页会轮询, 等它跳走;
            #  · **网关 502/503** —— 实例还没听端口, Caddy 没人可转。这时是一张
            #    错误页, 不会自己好, 得重新导航。
            # 最多等 6 分钟 (Coze 那种十容器栈实测 90 秒; OpenHands 5.8GB 更久)。
            for _ in range(180):
                if entry.get("status") in (502, 503, 504):
                    page.wait_for_timeout(4000)
                    try:
                        resp = page.goto(prod["url"], timeout=60000, wait_until="domcontentloaded")
                        entry["status"] = resp.status if resp else None
                    except Exception:
                        pass
                    continue
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
    # **先看机时够不够** (与 product_use_check 同一道闸)。机时耗尽时工作台把浏览器打回
    # 定价页, 而定价页 HTTP 200、没有密码框、没有坏词 —— 下面的判读会给它打 ✓。
    # 2026-09-04 全员回归跑到第 5 个产品机时归零, 后面 11 个全是"定价 — deepseek-harness-cloud"
    # 却全绿。每个产品要冷启动并跑到回收 (十几分钟机时), 这活儿本来就费机时。
    from app import work_access

    state = work_access.state(user["id"])
    left = state.get("minutes_left", state.get("remaining_minutes", 0)) or 0
    need = len(prods) * 12
    print(f"==> 机时余量 {left} 分钟 (这轮大约要 {need} 分钟)")
    if left < need:
        raise SystemExit(
            f"!! 机时不够 ({left} < {need})。跑下去会把'没配额'验成'产品坏了' —— 先给 {args.email} 补一包机时再来。"
        )

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
        # **状态码先看**。2026-09-01 这个脚本给两个 502 页面判了绿: Cloudflare 的
        # 错误页写的是"Bad gateway / Error code 502", 而词表里那条是
        # "502 bad gateway" —— 词序对不上就漏了。词表永远追不全, 状态码是硬的。
        # 假绿比假红严重: 假红我会去看图, 假绿谁也不会再看。
        if (e.get("status") or 200) >= 400:
            print(f"  ✗ {pid:14s} 打开就是 HTTP {e['status']}  ({e.get('shot')})")
            bad += 1
            continue
        # **落在别的域上 = 根本没到产品**。工作台把人打回主站的情形有三种: 机时/积分
        # 耗尽 (/pricing)、没登录 (/login)、超时还在启动页 (/work/starting) —— 三种页面
        # 都是 HTTP 200、没有密码框、没有坏词, 下面的词表全部放行。2026-09-04 就这么给
        # 11 个"定价 — deepseek-harness-cloud"打了 ✓。产品域名是硬的: 不在上面就是没过。
        from urllib.parse import urlparse

        want_host = urlparse(e.get("url") or "").hostname
        got = urlparse(e.get("final_url") or "")
        if want_host and got.hostname and got.hostname != want_host:
            print(
                f"  ✗ {pid:14s} 被带到 {got.hostname}{got.path} 而不是产品页 (机时/积分耗尽? 没登录? 启动超时?)  ({e.get('shot')})"
            )
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
        for w, (anchor, why) in (IGNORE.get(pid) or {}).items():
            if w in walls and anchor in text:
                walls.remove(w)
                print(f"      (放过「{w}」: {why})")
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
