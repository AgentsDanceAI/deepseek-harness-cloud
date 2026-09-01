"""agent-canvas 的开局: 定死会话密钥 -> 灌模型设置 -> 免掉首启向导。

为什么换成这个 all-in-one 镜像 (小镜像那条路走不通):
小镜像是"应用 + 子进程沙箱"的形态, 前端拿**应用给的 agent server 地址**去拼
WebSocket, 而那个地址是 `http://localhost:<port>` —— 在用户自己电脑上跑成立
(端口就在本机), 托管部署时浏览器的 localhost 是用户自己的机器。这是那个模式的
固有假设, 不是接线问题。all-in-one 把 agent server 放进同一个进程, 没有沙箱
概念 (它的 openapi 里一个 /api/sandboxes 都没有), 前端与它同源。

三件事:
1. **密钥先写死**。它的 API 认 X-Session-API-Key, 密钥是首次启动时随机生成落在
   agent-canvas/api-key.txt 里的。我们先写好, 服务端和注入给前端的就是同一把。
2. 用这把密钥灌模型设置 —— 灌完**回读校验**, 返回 200 不等于生效 (小镜像那边
   就是这么骗过我一轮的)。
3. 首启向导记在**浏览器 localStorage**, 服务端预置不掉 —— 往它的 index.html 里
   注入一段脚本把键种上。遥测种成拒绝, 不是同意。
"""
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

HOME = pathlib.Path("/home/openhands/.openhands")
KEY = os.environ.get("DSH_AC_KEY") or "dsh" + os.urandom(16).hex()
BASE = "http://127.0.0.1:8000"


def seed_key() -> None:
    """密钥不用我们写文件 —— 它的 entrypoint 自带 `LOCAL_BACKEND_API_KEY` 这个口子。

    先前我往 agent-canvas/api-key.txt 里写, 应用照样回 401: 那个文件只在
    **两个环境变量都为空**时才被读 (见 entrypoint.sh 的 if), 而且时序上也难保
    写在应用读之前。用环境变量是官方支持的做法, 少一层猜测。
    这个函数留着只为把状态目录建好并交回应用用户 —— 这一步以 root 跑 (要改
    /opt 下的前端), root 建出来的目录应用用户写不动, 而它只会在日志里抱怨一句
    然后照常起来, 于是用户的东西全丢 (同一个坑 agentui 那边踩过)。
    """
    import pwd

    d = HOME / "agent-canvas"
    d.mkdir(parents=True, exist_ok=True)
    try:
        u = pwd.getpwnam("openhands")
        for path in (HOME, d):
            os.chown(path, u.pw_uid, u.pw_gid)
    except (KeyError, PermissionError):
        pass


def call(path, data=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "X-Session-API-Key": KEY},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
        return json.loads(body) if body.strip() else {}


def inject_frontend() -> None:
    """把免向导的键种进 index.html —— 那是浏览器侧的状态, 服务端灌不进去。"""
    # 这几个键是**实测抓的**: 点完向导之后浏览器里存的就是这些 (照抄, 不臆造)。
    # · openhands-onboarded=1 免掉三步向导;
    # · agent-canvas-consent=1 + telemetry-consent 免掉遥测同意框 —— 注意应用
    #   启动时会把 consent 重置成 '0', 所以还要清掉 first-use 那个标记, 否则框
    #   照样弹 (第一版只种 consent, 白种了)。
    # 遥测种成 denied 而不是 granted: 隐私偏好一律选最保守的。
    script = (
        "<script>try{"
        "localStorage.setItem('openhands-onboarded','1');"
        "localStorage.setItem('agent-canvas-consent','1');"
        "localStorage.setItem('openhands-telemetry-consent','denied');"
        "localStorage.removeItem('openhands-telemetry-first-use');"
        "}catch(e){}</script>"
    )
    hits = 0
    for p in pathlib.Path("/opt/agent-canvas/frontend").rglob("index.html"):
        s = p.read_text()
        if "openhands-onboarded" in s:
            hits += 1
            continue
        p.write_text(s.replace("</head>", script + "</head>", 1)
                     if "</head>" in s else script + s)
        hits += 1
    print(f"[dsh] 首启向导已免掉 ({hits} 个 index.html)")


def main() -> None:
    # **这里不再碰密钥和前端** —— 那两件事在应用启动**之前**由 pre 阶段做完了
    # (启动脚本里单独调 seed_key/inject_frontend)。在这儿重跑一遍会把应用已经
    # 读进内存的密钥覆盖掉, 于是后面调接口一律 401 (实测撞到)。
    # 等应用真正起来。**401 不能当成"密钥错了"就退出** —— 应用启动早期
    # (还没读到密钥文件时) 也会回 401, 而那时退出等于把"起得慢"当成"配错了",
    # 于是模型档案永远灌不上, 界面一直说 LLM 没配好。实测撞到两轮。
    ready = False
    for _ in range(180):
        try:
            call("/api/settings")
            ready = True
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:        # 还没有设置记录, 但服务在应答
                ready = True
                break
            time.sleep(2)            # 401/403: 多半还在启动, 再等
        except (urllib.error.URLError, OSError):
            time.sleep(2)
    if not ready:
        raise SystemExit("[dsh] 等不到应用应答 —— 模型档案没灌上")

    model, base = os.environ["DSH_LLM_MODEL"], os.environ["DSH_LLM_BASE"]

    # **模型走 LLM 档案 (/api/profiles), 不是 agent_settings.llm**。
    # 灌进 agent_settings.llm 会回读成功、界面也把"Add LLM API key"打上勾, 但建
    # 对话时报 `LLM profile 'default' not found` —— 而界面上那句
    # "Your LLM isn't set up yet" 从头到尾都是对的, 是我灌错了地方。
    call("/api/profiles/default", {
        "llm": {"model": model, "base_url": base, "api_key": os.environ["DSH_CLOUD_TOKEN"]}
    })
    call("/api/profiles/default/activate", {})

    got = call("/api/profiles")
    prof = next((p for p in got.get("profiles", []) if p.get("name") == "default"), {})
    ok = (prof.get("model") == model and prof.get("base_url") == base
          and prof.get("api_key_set") and got.get("active_profile") == "default")
    print(f"[dsh] 模型档案{'已生效' if ok else '**没生效**'}: {prof.get('model')} @ {prof.get('base_url')}")

    # 遥测关掉 —— 隐私偏好一律选最保守的。
    try:
        call("/api/settings", {"misc_settings_diff": {"user_consents_to_analytics": False}})
    except urllib.error.HTTPError:
        pass


main()
