"""自研智能体工作台 (Claude Code / Codex 两格) 的接线守卫。

两格跑同一个镜像 (deploy/workspace-agentui), 区别只在默认驱动哪个 CLI。
这里钉的全是**静默错法** —— 钉错型号是每次 404, 属主没交是回收后才发现东西没了,
探针打首页是把人放进坏页面, 每一条都不会在日志里说自己错了。
"""

from __future__ import annotations

import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="dhc-agentui-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import model_catalog, products

SLOTS = ("claude-code", "codex")


@pytest.mark.parametrize("product_id", SLOTS)
def test_default_model_is_actually_on_sale(product_id):
    """网关只放行在售目录里的型号 —— 钉一个不在售的等于这个产品每次都 404。

    而这些 CLI 各自的内置默认值都是厂商自己的型号名 (Claude Code 要 haiku,
    Codex 要 gpt-5-codex), 一个都不在我们的目录里, 所以**必须**钉死。
    """
    env = products.env_for(product_id, "tok")
    assert model_catalog.resolve(env["ANTHROPIC_MODEL"]) is not None


@pytest.mark.parametrize("product_id", SLOTS)
def test_small_fast_model_is_pinned_too(product_id):
    """不设 SMALL_FAST 的话后台任务去要 haiku —— 我们不卖那个名字, 于是全 404。"""
    env = products.env_for(product_id, "tok")
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == env["ANTHROPIC_MODEL"]


@pytest.mark.parametrize("product_id", SLOTS)
def test_state_lands_on_the_nas(product_id):
    """会话清单与 agent 的 HOME 都必须落在 NAS 挂载点下。

    NAS 只挂在 /root 与 /workspace 两处 (workbackend 的 VolumeMount)。指别处的话
    会话、CLI 登录态全在容器里, 闲置回收一删就没 —— 而这个错法不报任何错, 用户
    只会发现昨天的东西不见了。
    """
    env = products.env_for(product_id, "tok")
    assert env["DSH_WORKSPACE"] == "/workspace"
    assert env["DSH_STATE_DIR"].startswith("/home/agent")
    # /home/agent 本身不在 NAS 上, 所以启动脚本必须把它建在挂载点里或交属主 ——
    # 见 test_boot_hands_the_mounts_to_the_agent_user。
    assert "/workspace" in products.boot_script(product_id)


@pytest.mark.parametrize("product_id", SLOTS)
def test_boot_hands_the_mounts_to_the_agent_user(product_id):
    """启动脚本必须把挂载点的属主交给 agent 用户。

    Claude Code **拒绝以 root** 跑放开权限的模式 ("--dangerously-skip-permissions
    cannot be used with root/sudo privileges"), 所以 agent 子进程降权到 uid 1000。
    而 NAS 挂进来的目录属主是 root —— 不交过去的话 agent 连自己的会话文件都写不
    进去, 它只在日志里抱怨一句然后照常跑, 用户是回收之后才发现东西没了。
    """
    boot = products.boot_script(product_id)
    # **/workspace 必须点名**。原先只断言 "chown -R 1000:1000" 出现过 —— 而脚本
    # 里还有一处只交 home 的 chown, 于是"漏掉 /workspace"这个变异照样能过。
    # 漏掉它的后果最重: 用户的代码目录 agent 写不进去。
    assert "chown -R 1000:1000" in boot and "/workspace" in boot.split("chown -R 1000:1000")[1].split("\n")[0]
    env = products.env_for(product_id, "tok")
    assert env["DSH_AGENT_UID"] == "1000"


@pytest.mark.parametrize("product_id", SLOTS)
def test_ready_path_is_not_the_front_page(product_id):
    """首页是静态文件, 后端没起来它照样 200。

    探针打首页 = 用户会在后端就绪前被放进一个坏页面 (2026-08-30 Dify 那次)。
    """
    assert products.registry()[product_id].ready_path.startswith("/api/")


