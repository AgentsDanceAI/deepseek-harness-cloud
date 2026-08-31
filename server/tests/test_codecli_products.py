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

CODECLI = ("claude-code", "codex")


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
    assert '"terminal.integrated.defaultProfile.linux"' in boot
    assert f"/usr/local/bin/{exe}" in boot
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
    """不设 SMALL_FAST 的话它去要 haiku —— 我们不卖那个名字, 于是后台任务全 404。"""
    env = products.env_for("claude-code", "tok")
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == env["ANTHROPIC_MODEL"]
    assert env["ANTHROPIC_BASE_URL"].endswith("/llm/anthropic")


def test_user_state_files_are_not_clobbered_every_boot():
    """CLI 自己的状态文件只在**不存在时**写。

    这些文件之后装的是用户的会话与偏好, 每次启动覆盖等于悄无声息地抹掉。
    产品配置 (code-server 的设置、Codex 的 config.toml) 相反, 必须每次重写 ——
    里面有每次建实例都会换的网关令牌 (沿用旧的必然 401)。
    """
    boot = products.boot_script("claude-code")
    assert 'if [ ! -f "$HOME/.claude.json" ]' in boot


def test_codex_trusts_the_workspace_up_front():
    """不写这段, Codex 一起来就问"你信任这个目录吗" —— 用户自己的工作区, 白挡。"""
    boot = products.boot_script("codex")
    assert '[projects."/workspace"]' in boot
    assert 'trust_level = "trusted"' in boot


def test_claude_onboarding_is_pre_answered():
    """选主题那一屏在托管环境里只有一个答案, 问出来就是让人先做一遍配置。"""
    assert '"hasCompletedOnboarding": true' in products.boot_script("claude-code")


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
