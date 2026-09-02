"""DSH Cloud 自研 agent 工作台的 HTTP/WS 层。

**没有账号体系, 也不该有。** 这个域前面压着我们自己的 forward_auth, 用户走到
这里已经登过一次了。再加一层等于第二道墙, 还会重演 Dify/Hermes/CloudCLI 那类
"会话过期就把人永久锁在登录页"的事故 —— 光 CloudCLI 一个, 为了绕开它自带的
账号体系我就写了四段补丁。容器每用户独占, 那才是隔离边界。

为什么不用现成的前端 (老板 2026-08-31 拍板自研):
  · 别人的界面里挂着别人的引流入口 (Star / Join Community / Report Issue),
    而用户付的是我们的钱;
  · **积分是我们的核心机制, 在别人的 UI 里没有位置** —— 余额、本轮消耗、
    剩余分钟, 这些只有自己写才放得进去;
  · 现成的那个 provider 里没有 Gemini, 四条线永远统一不了。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import shlex

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import adapters, sessions, workspace_fs

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"
WORKSPACE = os.environ.get("DSH_WORKSPACE", "/workspace")
GATEWAY = os.environ.get("DSH_GATEWAY_BASE", "https://dshcloud.online").rstrip("/")
CLOUD_TOKEN = os.environ.get("DSH_CLOUD_TOKEN", "")
PRODUCT_ID = os.environ.get("DSH_PRODUCT_ID", "")
#: 这台工作台默认驱动哪个 CLI。产品坑位不同, 值不同。
DEFAULT_CLI = os.environ.get("DSH_DEFAULT_CLI", "claude")
#: agent 子进程降权到哪个 uid/gid。0 或空 = 不降权 (本机开发时用)。
AGENT_UID = int(os.environ.get("DSH_AGENT_UID", "1000") or 0)
AGENT_GID = int(os.environ.get("DSH_AGENT_GID", str(AGENT_UID)) or 0)
#: agent 的 HOME。与 uid 配套 —— 指着 /root 的话它读不到自己的配置, 而症状是
#: "首跑向导又回来了"或者"模型没配"。
AGENT_HOME = os.environ.get("DSH_AGENT_HOME", "/home/agent")
#: 这台开放哪几个 CLI 供切换。留空 = 只有默认那个。
ENABLED_CLIS = [c for c in os.environ.get("DSH_ENABLED_CLIS", DEFAULT_CLI).split(",") if c.strip()]

app = FastAPI(title="DSH Cloud Agent")


def _agent_term_cmd() -> str:
    """终端标签页起来时先替用户敲的那条命令。

    **不能直接用 exe**: 有的接法 exe 是 Python 解释器 (OpenManus/CrewAI 走的是
    我们自己的 runner), 敲它只会掉进 Python REPL。由适配器自己说。
    """
    ad = adapters.ADAPTERS.get(DEFAULT_CLI)
    return ad().term_cmd if ad else "/usr/local/bin/claude"


def _drop(argv: list[str]) -> list[str]:
    """把 argv 包一层 setpriv, 降到非 root 跑。

    **为什么必须降权**: Claude Code 拒绝以 root 跑 bypassPermissions
    ("--dangerously-skip-permissions cannot be used with root/sudo privileges"),
    而不放开权限就得让用户逐条确认每次工具调用 —— 托管环境里那等于产品不能用。
    而我们又必须以 root 起服务 (NAS 挂进来的目录属主是 root)。

    **为什么用 setpriv 而不是 Python 的 user= 参数**: asyncio 的
    create_subprocess_exec 不吃 user/group (那是 subprocess.Popen 才有的),
    传了会 ValueError。而这个错发生在子进程启动前, 表现是整轮对话静默失败。

    失败症状很隐蔽: 进程起来了、一个字都不输出、退出码 0 —— 前端看到的只是
    "发了消息没反应"。
    """
    if not AGENT_UID:
        return argv
    return ["setpriv", f"--reuid={AGENT_UID}", f"--regid={AGENT_GID}",
            "--clear-groups", "--"] + argv


def _agent_env() -> dict:
    """给 agent 子进程的环境。

    HOME 必须跟着降权后的 uid 走 —— 继承 uvicorn 的 HOME=/root 的话, 它会去读
    一个自己没权限写的目录, 配置和会话都落不下来。
    """
    env = dict(os.environ)
    env["HOME"] = AGENT_HOME
    return env


@app.get("/api/health")
def health() -> dict:
    """就绪探针打这里。

    别拿首页当判据: 首页是静态文件, 后端没起来它照样 200 —— 探针那样写会在应用
    真正可用之前放人进来 (2026-08-30 Dify 与 Coze 都栽过这个)。
    """
    return {"ok": True, "cli": DEFAULT_CLI, "clis": ENABLED_CLIS}


@app.get("/api/config")
def get_config() -> dict:
    return {
        "cli": DEFAULT_CLI,
        "clis": [{"id": c, "name": adapters.ADAPTERS[c]().name} for c in ENABLED_CLIS if c in adapters.ADAPTERS],
        "workspace": WORKSPACE,
        "product": PRODUCT_ID,
    }


@app.get("/api/credits")
async def credits() -> dict:
    """余额与本期用量 —— 这是自研前端存在的理由之一, 别人的 UI 里没这个位置。

    读不到就返回 available=False 而不是抛错: 网关抖一下不该让整个界面变成错误页,
    用户该看到的是"余额暂时读不到", 而不是一个白屏。
    """
    if not CLOUD_TOKEN:
        return {"available": False, "reason": "未配置令牌"}
    url = f"{GATEWAY}/api/work/status"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, params={"product_id": PRODUCT_ID} if PRODUCT_ID else None,
                            headers={"Authorization": f"Bearer {CLOUD_TOKEN}"})
        if r.status_code != 200:
            return {"available": False, "reason": f"网关 {r.status_code}"}
        d = r.json()
        return {
            "available": True,
            "balance": d.get("balance"),
            "credits_per_min": d.get("credits_per_min"),
            "minutes_left": d.get("minutes_left"),
            "plan": d.get("plan_name"),
            "idle_stop_min": d.get("idle_stop_min"),
        }
    except (httpx.HTTPError, ValueError) as e:
        return {"available": False, "reason": type(e).__name__}


# ---- 会话 -------------------------------------------------------------------


@app.get("/api/sessions")
def api_sessions() -> dict:
    return {"sessions": sessions.list_sessions()}


@app.post("/api/sessions")
async def api_create(request: Request) -> dict:
    body = await request.json()
    cli = body.get("cli") or DEFAULT_CLI
    if cli not in adapters.ADAPTERS:
        return {"error": f"不认识的 CLI: {cli}"}
    return sessions.create(cli, body.get("title", ""))


@app.get("/api/sessions/{sid}/messages")
def api_messages(sid: str) -> dict:
    return {"messages": sessions.messages(sid)}


@app.delete("/api/sessions/{sid}")
def api_delete(sid: str) -> dict:
    sessions.delete(sid)
    return {"ok": True}


# ---- 文件与 git -------------------------------------------------------------


@app.get("/api/files")
def api_files(path: str = "") -> dict:
    try:
        return {"entries": workspace_fs.tree(path)}
    except ValueError as e:
        return {"error": str(e)}


@app.get("/api/file")
def api_file(path: str) -> dict:
    try:
        return workspace_fs.read(path)
    except ValueError as e:
        return {"error": str(e)}


@app.put("/api/file")
async def api_file_write(request: Request) -> dict:
    body = await request.json()
    try:
        return workspace_fs.write(body.get("path", ""), body.get("text", ""))
    except ValueError as e:
        return {"error": str(e)}


@app.get("/api/git/status")
def api_git_status() -> dict:
    return workspace_fs.git_status()


@app.get("/api/git/diff")
def api_git_diff(path: str = "") -> dict:
    try:
        return workspace_fs.git_diff(path)
    except ValueError as e:
        return {"error": str(e)}


@app.post("/api/git/commit")
async def api_git_commit(request: Request) -> dict:
    body = await request.json()
    return workspace_fs.git_commit(body.get("message", ""), body.get("paths"))


# ---- 对话 (WebSocket) -------------------------------------------------------


@app.websocket("/ws/chat/{sid}")
async def ws_chat(ws: WebSocket, sid: str) -> None:
    await ws.accept()
    row = sessions.get(sid)
    if row is None:
        await ws.send_json({"t": "error", "message": "会话不存在"})
        await ws.close()
        return

    proc: asyncio.subprocess.Process | None = None

    async def run_turn(prompt: str) -> None:
        nonlocal proc
        cur = sessions.get(sid) or row
        cli = cur.get("cli", DEFAULT_CLI)
        ad = adapters.ADAPTERS[cli]()
        argv = ad.argv(prompt, cur.get("cli_session") or None)
        payload = ad.stdin_payload(prompt)

        sessions.append(sid, {"role": "user", "text": prompt})
        await ws.send_json({"t": "user_echo", "text": prompt})

        proc = await asyncio.create_subprocess_exec(
            *_drop(argv),
            cwd=WORKSPACE,
            env=_agent_env(),
            stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            # stderr **单独收**, 不 merge 进 stdout: 这几个 CLI 的 stderr 里全是
            # 启动警告和路由日志, 混进来会把 JSON 流打断, 而适配器只会把它们当成
            # 认不出的行丢掉 —— 症状是"偶尔丢半句话"。
            stderr=asyncio.subprocess.PIPE,
        )
        if payload is not None and proc.stdin is not None:
            proc.stdin.write(payload.encode())
            await proc.stdin.drain()
            proc.stdin.close()

        collected: list[str] = []
        assert proc.stdout is not None

        async def drain_stderr() -> None:
            assert proc is not None and proc.stderr is not None
            async for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line and ("Error" in line or "error:" in line):
                    await ws.send_json({"t": "raw", "line": line[:400]})

        stderr_task = asyncio.create_task(drain_stderr())
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                for ev in ad.feed(line):
                    if ev["t"] == "session" and ev.get("id"):
                        sessions.update(sid, cli_session=ev["id"])
                    if ev["t"] in ("delta", "text"):
                        collected.append(ev.get("text", ""))
                    await ws.send_json(ev)
        finally:
            stderr_task.cancel()
            await proc.wait()

        text = "".join(collected).strip()
        if text:
            sessions.append(sid, {"role": "assistant", "text": text})
            cur2 = sessions.get(sid) or {}
            if cur2.get("title") in ("", "新会话"):
                sessions.update(sid, title=(sessions.messages(sid)[0]["text"][:28] or "新会话"))
        await ws.send_json({"t": "turn_end"})
        proc = None

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("t") == "send":
                text = (msg.get("text") or "").strip()
                if text:
                    try:
                        await run_turn(text)
                    except Exception as e:  # noqa: BLE001
                        # 一轮炸了不该把连接也带走 —— 否则用户看到的是"断线",
                        # 而真正的原因 (CLI 启动失败之类) 一个字都看不到。
                        await ws.send_json({"t": "error", "message": f"{type(e).__name__}: {e}"})
                        await ws.send_json({"t": "turn_end"})
            elif msg.get("t") == "stop" and proc is not None:
                proc.terminate()
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        if proc is not None:
            proc.terminate()


# ---- 终端 (反代 ttyd) -------------------------------------------------------
#
# 终端本身交给 ttyd (开源, libwebsockets + xterm.js), 我们只做转发。
#
# 先前是自己 pty.fork() + 往 WebSocket 上倒字节 —— 看着能跑, 实际反复出转义序列
# 的乱码 (先是满屏 `vvvv`, "修好"之后又变成满屏 `$$$$`)。终端仿真里"尺寸协商 +
# 转义序列 + 键盘编码"这几件事边角极多, 不值得我们自己趟。
#
# ttyd 绑回环, 由这里代理出去 —— 它自己带 --writable 之外没有鉴权, 而这个域
# 前面压着我们的 forward_auth, 容器又是每用户独占的。

TTYD_PORT = int(os.environ.get("DSH_TTYD_PORT", "7681"))
_ttyd_proc: asyncio.subprocess.Process | None = None


async def _ensure_ttyd() -> None:
    """按需起 ttyd。第一次点开「终端」标签页才起, 不用白占内存。"""
    global _ttyd_proc
    if _ttyd_proc is not None and _ttyd_proc.returncode is None:
        return
    argv = [
        "ttyd", "--port", str(TTYD_PORT), "--interface", "127.0.0.1",
        "--writable",
        # 客户端断开后别把 shell 也杀了 —— 用户切个标签页回来还是同一个会话。
        "--max-clients", "0",
        "--cwd", WORKSPACE,
        # **启动命令是必填的** —— 漏了它 ttyd 直接 "missing start command" 退出,
        # 而我们这边只看到反代 503。
        #
        # 直接进这一格对应的 agent, 不落一个要用户自己敲命令的 shell: 这个产品
        # 卖的就是"点开就能用", 让人先认出提示符再想起来敲 claude 是多一道坎。
        # agent 退出后 `exec bash -l` 兜住 —— 否则退出即断线, 用户想在同一个终端
        # 里跑个 git 都得重开标签页。
        # **先 `stty iutf8`**: ttyd 给的伪终端默认没开这一位, 于是退格在规范模式下
        # 只删**一个字节** —— 一个三字节的汉字被削掉一截, 剩下的不是合法 UTF-8,
        # Python 把它兑成代理字符, 发给网关时编不回去:
        #   'utf-8' codec can't encode character '\udce8': surrogates not allowed
        # 老板 2026-09-02 在 CrewAI 的终端里改了一个字就撞上了。整句中文没事,
        # 按过退格才炸 —— 真 PTY 里复现并验证过, 开了 IUTF8 就好。
        "bash", "-lc", f"stty iutf8 2>/dev/null; {_agent_term_cmd()}; exec bash -l",
    ]
    if AGENT_UID:
        # 与 agent 子进程同一个身份: 用户在终端里手敲 claude 时会撞上同一堵
        # "不能以 root 跑" 的墙, 两处不一致比两处都错更难查。
        argv = ["setpriv", f"--reuid={AGENT_UID}", f"--regid={AGENT_GID}",
                "--clear-groups", "--"] + argv
    _ttyd_proc = await asyncio.create_subprocess_exec(
        *argv, env=_agent_env(),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    # 等它真的开始监听 —— 立刻代理过去的话第一发必然 502。
    for _ in range(50):
        try:
            async with httpx.AsyncClient(timeout=1.0) as c:
                await c.get(f"http://127.0.0.1:{TTYD_PORT}/")
            return
        except httpx.HTTPError:
            await asyncio.sleep(0.1)


@app.get("/terminal")
@app.get("/terminal/{rest:path}")
async def terminal_proxy(rest: str = "") -> Response:
    await _ensure_ttyd()
    url = f"http://127.0.0.1:{TTYD_PORT}/{rest}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(url)
    except httpx.HTTPError as e:
        return PlainTextResponse(f"终端还没起来: {type(e).__name__}", status_code=503)
    # 原样带回内容类型, 否则 ttyd 的 js/css 会被当成 text/plain 而不执行。
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/octet-stream"),
    )


@app.websocket("/terminal/ws")
async def terminal_ws(ws: WebSocket) -> None:
    """双向转发 ttyd 的 WebSocket。

    **子协议必须带上**: ttyd 用 `tty` 这个子协议, 不回它的话浏览器端握手就失败,
    而症状只是终端一片空白 —— 看不出是握手挂了。
    """
    import websockets

    await _ensure_ttyd()
    await ws.accept(subprotocol="tty")
    url = f"ws://127.0.0.1:{TTYD_PORT}/ws"
    try:
        async with websockets.connect(url, subprotocols=["tty"], max_size=None) as up:
            async def c2s() -> None:
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if (b := msg.get("bytes")) is not None:
                        await up.send(b)
                    elif (t := msg.get("text")) is not None:
                        await up.send(t)

            async def s2c() -> None:
                async for data in up:
                    if isinstance(data, bytes):
                        await ws.send_bytes(data)
                    else:
                        await ws.send_text(data)

            done, pending = await asyncio.wait(
                [asyncio.create_task(c2s()), asyncio.create_task(s2c())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass


# ---- 静态页 (放最后: 它挂在 / 上, 会吃掉所有未匹配的路径) --------------------

app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def _asset_tag() -> str:
    """本次镜像里前端文件的指纹, 拼在静态资源 URL 后面。

    **不加这个的话新版发不出去**: app.js / style.css 是固定文件名, 内容改了
    URL 不变, 而 StaticFiles 默认发 max-age=14400 —— 浏览器四小时内一直用旧的。
    2026-08-31 实测踩到: 线上明明已经是 ttyd 版, 老板浏览器里跑的还是上一版
    手写终端, 报的是一句早就删掉的错误信息, 而两边都看不出是缓存。
    (同一天在 CloudCLI 那边踩的是它的孪生兄弟: 带令牌的页面被缓存重放。)
    """
    h = hashlib.sha256()
    for name in ("app.js", "style.css", "index.html"):
        f = WEB_DIR / name
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:12]


_ASSET_TAG = _asset_tag()


@app.get("/")
def index() -> HTMLResponse:
    html = (WEB_DIR / "index.html").read_text("utf-8")
    html = html.replace("/static/app.js", f"/static/app.js?v={_ASSET_TAG}")
    html = html.replace("/static/style.css", f"/static/style.css?v={_ASSET_TAG}")
    html = html.replace("/static/vendor/marked.js", f"/static/vendor/marked.js?v={_ASSET_TAG}")
    # 首页本身**永不缓存** —— 它是唯一带着指纹的入口, 它被缓存住的话指纹就永远
    # 更新不了, 加指纹这件事整个失效。
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