@pytest.mark.parametrize("product_id", SLOTS)
def test_credits_are_wired(product_id):
    """余额/本轮消耗是这个自研前端**存在的理由之一** —— 少了令牌就显示不出来。

    别人的 UI 里根本没有这个位置, 这也是顶掉 CloudCLI 的主因。
    """
    env = products.env_for(product_id, "tok")
    assert env["DSH_CLOUD_TOKEN"] == "tok"
    assert env["DSH_GATEWAY_BASE"]
    assert env["DSH_PRODUCT_ID"] == product_id


@pytest.mark.parametrize("product_id", SLOTS)
def test_no_login_wall_anywhere(product_id):
    """自研的前提之一: 这个域前面压着我们的 forward_auth, 里面不该再有账号体系。"""
    boot = products.boot_script(product_id)
    for banned in ("auth-token", "autologin", "--auth password", "login"):
        assert banned not in boot, f"启动脚本里冒出了 {banned} —— 是不是又加了登录墙"


def test_each_slot_drives_its_own_cli():
    """两格的默认 CLI 必须不同, 否则"两个产品"其实是同一个。"""
    a = products.env_for("claude-code", "t")["DSH_DEFAULT_CLI"]
    b = products.env_for("codex", "t")["DSH_DEFAULT_CLI"]
    assert a == "claude" and b == "codex"


def test_codex_uses_the_responses_wire():
    """Codex 0.151 起**不认 chat 面** (wire_api="chat" 直接报不支持)。"""
    boot = products.boot_script("codex")
    assert 'wire_api = "responses"' in boot


@pytest.mark.parametrize("product_id", SLOTS)
def test_first_run_wizards_are_pre_answered(product_id):
    """拆了登录墙却把人放进"必填"向导, 对用户没区别: 他还是进不去。"""
    boot = products.boot_script(product_id)
    assert '"hasCompletedOnboarding": true' in boot
    assert 'trust_level = "trusted"' in boot


@pytest.mark.parametrize("product_id", SLOTS)
def test_user_config_is_not_clobbered_every_boot(product_id):
    """.claude.json 之后装的是用户自己的偏好 —— 每次覆盖等于悄无声息地抹掉。

    Codex 的 config.toml 相反, **必须**每次重写: 里面有每次建实例都会换的网关
    令牌与型号, 沿用旧的必然 401。
    """
    boot = products.boot_script(product_id)
    assert "if [ ! -f /home/agent/.claude.json ]" in boot


# ---- OpenClaw 的配置注入 -----------------------------------------------------


def test_openclaw_config_has_no_retired_keys():
    """OpenClaw 的 controlUi 是 strictObject —— **多一个键就整个启动失败**。

    2026-08-31 升级到 2026.8.1 时踩到: allowInsecureAuth 被上游废弃了 (进了
    TIER_EVAL_RETIRED_ROOT_PATHS), 而我们还在下发它, 结果是
    `Config validation failed: gateway.controlUi: Unrecognized key`,
    容器直接 Terminated —— 症状是实例建得出来但探活永远不过, 用户看到的只是
    "云工作台启动中"转到超时。
    """
    boot = products.boot_script("openclaw")
    for retired in ("allowInsecureAuth", "dangerouslyDisableDeviceAuth"):
        assert retired not in boot, f"{retired} 已被上游废弃, 下发它会让容器起不来"


def test_openclaw_still_removes_the_login_wall():
    """这三处注入就是"不留登录墙"的实现, 少任何一处墙都会原样冒回来。

    · trusted-proxy: 身份由边缘给定 (它拒绝"监听 LAN + 无鉴权");
    · allowedOrigins: 少了它 WebSocket 被按来源拒, 页面退回一个要填
      URL/令牌/密码的连接表单 —— 看着就是第二道登录墙;
    · dangerouslyDisableDeviceAuth: 少了它要用户去主机上跑 devices approve,
      而他既没有主机也不该有。
    """
    boot = products.boot_script("openclaw")
    assert "trusted-proxy" in boot
    assert "allowedOrigins" in boot
    assert "deviceAutoApprove" in boot


# ---- 会话注入: 下行不够, 上游方向也要 ---------------------------------------


