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
        # 2026-09-02 起这一格是**工作台**, 不再是裸终端 (老板: "包类似咱们为
        # claude 和 codex 建的前端啊")。所以这里也从敲命令改成发消息 —— 验收
        # 要走用户真正会走的那条路。
        "kind": "chat",
        "placeholder": "说点什么",
        "send": "{a} 加 {b} 等于几? 只回数字, 不要解释",
        "want": ["{sum}"],
        # 这一轮跑完的标志: 「停止」按钮收起来。不等它就可能在半路截屏, 而那时
        # 用量还是 0、答案可能还没到 —— 判到的是上一轮的残留。
        "busy_hidden": "#stopBtn",
        # **"本轮消耗 0↑ 0↓" 当失败**: 用量键名拼错时界面一切正常, 只是这个数
        # 永远是 0 —— 不报错、不变红, 而积分是这个产品的核心机制。
        "fail_extra2": ["0↑ 0↓"],
        "why": "发一句话没反应 (它的日志走 stderr, 外壳只读 stdout)",
    },
    "crewai": {
        "kind": "chat",
        "placeholder": "说点什么",
        "send": "{a} 加 {b} 等于几? 只回数字, 不要解释",
        "want": ["{sum}"],
        "busy_hidden": "#stopBtn",
        # 型号没钉的样子: litellm 拿它自己的默认 gpt-4o-mini 去问网关, 回 404。
        "fail_extra2": ["gpt-4o-mini", "NotFoundError", "0↑ 0↓"],
        "why": "发一句话回 404 (litellm 用了它自己的默认型号)",
    },
    "pi": {
        "kind": "chat",
        # pi-web-ui 的输入框 (中文界面): "给 pi 发送消息 — Enter 发送，/ 查看命令"。
        "placeholder": "给 pi 发送消息",
        "send": "{a} 加 {b} 等于几? 只回数字, 不要解释",
        "want": ["{sum}"],
        # 模型走错门的样子: 内置 OpenAI 提供方被点亮, 拿网关令牌去打 api.openai.com。
        "fail_extra2": ["OpenAI API error", "invalid_jwt", "(401)"],
        "why": "回 OpenAI API error (401) invalid_jwt —— 内置提供方被 OPENAI_API_KEY 点亮",
    },
    "langchain": {
        "kind": "chat",
        "placeholder": "Type your message...",
        # 同 openhands: 出一道答案不可能出现在题面里的题。先前只问"只回我两个字",
        # 判据就只剩"没出现失败词"——而消息压根没发出去时页面也很干净。
        "send": "{a} 加 {b} 等于几? 只回数字, 不要解释",
        "want": ["{sum}"],
        "why": '一发消息就 "cannot be parsed as a URL"',
    },
    "autogen": {
        "kind": "chat",
        # 它的输入框是 "Type your message here...", 比 langchain 那个多两个词 ——
        # 拿 langchain 的选择器去找它是找不到的 (子串方向反了)。
        "placeholder": "Type your message here",
        "send": "{a} 加 {b} 等于几? 只回数字, 不要解释",
        "want": ["{sum}"],
        # 队伍/会话没预热成的样子。两者都会让用户进来对着一个空壳。
        "fail_extra": ["No session selected", "Create a team to get started"],
        "why": "侧栏空的 / 首屏没有会话, 用户得自己走一遍新建弹窗",
    },
    "openhands": {
        "kind": "openhands",
        # 出一道**答案不可能出现在题面里**的题。先前问"只回我两个字", 她回了"好的"
        # (完全正确), 可脚本没法证明那两个字是她写的 —— 于是判据只能退回去数页面
        # 长度, 而从首页进对话页文字反而**少了两千多字** (推荐 agent、自动化那些块
        # 没了), 一个能用的产品就这么被判成红的。
        # 换成算术: 屏幕上出现 13, 就只可能是她算的。
        # 数字挑大一点、别是常见数: "13" 那种在时间戳、侧栏、版本号里都可能撞上,
        # 撞上就是**假绿** —— 比假红更坏。
        "send": "{a} 加 {b} 等于几? 只回数字, 不要解释",
        "want": ["{sum}"],
        # 模型那一跳断了的样子: 它把上游异常原样贴在对话里。这几条**必须当失败**,
        # 否则"页面长出新东西"会把一条报错当成回话。
        "fail_extra2": ["LLMAuthenticationError", "AuthenticationError", "LLM profile"],
        # 首启向导没免掉的样子 (那是我们注入前端要解决的事), 以及模型没配上时
        # 它自己给的那句提示。
        # **"加载中"不能当失败词**: 右侧那块面板常驻这三个字, 拿它判等于永远红。
        "fail_extra": ["isn't set up yet", "Add LLM API key", "Accept the terms"],
        "why": "首启向导挡着 / 建对话报 LLM profile not found",
    },
}

