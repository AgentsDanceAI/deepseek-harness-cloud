"""**真动手用一遍**每个产品, 而不是只看首屏渲染出来没有。

为什么非要这个
--------------
workspace_visual_check 只证明"页面渲染出来了、没有登录墙"。2026-09-01 老板逐个
点开新接的四个产品, **四个全废**, 而那个脚本对它们全报 ✓ —— 因为三个毛病全都
发生在**用户动手之后**:

  * OpenManus / CrewAI: 首屏是漂亮的终端, 敲 python 就
    `ModuleNotFoundError: No module named 'pydantic'` (ttyd 起的是登录 shell,
    把我们 export 的 PATH 冲掉了);
  * LangChain: 聊天界面好好的, 一发消息就红
    `"/langgraph/threads" cannot be parsed as a URL`;
  * OpenHands: 首页写着"让我们开始开发", 点新对话就
    `500: Agent Server Failed to start properly`。

这套错法没有一个能靠"页面上有没有出现某些字"发现。判据只能是**动手之后有没有
真回应**。

同一条教训这个会话里栽过两次: 数字人那次是"字节在收、计时在走、画面一帧不动",
补了 avatar_call_check 去真打一通电话; 接产品时又退回只看首屏。

用法::

    bash scripts/product_use_check.sh                    # 全部会用的产品
    bash scripts/product_use_check.sh openmanus crewai   # 只试这几个

退出码 0 = 都真的能用。
"""

from __future__ import annotations

import argparse
import json
import pathlib

#: 每个产品"动手"的方式与"算数"的判据。
#:
#: 终端类的判据是**敲一条命令看输出**, 不是看提示符在不在 —— 提示符在而命令跑不了
#: 正是老板撞到的那个形状。
USE = {
    "openmanus": {
        "kind": "terminal",
        # 敲一条能立刻见分晓的: 解释器指向哪、关键依赖在不在。
        "type": "python -c 'import pydantic,sys;print(\"USE-OK\",sys.executable)'\n",
        # 分成两段: xterm 会**折行**, inner_text 里两段之间可能夹着换行 ——
        # 拿整串去匹配会把"产品其实好的"判成失败 (第一版就是这么误报的)。
        "want": ["USE-OK", "/opt/venv-openmanus/bin/python"],
        "why": "敲 python 报 ModuleNotFoundError —— 登录 shell 把 PATH 冲掉了",
    },
    "crewai": {
        "kind": "terminal",
        "type": "crewai --version && echo USE-OK\n",
        "want": ["USE-OK"],
        "why": "敲 crewai 找不到命令 —— 登录 shell 把 PATH 冲掉了",
    },
    "langchain": {
        "kind": "chat",
        "send": "只回我两个字",
        "why": '一发消息就 "cannot be parsed as a URL"',
    },
    "openhands": {
        "kind": "openhands",
        "send": "只回我两个字",
        # 判据落在"发出去的话进了对话" + 页面长出了新内容 (见 driver 里的 grew)。
        "want": ["只回我两个字"],
        # 连上又断开也不算能用。**这条目前是红的** —— 见 docs 里那条已知问题:
        # 沙箱与模型两跳都实测通 (接口建对话 10 秒 READY), 卡在前端那一跳,
        # 而浏览器控制台干净、没有失败请求、连 WebSocket 都没发起。
        "fail_extra2": ["已断开连接"],
        # 沙箱起不来时页面停在"等待沙盒", 那不算能用 —— 判据要落在它真回了话上。
        # "正在连接…"同样不算能用 —— 它比"等待沙盒"晚一步, 但用户照样干不了事。
        # **"加载中"不能当失败词**: 右侧那块面板常驻这三个字, 拿它判等于永远红。
        "fail_extra": ["等待沙盒", "waiting for sandbox", "正在连接"],
        "why": "点新对话就 500: Agent Server Failed to start properly / 一直等待沙盒",
    },
}