@pytest.mark.parametrize("product_id", ("dify", "coze", "hermes"))
def test_session_injection_covers_the_upstream_direction(product_id):
    """替用户登录的产品, **两个方向都要注入**。

    只发 Set-Cookie 是不够的: 浏览器第一发手里还没有 cookie, 而这些应用的首页
    往往会立刻 30x 到自己的鉴权路径, 那一跳认不出他就落到登录页 —— 整条链在
    **同一次访问里**走完, Set-Cookie 根本来不及生效。

    2026-08-31 在 Dify 上抓到: 所有容器 Running、所有接口 200, 而用户看到的是
    Next.js 的错误页。Coze 与 Hermes 早就做了上游注入, 是这条经验当时没推广到
    Dify —— 它那次只修了"无条件补发"这一半。
    """
    # 各产品注入用的变量名不同, 但都必须是"工作台那份会话"而不是原样透传。
    # 只断言 `proxy_set_header Cookie` 出现过是不够的 —— 配置里可能另有一处
    # 原样透传的, 变异掉真正那处照样能过 (实测)。
    var = {"dify": "$dsh_up", "coze": "$dsh_cookie", "hermes": "$hm_up"}[product_id]
    boot = products.boot_script(product_id)
    assert f"proxy_set_header Cookie {var}" in boot, "少了上游方向的 cookie 注入"


# ---- OpenManus / CrewAI: 同一套工作台外壳的另外两格 -------------------------
#
# 老板 2026-09-02: "CrewAI 和 OpenManus 包类似咱们为 claude 和 codex 建的前端啊"。
# 原先这两格只有一个浏览器终端 —— 框架装好了、网关配好了, 但用户打开看到的是
# 一个黑底提示符。下面钉的是"换成工作台"之后**不许退回去**的几条。

FRAMEWORK_SLOTS = ("openmanus",)


@pytest.mark.parametrize("product_id", FRAMEWORK_SLOTS)
def test_framework_slots_serve_the_workbench_not_a_bare_terminal(product_id):
    """端口/探针/启动命令三处要一起指向工作台, 少一处就是静默半坏。

    只改端口不改启动命令 = 8080 上没人听, 冷启动一直 502;
    只改启动命令不改探针 = 探 "/" 会在后端起来之前放人进来 (Dify/Coze 栽过)。
    """
    prod = products.registry()[product_id]
    assert prod.port == 8080, "工作台在 8080; 7681 是 ttyd, 它已经退到反代后面了"
    assert prod.ready_path == "/api/health", "探首页等于只探到静态文件"

    boot = products.boot_script(product_id)
    assert "uvicorn app.main:app" in boot, "起的不是工作台"
    assert "exec ttyd" not in boot, "又退回成一个裸终端了"
    # uvicorn 必须从 /srv 起 —— 上一行 cd 去了 /workspace, 不回来就是
    # ModuleNotFoundError, 而表现是容器起不来。
    assert boot.index("cd /srv") < boot.index("uvicorn app.main:app")


@pytest.mark.parametrize("product_id", FRAMEWORK_SLOTS)
def test_framework_slots_open_only_their_own_cli(product_id):
    """这一格开放的必须只有它自己那个框架。

    镜像里根本没有 claude/codex/gemini —— 露出来就是用户切过去发一句话、
    进程起不来, 而前端只会显示"发了消息没反应"。
    """
    env = products.env_for(product_id, "tok")
    assert env["DSH_DEFAULT_CLI"] == product_id
    assert env["DSH_ENABLED_CLIS"] == product_id


@pytest.mark.parametrize("product_id", FRAMEWORK_SLOTS)
def test_framework_model_is_pinned_for_litellm_too(product_id):
    """CrewAI 走 litellm, **不给型号就用它自己的默认** (gpt-4o-mini)。

    网关只放行在售目录里的型号 —— 表现是发一句话回一条 404, 看起来像"配置没
    生效", 其实是型号名不对。
    """
    env = products.env_for(product_id, "tok")
    assert model_catalog.resolve(env["OPENAI_MODEL_NAME"]) is not None


