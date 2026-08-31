"""编码智能体工作台 (Claude Code / Codex) 的接线守卫。

这两个坑位共用一个镜像, 区别全在启动脚本和 env 里 —— 而它们错起来都是**静默**的:
钉错型号是每次请求 404, HOME 放错是闲置回收后"昨天的会话不见了", 两样都不会在
日志里说自己错了。
"""

from __future__ import annotations

import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="dhc-codecli-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import model_catalog, products

CODECLI = ("codex",)


@pytest.mark.parametrize("product_id", CODECLI)
def test_default_model_is_actually_on_sale(product_id):
    """网关只放行在售目录里的型号 —— 钉一个不在售的等于这个产品每次都 404。

    而这些 CLI 各自的内置默认值都是厂商自己的型号名 (Claude Code 要 haiku,
    Codex 要 gpt-5-codex), 一个都不在我们的目录里, 所以**必须**钉死。
    """
    model = products._codecli_model(product_id)
    assert model_catalog.resolve(model) is not None, f"{product_id} 钉的 {model} 不在售"


@pytest.mark.parametrize("product_id", CODECLI)
def test_state_lands_on_the_nas(product_id):
    """HOME 必须是 /root。

    NAS 只挂在 /root 与 /workspace 两处 (workbackend 的 VolumeMount)。指别处的话
    code-server 的设置、会话历史、CLI 登录态全落在容器里, 闲置回收一删就没 ——
    这个错法不报任何错, 用户只会发现昨天的东西不见了。
    """
    env = products.env_for(product_id, "tok")
    assert env["HOME"] == "/root"
    assert "/workspace" in products.boot_script(product_id)


@pytest.mark.parametrize("product_id", CODECLI)
def test_no_login_wall(product_id):
    """--auth none 是我们选 code-server 的**唯一理由** (老板铁律: 不留登录墙)。"""
    assert "--auth none" in products.boot_script(product_id)


@pytest.mark.parametrize("product_id", CODECLI)
def test_terminal_opens_straight_into_the_agent(product_id):
    """默认终端就是 agent 本身, 且打开工作区就自动起一个。

    否则用户进来看到的是一个空编辑器, 得先知道该敲哪个命令 —— 这个产品卖的
    就是"开箱即用", 少了这一步它和一个空 VS Code 没区别。
    """
    boot = products.boot_script(product_id)
    exe = products._CODECLI_AGENTS[product_id][0]
    assert f"/usr/local/bin/{exe}" in boot
    # 默认终端必须是 **bash** 而不是 agent: agent 由镜像里的扩展开在编辑器区,
    # 这里若也设成 agent, VS Code 每实例化一次终端面板就按默认配置文件再起一个
    # —— 屏幕上两个、账上两份 (实测过)。
    assert '"terminal.integrated.defaultProfile.linux": "bash"' in boot
    # 自动开终端归镜像里那个 dsh-agent 扩展管 (见 test_agent_terminal_is_
    # wired_through_env)。**不再**走 VS Code 的 folderOpen 任务: 两条路同时
    # 生效, 实测起了两个终端、两个 agent 进程, 各自烧着积分。
    assert "folderOpen" not in boot, "tasks.json 那条路回来了 —— 会起两个 agent"


def test_codex_uses_the_responses_wire():
    """Codex 0.151 起**不认 chat 面** (wire_api="chat" 直接报不支持)。"""
    boot = products.boot_script("codex")
    assert 'wire_api = "responses"' in boot
    assert "/llm/v1" in boot


def test_claude_code_pins_the_small_model_too():
    """不设 SMALL_FAST 的话它去要 haiku —— 我们不卖那个名字, 于是后台任务全 404。

    Claude Code 这一格现在是 CloudCLI 形态, 业务 env 在伴随容器上。
    """
    env = dict(products.registry()["claude-code"].sidecars[0].env)
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == env["ANTHROPIC_MODEL"]
    assert env["ANTHROPIC_BASE_URL"].endswith("/llm/anthropic")


def test_codex_user_state_files_are_not_clobbered_every_boot():
    """CLI 自己的状态文件只在**不存在时**写。

    这些文件之后装的是用户的会话与偏好, 每次启动覆盖等于悄无声息地抹掉。
    产品配置 (code-server 的设置、Codex 的 config.toml) 相反, 必须每次重写 ——
    里面有每次建实例都会换的网关令牌 (沿用旧的必然 401)。
    """
    boot = products.boot_script("codex")
    assert "/root" in boot


def test_codex_trusts_the_workspace_up_front():
    """不写这段, Codex 一起来就问"你信任这个目录吗" —— 用户自己的工作区, 白挡。"""
    boot = products.boot_script("codex")
    assert '[projects."/workspace"]' in boot
    assert 'trust_level = "trusted"' in boot


# ---- CloudCLI 形态 (claude-code 这一格) ------------------------------------
#
# 与 code-server 版跑同一批 CLI、同一套网关接线, 只是外壳不同。它自带单用户账号
# 体系 (注册 + 账号密码 + 7 天期 JWT) 且没有关掉鉴权的开关 —— 凭据由工作台代持。


