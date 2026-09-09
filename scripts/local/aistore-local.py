#!/usr/bin/env python3
"""把 AI Store 的工作台跑在**你自己的机器**上, 账号和模型网关还是云端那一份。

    python3 aistore-local.py login          # 一次性: 用浏览器授权这台机器
    python3 aistore-local.py run codex      # 拉镜像 + 起容器, 打开 localhost:8080
    python3 aistore-local.py ps / stop <格>

这不是自部署。分工是:

    你的机器                        aistore.best
    ├─ 容器 (CPU/GPU 在这)          ├─ 账号与登录
    └─ 界面开在 localhost           ├─ 模型网关 (按积分计费)
                                    └─ 支付

**我们的服务器从不反向连接你的机器** —— 没有隧道、不用公网 IP、不用开端口。
鉴权走设备令牌 (与桌面端同一条 RFC 8628 流程, 可随时在网页上吊销), 容器里的
agent 拿着它调 aistore.best/llm/*, 用量照常记在你账上。

为什么要有这个: 一台 5090 或一台 Mac 闲着也是闲着, 而云端机时是要钱的。把容器
挪到本机, 省掉的是机时, 花的还是同一份积分 —— 而且不用为每个产品各注册一个
账号、各配一把 API key, 那些接线已经烧在镜像里了。

只依赖 Python 3 标准库和 docker。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("AISTORE_BASE", "https://aistore.best").rstrip("/")
STATE = pathlib.Path(os.environ.get("AISTORE_HOME", str(pathlib.Path.home() / ".aistore")))
TOKEN_FILE = STATE / "device.json"

#: 能在本机独立跑起来的格子。
#:
#: 只收**单容器 + 镜像公开**的。多容器栈 (Dify 11 个、Coze 10 个、Hermes 3 个)
#: 要 compose 编排, 不在这一版里; 镜像还没公开的那几个 (pi / langchain /
#: openmanus / openmausbot) 陌生人拉不动, 列出来只会让人白等一次 404。
#:
#: 端口取的是容器内的端口, 与 products.py 里那张表一致 —— 本机直接映到同号端口,
#: 不用子域: 我们线上需要一格一个子域, 是因为**一个 Caddy 前面压着 forward_auth
#: 要区分 16 个产品**; 你的笔记本上没有这个问题, 而产品前端用绝对路径引资源,
#: 落在 host:port 上反而最省事。
PRODUCTS = {
    "codex": {
        "image": "ghcr.io/agentsdancepro/agentui:0.2.5",
        "port": 8080,
        "desc": "Codex 编码智能体 (自研工作台外壳)",
        "agent": "codex",
    },
    "claude-code": {
        "image": "ghcr.io/agentsdancepro/agentui:0.2.5",
        "port": 8080,
        "desc": "Claude Code (同一个外壳, 换个 agent)",
        "agent": "claude",
    },
    "comfyui": {
        "image": "ghcr.io/agentsdancepro/comfy-local:v0.34.1-r16",
        "port": 8188,
        "desc": "节点编排生图/生视频 —— 有 N 卡才跑得动, Mac 上只有 CPU 那部分",
    },
    "open-design": {
        "image": "ghcr.io/agentsdancepro/od-local:0.21.0-r2",
        "port": 7456,
        "desc": "AI 设计智能体: 说想法出成品",
    },
    "autogen": {
        "image": "ghcr.io/agentsdancepro/autogen-studio:0.4.2-r3",
        "port": 8081,
        "desc": "AutoGen Studio 多智能体搭建台",
    },
}


def die(msg: str) -> None:
    print("!! " + msg, file=sys.stderr)
    raise SystemExit(1)


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


# ---- login ----------------------------------------------------------------


def cmd_login(_args) -> int:
    """RFC 8628 设备流程。令牌只落在本机, 权限 600。"""
    d = _post("/api/device/start", {"name": os.uname().nodename, "platform": sys.platform})
    print("\n  在浏览器里打开并确认:\n\n      %s\n" % d["verification_url"])
    print("  (等你点确认…… Ctrl-C 可中止)\n")
    deadline = time.time() + int(d.get("expires_in", 600))
    interval = max(2, int(d.get("interval", 3)))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            out = _post("/api/device/poll", {"device_code": d["device_code"]})
        except urllib.error.HTTPError as e:
            if e.code in (400, 428):  # 还没批准
                continue
            raise
        if out.get("token"):
            STATE.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(json.dumps({"token": out["token"], "base": BASE}))
            TOKEN_FILE.chmod(0o600)
            who = (out.get("user") or {}).get("email", "?")
            print("  ✓ 这台机器已授权给 %s" % who)
            print("    令牌存在 %s (600)。要撤销: 网页端「设备」里点吊销。" % TOKEN_FILE)
            return 0
    die("超时了 —— 十分钟内没等到确认")
    return 1


def _token() -> str:
    if not TOKEN_FILE.exists():
        die("还没授权这台机器, 先跑一次: aistore-local.py login")
    return json.loads(TOKEN_FILE.read_text())["token"]


# ---- run ------------------------------------------------------------------


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], text=True, capture_output=True, check=check)


def _container(slot: str) -> str:
    return "aistore-" + slot


def cmd_run(args) -> int:
    slot = args.product
    p = PRODUCTS.get(slot)
    if p is None:
        die("没有这一格: %s (能跑的: %s)" % (slot, ", ".join(sorted(PRODUCTS))))
    tok = _token()
    name, port = _container(slot), args.port or p["port"]

    # 同名容器还在就先收掉 —— 否则 docker run 直接报名字冲突, 而用户看到的
    # 只是一行红字, 不知道那是"上次那个还开着"。
    _docker("rm", "-f", name, check=False)

    # 镜像目前只出 linux/amd64。Apple Silicon 上 docker 会自动用模拟跑, 能用但慢;
    # 不说一声的话, 用户只会觉得"这东西怎么这么卡"而不知道是模拟。
    if os.uname().machine in ("arm64", "aarch64"):
        print("  提示: 镜像只有 linux/amd64, 这台机器是 arm64 —— 会走模拟, 明显更慢。")
        print("        x86 的机器 (比如装 5090 那台) 是原生跑。\n")

    print("==> 拉镜像 %s (第一次会久一点)" % p["image"])
    if subprocess.run(["docker", "pull", p["image"]]).returncode != 0:
        die("拉不动。这几个镜像里有一部分还没设成公开, 见 README 的「镜像可见性」。")

    env = {
        # 网关: agent 在容器里调的就是这个地址, 用量记在你账上。
        "DSH_GATEWAY_BASE": BASE,
        "OPENAI_BASE_URL": BASE + "/llm/v1",
        "OPENAI_API_KEY": tok,
        "ANTHROPIC_BASE_URL": BASE + "/llm/anthropic",
        "ANTHROPIC_AUTH_TOKEN": tok,
        # 工作台自己的 HOME 与工作目录, 都落在下面那个卷上。
        "HOME": "/home/agent",
    }
    if p.get("agent"):
        env["DSH_DEFAULT_CLI"] = p["agent"]

    vol = "aistore-%s-data" % slot
    cmd = ["run", "-d", "--name", name, "-p", "%d:%d" % (port, p["port"]), "-v", "%s:/home/agent" % vol]
    for k, v in env.items():
        cmd += ["-e", "%s=%s" % (k, v)]
    cmd.append(p["image"])

    print("==> 起容器")
    r = _docker(*cmd, check=False)
    if r.returncode != 0:
        die("起不来:\n" + (r.stderr or r.stdout))
    print("\n  ✓ %s 跑起来了\n\n      http://localhost:%d\n" % (slot, port))
    print("    日志: docker logs -f %s" % name)
    print("    收工: python3 %s stop %s" % (pathlib.Path(__file__).name, slot))
    return 0


def cmd_stop(args) -> int:
    n = _container(args.product)
    r = _docker("rm", "-f", n, check=False)
    print("已停 " + n if r.returncode == 0 else "没有在跑的 " + n)
    return 0


def cmd_ps(_args) -> int:
    r = _docker("ps", "--filter", "name=aistore-", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}", check=False)
    print(r.stdout.strip() or "(本机没有在跑的格子)")
    return 0


def cmd_list(_args) -> int:
    print("能在本机跑的格子:\n")
    for k, v in sorted(PRODUCTS.items()):
        print("  %-13s :%-5d %s" % (k, v["port"], v["desc"]))
    print("\n多容器栈 (Dify / Coze / Hermes) 和镜像未公开的几格暂不在列, 见 README。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="在自己的机器上跑 AI Store 的工作台")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="用浏览器授权这台机器").set_defaults(fn=cmd_login)
    r = sub.add_parser("run", help="起一格")
    r.add_argument("product")
    r.add_argument("--port", type=int, default=0, help="换个本机端口")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("stop", help="停一格")
    s.add_argument("product")
    s.set_defaults(fn=cmd_stop)
    sub.add_parser("ps", help="本机在跑哪些").set_defaults(fn=cmd_ps)
    sub.add_parser("list", help="有哪些格子能跑").set_defaults(fn=cmd_list)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