def test_openmanus_browser_mcp_is_off():
    """不关掉的话它每次启动都去连 Browser Use 的 MCP, 而镜像里没有 uvx。

    结果是用户第一眼看到一条红 ERROR, 而它其实无害。这是上游自带的开关,
    不是我们打的补丁。
    """
    assert products.env_for("openmanus", "tok")["OPENMANUS_DISABLE_BROWSER_USE"] == "1"


@pytest.mark.parametrize("product_id", FRAMEWORK_SLOTS)
def test_framework_terminal_does_not_drop_into_a_python_repl(product_id):
    """终端标签页起的命令由适配器给, 不能直接拿 exe。

    这两格的 exe 是 **Python 解释器** (我们跑的是自己的 runner) —— 直接敲它,
    用户点开终端看到的是 `>>>`, 而不是这个框架。
    """
    import importlib.util
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "dsh_adapters", root / "deploy" / "workspace-agentui" / "app" / "adapters.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # **先登记进 sys.modules 再执行**: 文件里有 from __future__ import annotations,
    # 于是 @dataclass 解析注解时要回头找自己所在的模块 —— 找不到就是一句
    # "'NoneType' object has no attribute '__dict__'", 跟被测代码毫无关系。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    ad = mod.ADAPTERS[product_id]()
    assert ad.term_cmd != ad.exe, "终端里敲解释器 = 掉进 Python REPL"
    # 终端要**直接进框架自己的交互界面**, 不落一个裸 shell —— 老板 2026-09-02
    # 看到 CrewAI 那格停在提示符上: "怎么终端没有把 Agent 自动启动"。
    assert {"openmanus": "python main.py"}[product_id] in ad.term_cmd
    assert "/opt/venv-" in ad.exe, "runner 要用这一格自己的虚拟环境跑"
    # runner 吐的就是统一事件, 认不出的行不许丢 —— 丢掉的症状是"偶尔少半句话"。
    assert ad.feed('{"t":"text","text":"x"}') == [{"t": "text", "text": "x"}]
    assert ad.feed("不是 json")[0]["t"] == "raw"
    # **用量的键名是 input/output**。写成各家 API 那套 *_tokens 不会报错, 只是
    # 界面上"本轮消耗"永远 0↑0↓ —— 上线当天就是这么漏出去的。
    done = ad.feed('{"t":"done","usage":{"input_tokens":7,"output_tokens":3}}')[0]
    assert done["usage"]["input"] == 7 and done["usage"]["output"] == 3


def test_openmanus_stops_when_the_model_answers_without_tools():
    """直接作答就收口的补丁必须在, 而且钉的是上游的原句 —— 改了当场知道。

    上游 ReAct 循环里, AUTO 模式下模型回了正文却没选工具时会**继续下一步**,
    直到它主动调 terminate 或跑满 max_steps。对一句"你好啊"它永远不会去调
    terminate, 于是一路自言自语 20 步 (老板 2026-09-02 在终端里撞到的)。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    src = (root / "deploy" / "workspace-frameworks" / "patch_openmanus.py").read_text(encoding="utf-8")
    assert "self.state = AgentState.FINISHED" in src
    assert "if self.tool_choices == ToolChoice.AUTO and not self.tool_calls:" in src, "锚点要钉上游原句"
    dockerfile = (root / "deploy" / "workspace-frameworks" / "Dockerfile").read_text(encoding="utf-8")
    assert "patch_openmanus.py" in dockerfile, "补丁没进镜像等于没打"


def test_terminal_turns_on_iutf8_before_the_agent():
    """终端起来前必须 `stty iutf8`, 否则退格会把汉字削掉一个字节。

    ttyd 给的伪终端默认没开这一位; 规范模式下退格只删一个字节, 剩下的不是合法
    UTF-8, Python 兑成代理字符, 发网关时 'surrogates not allowed'。老板 2026-09-02
    在 CrewAI 终端里改了一个字就撞上; 真 PTY 里复现并验证过, 开了就好。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    src = (root / "deploy" / "workspace-agentui" / "app" / "main.py").read_text(encoding="utf-8")
    i = src.index("stty iutf8")
    assert i < src.index("_agent_term_cmd()}; exec bash -l"), "要在进 agent 之前开"
    # (crewai 那格 2026-09-02 换成了 CrewAI-Studio, 不再走这个终端)
    for pid in ("openmanus",):
        assert products.env_for(pid, "tok")["PYTHONIOENCODING"].startswith("utf-8:replace")