def test_cloudcli_model_is_actually_on_sale():
    """伴随容器上钉的型号必须在售 —— 否则这个产品每次请求都 404。"""
    env = dict(products.registry()["claude-code"].sidecars[0].env)
    assert model_catalog.resolve(env["ANTHROPIC_MODEL"]) is not None


def test_cloudcli_autologin_covers_both_register_and_login():
    """第一次是注册, 之后是登录 —— 判据是它自己的 needsSetup。

    只做登录的话新实例永远登不进去 (还没有账号), 只做注册的话第二次启动会失败。
    """
    boot = products.boot_script("claude-code")
    assert "needsSetup" in boot
    assert "EP=register" in boot
    assert "EP=login" in boot
    assert "/api/auth/$EP" in boot


def test_cloudcli_injection_is_unconditional():
    """令牌**每次页面加载都覆盖**, 不判断浏览器里有没有。

    手里那份一旦失效 (实例重建换了密码、或 7 天到期) 它仍然"存在" —— 只在缺失时
    补的话用户会被永久钉在登录页, 而且刷新多少次都没用。这个判据我在 Dify 上错过
    两次, 症状一模一样。
    """
    boot = products.boot_script("claude-code")
    assert 'localStorage.setItem("auth-token"' in boot
    # 判据不该出现在注入逻辑里
    assert "getItem" not in boot


def test_cloudcli_disables_upstream_compression():
    """sub_filter 改不了 gzip 过的响应体 —— 不关掉上游压缩, 脚本根本插不进去,
    而两边都不报错。"""
    assert 'proxy_set_header Accept-Encoding ""' in products.boot_script("claude-code")


def test_cloudcli_first_run_walls_are_pre_answered():
    """拆了登录墙却把人放进"必填"向导或空项目列表, 对用户没区别: 他还是进不去。"""
    boot = products.boot_script("claude-code")
    assert "complete-onboarding" in boot
    assert "create-project" in boot
    assert "/root/.gitconfig" in boot


def test_cloudcli_ready_path_is_not_the_front_page():
    """首页是前端静态资源, 后端没起来它照样 200。

    探活打首页 = 用户会在后端就绪前被放进一个坏页面 (2026-08-30 Dify 那次)。
    """
    p = products.registry()["claude-code"]
    assert p.ready_path.startswith("/api/")


def test_cloudcli_shares_the_same_nas_dirs_as_the_editor_shell():
    """NAS 子路径要与主容器那两个 (/root、/workspace) 是同一份。

    否则换外壳的时候用户的文件和会话凭空消失 —— 而这两版本来就是给他挑的。
    """
    mounts = dict(products.registry()["claude-code"].sidecars[0].mounts)
    assert mounts["home"] == "/root"
    assert mounts["workspace"] == "/workspace"


@pytest.mark.parametrize("product_id", CODECLI)
def test_agent_terminal_is_wired_through_env(product_id):
    """镜像里那个 dsh-agent 扩展照这两个变量开终端。

    少了它们用户进去是一个空编辑器 —— 这个产品卖的就是"点开就能用"。
    (原先靠 VS Code 的 folderOpen 自动任务, 实测不跑: 它的前置条件太多,
    每一条都可能让它静默不执行。)
    """
    env = products.env_for(product_id, "tok")
    exe = products._CODECLI_AGENTS[product_id][0]
    assert env["DSH_AGENT_CMD"] == f"/usr/local/bin/{exe}"
    assert env["DSH_AGENT_NAME"]


@pytest.mark.parametrize("product_id", CODECLI)
def test_builtin_chat_panel_is_disabled(product_id):
    """VS Code 自带的 Chat/Agent 面板必须关掉。

    它在 code-server 4.135 里是**内核 UI** —— 把 copilot 扩展从镜像里删掉也去不
    掉: 面板还在, 右下角照样挂着 "Sign In", 一点就要 GitHub OAuth。等于我们自己
    的产品里又藏了一道登录墙 (老板铁律里明令不要的)。而且它是多余的: 这个产品的
    agent 就是终端里那个 CLI。
    """
    boot = products.boot_script(product_id)
    assert '"chat.disableAIFeatures": true' in boot


def test_cloudcli_projects_are_confined_to_the_nas_workspace():
    """项目根必须指到 /workspace (NAS 那一份)。

    它默认用 HOME —— 而 CloudCLI 只允许在这个根下面建项目, 所以默认值下用户建的
    东西全落在容器里, 闲置回收一删就没。这个错法不报任何错。
    """
    env = dict(products.registry()["claude-code"].sidecars[0].env)
    assert env["WORKSPACES_ROOT"] == "/workspace"


def test_cloudcli_html_response_is_never_cached():
    """这个页面里嵌着一枚活令牌 —— 任何一层缓存它都是一次重放风险。

    浏览器自己的 HTTP 缓存就够制造"session expired": 缓存发生在令牌注入之后、
    容器某次重建之前, 之后免网络重放, 症状是令牌本身没坏, 只是那次响应是旧的。
    只对 HTML 禁缓存 —— JS/CSS 文件名带内容哈希, 天然可以长期缓存。
    """
    boot = products.boot_script("claude-code")
    assert "Cache-Control $cc_no_store always" in boot
    assert '"no-store"' in boot
