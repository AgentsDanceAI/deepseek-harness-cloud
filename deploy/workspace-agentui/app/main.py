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
import json
import os
import pathlib
import shlex

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
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


# ---- 终端 (WebSocket + PTY) -------------------------------------------------


@app.websocket("/ws/shell")
async def ws_shell(ws: WebSocket) -> None:
    """一个真 PTY。没有它, `top`/`vim`/带颜色的输出全是坏的。"""
    import fcntl
    import pty
    import signal
    import struct
    import termios

    await ws.accept()
    pid, fd = pty.fork()
    if pid == 0:  # 子进程
        # 与对话那条一样降权: 用户在终端里手敲 `claude` 时会撞上同一堵
        # "不能以 root 跑 bypassPermissions" 的墙, 两处不一致比两处都错更难查。
        if AGENT_UID:
            try:
                os.setgid(AGENT_GID)
                os.setuid(AGENT_UID)
            except OSError:
                pass
        os.environ["HOME"] = AGENT_HOME if AGENT_UID else os.environ.get("HOME", "/root")
        os.chdir(WORKSPACE)
        os.execvp("/bin/bash", ["/bin/bash", "-l"])
        os._exit(1)  # pragma: no cover

    loop = asyncio.get_running_loop()

    async def pump() -> None:
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, fd, 65536)
            except OSError:
                break
            if not data:
                break
            await ws.send_text(data.decode("utf-8", "replace"))

    task = asyncio.create_task(pump())
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("t") == "in":
                os.write(fd, msg.get("data", "").encode())
            elif msg.get("t") == "resize":
                # 不同步窗口大小的话, 任何全屏程序 (vim/htop) 的画面都是错位的。
                winsize = struct.pack("HHHH", int(msg.get("rows", 24)), int(msg.get("cols", 80)), 0, 0)
                fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except (WebSocketDisconnect, json.JSONDecodeError, OSError):
        pass
    finally:
        task.cancel()
        try:
            os.kill(pid, signal.SIGKILL)
            os.close(fd)
        except OSError:
            pass


# ---- 静态页 (放最后: 它挂在 / 上, 会吃掉所有未匹配的路径) --------------------

app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