# ---- pi (pi-web-ui 前端) -----------------------------------------------------
#
# 老板 2026-09-02 拍板: pi 顶掉 OpenHands, 前端用社区的 pi-web-ui。下面钉的全是
# 静默错法: 型号没钉是每次 404、探针打到 SPA 兜底是把人放进坏页面、白名单不设是
# 页面能开但对话/终端一直重连。


def test_pi_slot_is_wired_to_the_gateway(monkeypatch):
    from app import config, model_catalog

    monkeypatch.setattr(config, "PI_DOMAIN", "pi.test.local")
    prod = products.registry()["pi"]
    assert prod.port == 8787
    # /health 与 /healthz 是 SPA 兜底 (什么路径都 200), 只有 /api/health 是真接口。
    assert prod.ready_path == "/api/health"

    boot = products.boot_script("pi")
    assert '"api": "openai-completions"' in boot
    assert '"apiKey": "$DSH_GATEWAY_KEY"' in boot, "令牌要从环境变量取, 不落盘"
    assert "/llm/v1" in boot
    # 型号必须在售 —— 网关只放行目录里的
    import re

    m = re.search(r'"defaultModel": "([^"]+)"', boot)
    assert m and model_catalog.resolve(m.group(1)) is not None
    assert '"enableAnalytics": false' in boot and '"enableInstallTelemetry": false' in boot
    assert "--no-browser" in boot, "容器里没有 xdg-open, 不关会多两行抱怨"
    assert boot.index("models.json") < boot.index("exec pi-web-ui"), "配置要在起服务之前写好"

    env = products.env_for("pi", "tok")
    assert env["DSH_GATEWAY_KEY"] == "tok"
    # 探针实测: 给了 OPENAI_API_KEY, pi 就把内置 OpenAI 提供方点亮并排在前面,
    # 界面选中 gpt-5.5, 拿网关令牌去打 api.openai.com, 回 401 invalid_jwt。
    assert "OPENAI_API_KEY" not in env, "会把内置 OpenAI 提供方点亮"
    assert env["PI_OFFLINE"] == "1" and env["PI_TELEMETRY"] == "0"
    # WS 同源校验白名单: 反代进来 Origin 是 https://<域>, 不放行就是"页面能开,
    # 对话和终端一直重连"。
    assert env["PI_WEB_ALLOW_ORIGINS"] == "https://pi.test.local"


def test_pi_took_over_the_openhands_slot():
    """目录里是 pi 不再是 OpenHands; 文案两种语言都要有, 少一种那张卡就是空的。"""
    import json
    import pathlib

    from app import apps_catalog

    ids = [a.id for a in apps_catalog.CATALOG]
    assert "pi" in ids and "openhands" not in ids
    assert len(ids) == 16
    root = pathlib.Path(__file__).resolve().parents[1] / "config" / "i18n"
    for lang in ("zh", "en"):
        d = json.loads((root / f"{lang}.json").read_text(encoding="utf-8"))
        assert d.get("apps.d.pi"), f"{lang}.json 缺 apps.d.pi"