DRIVER = r"""
import json, pathlib
from playwright.sync_api import sync_playwright

spec = json.loads(pathlib.Path("/work/spec.json").read_text())
out = pathlib.Path("/work/out"); out.mkdir(parents=True, exist_ok=True)
results = []


# 冷启动: 我们自己的启动页会轮询, 等它跳走。
def wait_started(page):
    for _ in range(90):
        if "/work/starting" not in page.url and "启动中" not in page.title():
            return
        page.wait_for_timeout(2000)


with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
    ctx.add_cookies([{
        "name": spec["cookie_name"], "value": spec["token"],
        "domain": "." + spec["base_domain"], "path": "/", "secure": True,
    }])
    for prod in spec["products"]:
        page = ctx.new_page()
        e = {"id": prod["id"]}
        # **浏览器打不通的请求要记下来**: 有一类故障是应用把只有服务端才通的地址
        # (localhost:xxxx) 交给浏览器 —— 服务端自检全绿, 而用户那边一直转圈。
        page.on("requestfailed", lambda r: e.setdefault("bad", []).append(f"failed {r.url}"[:150]))
        # WebSocket 也要记: "正在连接…"这类卡住多半卡在它上面, 而 WS 不算 request,
        # requestfailed 抓不到。
        page.on("websocket", lambda ws: e.setdefault("ws", []).append(ws.url[:150]))
        page.on("response", lambda r: r.status >= 400 and e.setdefault("bad", []).append(f"{r.status} {r.url}"[:150]))
        try:
            page.goto(prod["url"], timeout=60000, wait_until="domcontentloaded")
            wait_started(page)
            page.wait_for_timeout(8000)
            kind = prod["kind"]

            if kind == "terminal":
                # ttyd 的 xterm 收键盘: 点一下画布拿到焦点, 再逐字敲。
                page.click("body")
                page.wait_for_timeout(1000)
                page.keyboard.type(prod["type"], delay=25)
                page.wait_for_timeout(12000)
                # **终端要读 xterm 的缓冲区, 不能用 inner_text**: ttyd 把字画在
                # canvas 上, DOM 里一个字都没有 —— inner_text 返回空串, 而屏幕上
                # 明明写着结果。第一版就是这么把好产品判成失败的 (截图里有字、
                # 抓到的文本是 '')。
                e["text"] = page.evaluate(
                    "() => { const t = window.term; if (!t || !t.buffer) return '';"
                    " const b = t.buffer.active; const out = [];"
                    " for (let i = 0; i < b.length; i++) {"
                    "   const ln = b.getLine(i); if (ln) out.push(ln.translateToString(true)); }"
                    " return out.join('\\n'); }")[-1500:]

            elif kind == "chat":
                box = page.get_by_placeholder("Type your message...")
                box.click(); box.fill(prod["send"])
                page.keyboard.press("Enter")
                # 模型要想一会儿; 出错的话红框几秒就弹出来。
                page.wait_for_timeout(45000)
                e["text"] = page.inner_text("body")[-1500:]

            elif kind == "openhands":
                # **有现成对话就点它**。沙箱是**按对话**建的, 点"新对话"要现建一个
                # (实测四分钟以上); 而容器启动时已经预热了一个 (见
                # products._openhands_boot), 侧栏里就有 —— 用户回到工作台看到的
                # 也是它。这条才是常态路径。
                try:
                    page.get_by_text("Conversation", exact=False).first.click(timeout=8000)
                except Exception:
                    page.get_by_role("button", name="新对话").first.click(timeout=15000)
                # 沙箱起来要时间, 起来之后还要真发一句、等它回。**不能只等"页面
                # 变了"就算过** —— "等待沙盒 / 加载中" 也是变了, 而那正是坏的样子。
                # 冷启动 + 起沙箱 + 前端连上, 实测要 **270 秒**。等不够久就会把
                # "还在起"报成"起不来" —— 那是假故障, 比漏报更耽误事 (为这个白判了
                # 好几轮)。给到 8 分钟, 反正它自己界面上也写着"这可能需要 1-2 分钟"。
                for _ in range(160):
                    page.wait_for_timeout(3000)
                    body = page.inner_text("body")
                    if all(w not in body for w in ("等待沙盒", "Waiting for sandbox", "正在连接")):
                        break
                try:
                    box = page.get_by_placeholder("你想要构建什么?").first
                    box.click(timeout=10000)
                    box.fill(prod["send"])
                    page.keyboard.press("Enter")
                except Exception:
                    # 输入框还没出来 —— 让判读去看 text 里的"等待沙盒"报错
                    pass
                # 发完之后**盯着页面长没长出新东西** —— 那才是"她回话了"。
                # 光看"没有失败词"不够: 消息没发出去时页面也很干净。
                before = len(page.inner_text("body"))
                for _ in range(40):
                    page.wait_for_timeout(5000)
                    if len(page.inner_text("body")) > before + 20:
                        break
                page.wait_for_timeout(5000)
                e["text"] = page.inner_text("body")[-2000:]
                e["grew"] = len(page.inner_text("body")) - before

            page.screenshot(path=str(out / (prod["id"] + ".png")))
        except Exception as ex:
            e["error"] = f"{type(ex).__name__}: {ex}"[:300]
            try:
                page.screenshot(path=str(out / (prod["id"] + ".png")))
            except Exception:
                pass
        results.append(e)
        page.close()
    browser.close()

pathlib.Path("/work/results.json").write_text(json.dumps(results, ensure_ascii=False))
"""

