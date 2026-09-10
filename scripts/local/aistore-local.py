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


#: 请求头里的 User-Agent。
#:
#: **不能用 urllib 的默认值**: 站点在 Cloudflare 后面, 而 `Python-urllib/3.x` 会被
#: 直接挡掉 —— 返回 403 与一句 `error code: 1010`, 既不是 JSON 也没有任何提示。
#: 2026-09-10 实测: 同一把令牌, curl 200 / urllib 403。这个脚本是 README 里写给
#: 陌生人的第一条命令, 撞上的话第一步就走不下去, 而报错会指向令牌 —— 指错方向。
USER_AGENT = "aistore-local/1.0 (+https://aistore.best)"


def _req(path: str, body: dict | None = None, token: str = "") -> dict:
    headers = {"content-type": "application/json", "user-agent": USER_AGENT}
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
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
        try:
            detail = json.loads(raw).get("detail", "")
        except Exception:  # noqa: BLE001
            # 非 JSON 的错误体也要留着 —— Cloudflare 的拦截页就是纯文本, 丢掉它
            # 等于把"被挡了"读成"令牌不对"。
            detail = raw.decode("utf-8", "replace")[:200].strip()
        if e.code == 403 and "1010" in detail:
            die("被 Cloudflare 挡了 (error 1010) —— 通常是 User-Agent 被拦。请更新这个脚本。")
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


def _sidecar_name(slot: str, name: str) -> str:
    """伴随容器的名字。双横线分隔, 好让 stop 按一个正则收干净整栈,
    又不会误伤名字前缀相同的另一格 (aistore-dify 与 aistore-dify-x)。"""
    return f"{_container(slot)}--{name}"


def cmd_run(args) -> int:
    slot = args.product
    tok = _token()
    plan = _api("/api/local/plan/" + slot)
    if plan["runnable"] != "ready":
        die("这一格现在起不动: {}".format(plan["reason"]))
    ph = plan["token_placeholder"]
    main = next(c for c in plan["containers"] if c["role"] == "main")
    inits = [c for c in plan["containers"] if c["role"] == "init"]
    sides = [c for c in plan["containers"] if c["role"] == "sidecar"]
    name, port = _container(slot), args.port or plan["port"]

    # 整栈先收干净 —— 否则 docker 只回一句名字冲突, 而用户看到的是一行看不懂的
    # 红字, 不知道那是"上次那个还开着"。
    _stop_stack(slot)

    if os.uname().machine in ("arm64", "aarch64"):
        print("  提示: 我们自己的工作台镜像只有 linux/amd64 —— 这台机器会走模拟, 明显更慢。")
        print("        x86 的机器 (比如装 5090 那台) 是原生跑。\n")

    # 逐个镜像判架构, 不一刀切: 栈里的上游中间件 (postgres/redis) 多是多架构的,
    # 一刀切给它们扣上 amd64 等于让本来能原生跑的东西白白走模拟。
    platforms = {}
    for c in plan["containers"]:
        if c["image_ref"] in platforms:
            continue
        print(f"==> 拉镜像 {c['image_ref']}")
        platforms[c["image_ref"]] = _pull(c["image_ref"])

    env = {k: _fill(v, tok, ph) for k, v in (main.get("env") or {}).items()}
    home = env.get("DSH_AGENT_HOME") or env.get("HOME") or "/home/agent"

    # 1. 初始化容器逐个跑完 —— 不能并行, 它们之间就是靠顺序保证的
    for ic in inits:
        cmd = ["run", "--rm", "--name", _sidecar_name(slot, "init-" + ic["name"])]
        if platforms.get(ic["image_ref"]):
            cmd += ["--platform", platforms[ic["image_ref"]]]
        cmd += ["-v", f"aistore-{slot}-data:{home}", ic["image_ref"]]
        cmd += [_fill(a, tok, ph) for a in (ic.get("cmd") or [])]
        print(f"==> 初始化 {ic['name']}")
        r = _docker(*cmd, check=False)
        if r.returncode != 0:
            die(f"初始化容器 {ic['name']} 失败:\n" + (r.stderr or r.stdout))

    # 2. 主容器: 它建网络命名空间, 端口也只有它映射
    cmd = ["run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:{plan['port']}"]
    if platforms.get(main["image_ref"]):
        cmd += ["--platform", platforms[main["image_ref"]]]
    cmd += ["-v", f"aistore-{slot}-data:{home}"]
    if main.get("run_as_user") is not None:
        cmd += ["--user", str(main["run_as_user"])]
    # 只有主容器能加 host 映射: 伴随容器用 --network container: 加进来之后,
    # docker 会拒绝 --add-host。上游那些配置把服务名当主机名用, 共享命名空间里
    # 都指回环。
    for alias in plan.get("host_aliases") or []:
        cmd += ["--add-host", f"{alias}:127.0.0.1"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd.append(main["image_ref"])
    cmd += [_fill(a, tok, ph) for a in (main.get("cmd") or [])]
    print(f"==> 起 {1 + len(sides)} 个容器" if sides else "==> 起容器")
    r = _docker(*cmd, check=False)
    if r.returncode != 0:
        die("起不来:\n" + (r.stderr or r.stdout))

    # 3. 伴随容器加入主容器的网络命名空间 (与云端的 pod 语义一致)。
    # 不接同一个 bridge 再靠 DNS: 上游那些栈的配置里全是 127.0.0.1。
    for sc in sides:
        cmd = ["run", "-d", "--name", _sidecar_name(slot, sc["name"]), "--network", f"container:{name}"]
        if platforms.get(sc["image_ref"]):
            cmd += ["--platform", platforms[sc["image_ref"]]]
        for k, v in (sc.get("env") or {}).items():
            cmd += ["-e", f"{k}={_fill(v, tok, ph)}"]
        cmd.append(sc["image_ref"])
        # cmd 顶掉 entrypoint, args 不顶 —— 顺序不能反
        cmd += [_fill(a, tok, ph) for a in (sc.get("cmd") or [])]
        cmd += [_fill(a, tok, ph) for a in (sc.get("args") or [])]
        r = _docker(*cmd, check=False)
        if r.returncode != 0:
            _stop_stack(slot)
            die(f"伴随容器 {sc['name']} 起不来:\n" + (r.stderr or r.stdout))

    print(f"\n  ✓ {slot} 跑起来了\n\n      http://localhost:{port}\n")
    print(f"    就绪探针: http://localhost:{port}{plan['ready_path']}")
    print(f"    日志: docker logs -f {name}")
    print(f"    收工: python3 {pathlib.Path(__file__).name} stop {slot}")
    return 0


def _stop_stack(slot: str) -> int:
    """收掉这一格的整栈。按名字正则匹配, 两端锚定, 免得停 dify 顺手带走 dify-x。"""
    r = _docker("ps", "-aq", "--filter", f"name=^{_container(slot)}(--.*)?$", check=False)
    ids = [x for x in (r.stdout or "").split() if x]
    if ids:
        _docker("rm", "-f", *ids, check=False)
    return len(ids)


def cmd_stop(args) -> int:
    n = _stop_stack(args.product)
    print(f"已停 {_container(args.product)} ({n} 个容器)" if n else "没有在跑的 " + _container(args.product))
    return 0


def cmd_ps(_args) -> int:
    r = _docker(
        "ps", "--filter", "name=aistore-", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}", check=False
    )
    rows = [ln for ln in (r.stdout or "").splitlines() if "--" not in ln.split("\t")[0]]
    print("\n".join(rows) or "(本机没有在跑的格子)")
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