DRIVER = r"""
import json, pathlib, re
from playwright.sync_api import sync_playwright

spec = json.loads(pathlib.Path("/work/spec.json").read_text())
out = pathlib.Path("/work/out"); out.mkdir(parents=True, exist_ok=True)
results = []


# 冷启动: 我们自己的启动页会轮询, 等它跳走。
def wait_started(page, url):
    # 两种"还没起来"长得完全不一样, 都要等:
    #  · 我们自己的启动页 (/work/starting) —— 会自己轮询然后跳走;
    #  · **网关 502** —— 实例还没听端口, Caddy 没人可转。这时页面是一张错误页,
    #    不会自己好, 得重新导航。开局撞上 502 就直接去找输入框, 只会等满超时
    #    然后报"点不动", 把一次冷启动误判成产品坏了 (刚为此白跑一轮)。
    # 5.8GB 的镜像就算命中缓存也要几分钟, 所以给到 6 分钟。
    for i in range(180):
        if "502" in (page.title() or "") or "Bad Gateway" in (page.content()[:2000] or ""):
            page.wait_for_timeout(4000)
            try:
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception:
                pass
            continue
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
            wait_started(page, prod["url"])
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
                # 占位符按产品给 —— 两家的文案差两个词, 写死一个就永远找不到另一个。
                box = page.get_by_placeholder(prod.get("placeholder") or "Type your message...")
                box.click(); box.fill(prod["send"])
                page.keyboard.press("Enter")
                # **等答案出现**, 不是干等一个固定秒数: 答得快就早走, 答得慢
                # (冷启动第一句) 也不会被腰斩。最多三分钟。
                want = ["".join(w.split()) for w in (prod.get("want") or [])]
                busy = prod.get("busy_hidden")
                for _ in range(36):
                    page.wait_for_timeout(5000)
                    got = want and all(w in "".join(page.inner_text("body").split()) for w in want)
                    if not got:
                        continue
                    # 有「这一轮还在跑」的标志就必须等它收起来 —— 否则可能在半路
                    # 截屏: 那时用量还是 0, 而看到的答案可能是上一轮留下的。
                    if not busy:
                        break
                    loc = page.locator(busy)
                    if loc.count() and loc.first.is_hidden():
                        break
                e["text"] = page.inner_text("body")[-1500:]

            elif kind == "openhands":
                # agent-canvas (all-in-one) 的首页就是一个输入框, 打字发出去即建
                # 对话 —— 不再有"沙箱"这一层 (小镜像那套是应用 + 子进程沙箱, 前端
                # 拿到的是 http://localhost:<端口>, 托管部署下浏览器连的是用户
                # 自己的机器, 永远连不上)。所以这里不再等沙箱, 也没有侧栏预热对话。
                #
                # 两个**实测**出来的坑, 都会让能用的产品看起来是坏的:
                #   1. 输入框不是 <textarea> 而是 contenteditable —— fill() 直接抛;
                #   2. **回车不发送**。敲完 Enter 那句话原地不动躺在框里, 页面
                #      一点没变, 看起来就像"发不出去"。要点右边那个圆形 ↑。
                box = page.locator("[contenteditable='true']").first
                box.click(timeout=60000)
                page.keyboard.type(prod["send"])
                page.wait_for_timeout(500)
                before = len(page.inner_text("body"))  # 只为末尾那句参考打印
                # 提交键没有可见文字, 按可及名字找; 找不到就回退到"输入框右边最近
                # 的那个按钮", 别用坐标猜。
                try:
                    page.get_by_role("button", name=re.compile("send|submit|发送", re.I)).first.click(timeout=8000)
                except Exception:
                    box.press("Meta+Enter")
                # 建对话 + 模型作答。冷启动时第一句慢, 给到 5 分钟。
                # **等的是那个答案, 不是"页面变长"**: 从首页跳进对话页会把首页
                # 那一大片 (推荐 agent、自动化) 卸掉, 文字净减少两千多字 —— 而那
                # 恰恰说明对话开起来了。拿长度判就是把能用的产品判成红的 (踩过)。
                want = ["".join(w.split()) for w in (prod.get("want") or [])]
                for _ in range(60):
                    page.wait_for_timeout(5000)
                    flat = "".join(page.inner_text("body").split())
                    if want and all(w in flat for w in want):
                        break
                page.wait_for_timeout(8000)
                e["text"] = page.inner_text("body")[-2000:]
                e["grew"] = len(page.inner_text("body")) - before

            page.screenshot(path=str(out / (prod["id"] + ".png")))
        except Exception as ex:
            e["error"] = f"{type(ex).__name__}: {ex}"[:300]
            try:
                page.screenshot(path=str(out / (prod["id"] + ".png")))
            except Exception:
                pass
        # **把这一轮真正问的那道题带回去**: 题目每次随机, 而判读那边看到的
        # USE 表里还是模板 ("{sum}") —— 不带回来就是拿模板去比对, 永远不匹配。
        e["want"] = prod.get("want")
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
        # **每次换一道题**。会话记录落在 NAS 上跨实例留着 —— 题目固定的话, 上一轮
        # 的答案就明晃晃挂在屏幕上, 这一轮什么都不干也能"验过"。2026-09-02 的
        # 截图里就是这样: 741 是上一轮留下的, 而这一轮还在跑。
        # 假绿比假红严重: 假红我会去看图, 假绿谁也不会再看。
        import random

        a, b = random.randint(211, 899), random.randint(211, 899)
        for pr in prods:
            for k in ("send", "type"):
                if isinstance(pr.get(k), str) and "{a}" in pr[k]:
                    pr[k] = pr[k].format(a=a, b=b, sum=a + b)
            if pr.get("want"):
                pr["want"] = [w.format(a=a, b=b, sum=a + b) if "{" in w else w for w in pr["want"]]
        print(f"==> 这轮的题: {a} + {b} = {a + b} (每次换 —— 上一轮的答案还在屏幕上)")

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
        # 优先用**这一轮真正问的那道题** (随机出的); 回退到表里的写法。
        want = e.get("want") or USE[pid].get("want")
        # 逐段查, 且**把空白全抹掉再比** —— 终端里的换行/折行不该算差异
        # (第一版拿整串比, 把 "USE-OK\n/opt/venv-..." 判成了失败, 而产品是好的)。
        flat = "".join(text.split())
        missing = [w for w in (want or []) if "".join(w.lower().split()) not in flat]
        grew = e.get("grew")
        # grew **只作参考, 不作判据**: 有的界面从首页跳进对话页会把首页那一大片
        # 内容卸掉, 页面文字净减少, 而那恰恰说明对话开起来了。判据一律落在
        # want 那几个"答案不可能出现在题面里"的字上。
        if grew is not None:
            print(f"    ({pid} 发送前后页面字数 {grew:+d} —— 仅供参考)")
        if hits:
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