#: 动手之后**屏幕上出现这些就是没用成**。与首屏检查的词表分开: 这些字只有在
#: 用户动过手之后才可能出现。
FAIL_PHRASES = [
    "modulenotfounderror",
    "command not found",
    "cannot be parsed as a url",
    "an error occurred",
    "failed to start properly",
    "traceback (most recent call last)",
    "no such file or directory",
]


def _products(only: list[str]) -> tuple[list[dict], str]:
    from app import config, products

    base = config.PUBLIC_BASE.rstrip("/").split("//")[-1]
    out = []
    for p in products.enabled():
        if p.id not in USE or not p.domain or (only and p.id not in only):
            continue
        out.append({"id": p.id, "url": f"https://{p.domain}/", **USE[p.id]})
    if not out:
        raise SystemExit("没有可试的产品")
    return out, base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("products", nargs="*")
    ap.add_argument("--email", default="qa-verify@dshcloud.online")
    ap.add_argument("--emit-spec")
    ap.add_argument("--read-results")
    args = ap.parse_args()

    from app import config, db, security

    if args.emit_spec:
        prods, base = _products(args.products)
        user = db.query_one("SELECT * FROM users WHERE email=?", (args.email,))
        if user is None:
            raise SystemExit(f"没有这个账号: {args.email}")

        # **先看机时够不够**。机时耗尽时工作台会把浏览器打回定价页, 而这个脚本
        # 看到的是"元素找不到" —— 于是把"没配额"报成"产品坏了"。2026-09-01 为这个
        # 误判过两轮: 三个刚验过能用的产品一起跑时全红, 只因为跑到一半机时归零。
        # 每个产品要冷启动一次, 这活儿本来就费机时。
        from app import work_access

        state = work_access.state(user["id"])
        left = state.get("minutes_left", state.get("remaining_minutes", 0)) or 0
        need = len(prods) * 10
        print(f"==> 机时余量 {left} 分钟 (这轮大约要 {need} 分钟)")
        if left < need:
            raise SystemExit(
                f"!! 机时不够 ({left} < {need})。跑下去会把'没配额'验成'产品坏了' —— "
                f"先给 {args.email} 补一包机时再来。"
            )
        spec = {
            "cookie_name": config.SESSION_COOKIE,
            "token": security.sign_token(user["id"], epoch=user["session_epoch"]),
            "base_domain": base,
            "products": prods,
        }
        t = pathlib.Path(args.emit_spec)
        t.mkdir(parents=True, exist_ok=True)
        (t / "spec.json").write_text(json.dumps(spec, ensure_ascii=False))
        (t / "driver.py").write_text(DRIVER)
        print(f"==> {len(prods)} 个产品要真动手试 (账号 {args.email})")
        for p in prods:
            print(f"      {p['id']:12s} {p['kind']:9s} 防的是: {p['why']}")
        return 0

    if not args.read_results:
        raise SystemExit("要么 --emit-spec, 要么 --read-results (见 product_use_check.sh)")

    results = json.loads((pathlib.Path(args.read_results) / "results.json").read_text())
    bad = 0
    print()
    for e in results:
        pid = e["id"]
        for w in list(dict.fromkeys(e.get("ws") or []))[-4:]:
            print(f"      WebSocket | {w}")
        for b in list(dict.fromkeys(e.get("bad") or []))[-6:]:
            print(f"      浏览器打不通 | {b}")
        if e.get("error"):
            print(f"  ✗ {pid:12s} 动不了: {e['error']}")
            bad += 1
            continue
        text = (e.get("text") or "").lower()
        extra = (USE[pid].get("fail_extra") or []) + (USE[pid].get("fail_extra2") or [])
        hits = [f for f in FAIL_PHRASES + extra if f in text]
        want = USE[pid].get("want")
        # 逐段查, 且**把空白全抹掉再比** —— 终端里的换行/折行不该算差异
        # (第一版拿整串比, 把 "USE-OK\n/opt/venv-..." 判成了失败, 而产品是好的)。
        flat = "".join(text.split())
        missing = [w for w in (want or []) if "".join(w.lower().split()) not in flat]
        grew = e.get("grew")
        if grew is not None and grew <= 20:
            print(f"  ✗ {pid:12s} 发出去之后页面没长东西 (+{grew} 字) —— 她没回话")
            bad += 1
        elif hits:
            print(f"  ✗ {pid:12s} 动手之后报错: {hits}")
            bad += 1
        elif want and missing:
            print(f"  ✗ {pid:12s} 没看到预期回应 {missing!r}")
            bad += 1
        else:
            tail = " ".join((e.get("text") or "").split())[-90:]
            print(f"  ✓ {pid:12s} 真的用上了 … {tail}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