def test_pi_gets_the_whole_catalog_with_reasoning(monkeypatch):
    """pi 的 models.json 要列**整个在售目录**, 并按能力元数据标 reasoning。

    老板 2026-09-02 第一眼: "pi 这个为什么只有一个模型, 思考也开不了" —— 第一版
    只写了一个型号还标了 reasoning=false。走网关实测 20/20 型号都吃
    reasoning_effort, 所以默认 true; 能力表在 config/model_capabilities.json。
    """
    import json

    from app import config, model_catalog

    monkeypatch.setattr(config, "PI_DOMAIN", "pi.test.local")
    boot = products.boot_script("pi")
    i = boot.index("<<'DSHEOF'\n") + len("<<'DSHEOF'\n")
    j = boot.index("\nDSHEOF", i)
    d = json.loads(boot[i:j])
    prov = d["providers"]["dsh"]
    ids = [m["id"] for m in prov["models"]]
    assert set(ids) == set(model_catalog.catalog()), "目录里有的型号 pi 里都得有, 多一个少一个都不对"
    assert ids[0] == products._codecli_model("codex"), "默认型号排第一 (它的下拉按顺序列)"
    # reasoning 跟能力表走: 实测 19/20 吃, mimo-v2-omni 记了 false (上游什么都 400)
    for m in prov["models"]:
        assert m["reasoning"] is bool(model_catalog.capabilities(m["id"])["reasoning"]), m["id"]
    assert sum(m["reasoning"] for m in prov["models"]) >= len(prov["models"]) - 2, "思考开关几乎全开"
    assert prov["compat"]["supportsReasoningEffort"] is True
    assert prov["compat"]["thinkingFormat"] == "reasoning_effort"
    # cost 一律 0: 它会在状态栏按这个算美元, 而用户付的是积分, 那个数只会误导
    assert all(m["cost"]["input"] == 0 and m["cost"]["output"] == 0 for m in prov["models"])


def test_model_capabilities_fall_back_to_defaults():
    """能力表缺条目时用 default, 缺文件时也不许炸 —— 它只是元数据, 不该拖垮启动。"""
    from app import model_catalog

    cap = model_catalog.capabilities("this-model-does-not-exist")
    assert cap["reasoning"] is True and cap["vision"] is False and cap["context_window"] == 128000


# ---- CrewAI (CrewAI-Studio 前端) --------------------------------------------


def test_crewai_slot_runs_the_studio(monkeypatch):
    """CrewAI 那格换成社区的 CrewAI-Studio (老板 2026-09-02): 端口/探针/启动/示例队伍。"""
    from app import config, model_catalog

    monkeypatch.setattr(config, "CREWAI_DOMAIN", "crew.test.local")
    prod = products.registry()["crewai"]
    assert prod.port == 8501 and prod.ready_path == "/_stcore/health"
    boot = products.boot_script("crewai")
    assert "seed_demo.py" in boot, "空 Studio 不叫开箱即用 —— 开机要种示例队伍"
    assert "--client.toolbarMode minimal" in boot, "右上角 Deploy 是它家的入口"
    assert "--browser.gatherUsageStats false" in boot
    assert boot.index("seed_demo.py") < boot.index("exec streamlit")
    env = products.env_for("crewai", "tok")
    assert env["OPENAI_API_BASE"].endswith("/llm/v1")
    assert env["DB_URL"].startswith("sqlite:////root/"), "库要落 NAS, 不然回收就没了"
    assert env["DEFAULT_LANGUAGE"] == "zh"
    assert env["CREWAI_TRACING_ENABLED"] == "false" and env["CREWAI_DISABLE_TELEMETRY"] == "true"
    models = env["OPENAI_PROXY_MODELS"].split(",")
    assert set(models) == set(model_catalog.catalog()) and models[0] == products._codecli_model("codex")


def test_openmanus_no_longer_seeds_a_crew_project(monkeypatch):
    """OpenManus 的工作区里不该长出 crew/ —— 老板 2026-09-02: "好诡异"。

    CrewAI 换成 Studio 之前两格合用一份启动脚本, 开机会把 CrewAI 的工程模板复制进
    /workspace/crew。现在不种了; 之前种下的、用户没动过的收回去 (逐字节比对), 改过的留着。
    """
    boot = products.boot_script("openmanus")
    assert "cp -r /opt/dsh/crew-template" not in boot
    assert "diff -q $W/agents.yaml $T/agents.yaml" in boot, "没动过的要收回去 —— 只比我们写的两份 yaml"
        "没动过的要收回去 (跳过每次构建都变的 .git)"
    )
    assert "DSH_CREW_DIR" not in products.env_for("openmanus", "tok")
