#!/usr/bin/env python3
"""把 AI Store 的工作台跑在**你自己的机器**上, 账号和模型网关还是云端那一份。

    python3 aistore-local.py login          # 一次性: 用浏览器授权这台机器
    python3 aistore-local.py list           # 有哪些格子能跑
    python3 aistore-local.py run codex      # 拉镜像 + 起容器, 打开 localhost
    python3 aistore-local.py ps / stop <格>

这不是自部署。分工是:

    你的机器                        aistore.best
    ├─ 容器 (CPU/GPU 在这)          ├─ 账号与登录
    └─ 界面开在 localhost           ├─ 模型网关 (按积分计费)
                                    └─ 支付

**我们的服务器从不反向连接你的机器** —— 没有隧道、不用公网 IP、不用开端口。
鉴权走设备令牌 (与桌面端同一条 RFC 8628 流程, 可随时在网页上吊销), 容器里的
agent 拿着它调 aistore.best/llm/*, 用量照常记在你账上。

**目录和编排都从服务端取** (`/api/local/catalog`、`/api/local/plan/<格>`)。
这个脚本以前硬编码着五格和它们的镜像 tag —— 镜像每次重建都在动 (2026-09-10 一天
五个), 而抄一份的下场不是报错, 是拉到一个过期镜像然后一切"正常"。现在这里只剩
一个执行器: 服务端说起什么, 它就起什么。

计划里不含任何凭据, 令牌位置是 `${AISTORE_TOKEN}` 占位符, 由这里替换成本机存的
那一个。只依赖 Python 3 标准库和 docker。
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


def die(msg: str) -> None:
    print("!! " + msg, file=sys.stderr)
    raise SystemExit(1)


def _req(path: str, body: dict | None = None, token: str = "") -> dict:
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ---- login ----------------------------------------------------------------


def cmd_login(_args) -> int:
    """RFC 8628 设备流程。令牌只落在本机, 权限 600。"""
    d = _req("/api/device/start", {"name": os.uname().nodename, "platform": sys.platform})
    print("\n  在浏览器里打开并确认:\n\n      {}\n".format(d["verification_url"]))
    print("  (等你点确认…… Ctrl-C 可中止)\n")
    deadline = time.time() + int(d.get("expires_in", 600))
    interval = max(2, int(d.get("interval", 3)))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            out = _req("/api/device/poll", {"device_code": d["device_code"]})
        except urllib.error.HTTPError as e:
            if e.code in (400, 428):  # 还没批准
                continue
            raise
        if out.get("token"):
            STATE.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(json.dumps({"token": out["token"], "base": BASE}))
            TOKEN_FILE.chmod(0o600)
            who = (out.get("user") or {}).get("email", "?")
            print(f"  ✓ 这台机器已授权给 {who}")
            print(f"    令牌存在 {TOKEN_FILE} (600)。要撤销: 网页端「设备」里点吊销。")
            return 0
    die("超时了 —— 十分钟内没等到确认")
    return 1


def _token() -> str:
    if not TOKEN_FILE.exists():
        die("还没授权这台机器, 先跑一次: aistore-local.py login")
    return json.loads(TOKEN_FILE.read_text())["token"]


def _api(path: str) -> dict:
    try:
        return _req(path, token=_token())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("detail", "")
        except Exception:  # noqa: BLE001
            pass
        if e.code in (401, 403):
            die("令牌不认了 (可能已在网页端吊销)。重新跑一次 login。")
        die(f"服务端拒绝了这个请求: HTTP {e.code} {detail}")
        raise


# ---- run ------------------------------------------------------------------


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], text=True, capture_output=True, check=check)


def _container(slot: str) -> str:
    return "aistore-" + slot


def _fill(value: str, token: str, placeholder: str) -> str:
    return value.replace(placeholder, token)


def cmd_list(_args) -> int:
    body = _api("/api/local/catalog")
    ready = [p for p in body["products"] if p["runnable"] == "ready" and not p["locked"]]
    rest = [p for p in body["products"] if p not in ready]
    print("能在本机跑的格子:\n")
    for p in ready:
        print(f"  {p['id']:<13} :{p['port']:<5} {p['name']}")
    if rest:
        print("\n暂时跑不了的:\n")
        for p in rest:
            why = "要先买通行证" if p["locked"] else p["reason"]
            print(f"  {p['id']:<13} {why}")
    print(f"\n网关: {body['gateway']}")
    return 0


def _pull(image: str) -> str:
    """拉镜像; 返回跑它要用的 --platform (空串表示本机原生)。

    先按原生拉, 拉不动再退回 linux/amd64 —— 不一上来就写死, 是因为哪天我们出了
    arm64 镜像, 写死的那版会让 M 系机器继续白白走模拟。

    必须有这个回退: Apple Silicon 上拉一个只有 amd64 manifest 的镜像**会直接失败**,
    不是"慢一点" —— `no matching manifest for linux/arm64/v8`。
    """
    r = subprocess.run(["docker", "pull", image], capture_output=True, text=True)
    if r.returncode == 0:
        print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")
        return ""
    err = (r.stderr or "") + (r.stdout or "")
    if "no matching manifest" not in err and "no match for platform" not in err:
        print(err.strip()[-500:], file=sys.stderr)
        die("拉不动。镜像可见性见 README, 网络问题请重试。")
    print("  本机架构没有原生镜像, 改用 linux/amd64 模拟运行 (会慢一些)")
    if subprocess.run(["docker", "pull", "--platform", "linux/amd64", image]).returncode != 0:
        die("模拟架构也拉不动 —— 确认 Docker Desktop 里开了 Rosetta / 多架构支持。")
    return "linux/amd64"


def cmd_run(args) -> int:
    slot = args.product
    tok = _token()
    plan = _api("/api/local/plan/" + slot)
    if plan["runnable"] != "ready":
        die("这一格现在起不动: {}".format(plan["reason"]))
    ph = plan["token_placeholder"]
    main = next(c for c in plan["containers"] if c["role"] == "main")
    name, port = _container(slot), args.port or plan["port"]

    # 同名容器还在就先收掉 —— 否则 docker run 直接报名字冲突, 而用户看到的
    # 只是一行红字, 不知道那是"上次那个还开着"。
    _docker("rm", "-f", name, check=False)

    # 镜像目前只出 linux/amd64。Apple Silicon 上 docker 会自动用模拟跑, 能用但慢;
    # 不说一声的话, 用户只会觉得"这东西怎么这么卡"而不知道是模拟。
    if os.uname().machine in ("arm64", "aarch64"):
        print("  提示: 镜像只有 linux/amd64, 这台机器是 arm64 —— 会走模拟, 明显更慢。")
        print("        x86 的机器 (比如装 5090 那台) 是原生跑。\n")

    image = main["image_ref"]
    print(f"==> 拉镜像 {image} (第一次会久一点)")
    platform = _pull(image)

    env = {k: _fill(v, tok, ph) for k, v in (main.get("env") or {}).items()}
    home = env.get("DSH_AGENT_HOME") or env.get("HOME") or "/home/agent"
    cmd = ["run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:{plan['port']}"]
    # 拉的时候用了哪个 platform, 跑的时候必须一致 —— 否则 docker 会去找一个本机
    # 架构的镜像, 而那个镜像根本不存在。
    if platform:
        cmd += ["--platform", platform]
    cmd += ["-v", f"aistore-{slot}-data:{home}"]
    if main.get("run_as_user") is not None:
        cmd += ["--user", str(main["run_as_user"])]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd.append(image)
    cmd += [_fill(a, tok, ph) for a in (main.get("cmd") or [])]

    print("==> 起容器")
    r = _docker(*cmd, check=False)
    if r.returncode != 0:
        die("起不来:\n" + (r.stderr or r.stdout))
    print(f"\n  ✓ {slot} 跑起来了\n\n      http://localhost:{port}{plan['ready_path']}\n")
    print(f"    日志: docker logs -f {name}")
    print(f"    收工: python3 {pathlib.Path(__file__).name} stop {slot}")
    return 0


def cmd_stop(args) -> int:
    n = _container(args.product)
    r = _docker("rm", "-f", n, check=False)
    print("已停 " + n if r.returncode == 0 else "没有在跑的 " + n)
    return 0


def cmd_ps(_args) -> int:
    r = _docker(
        "ps", "--filter", "name=aistore-", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}", check=False
    )
    print(r.stdout.strip() or "(本机没有在跑的格子)")
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
