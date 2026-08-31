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
    assert '"runOn": "folderOpen"' in boot


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


def test_user_workspace_files_are_not_clobbered_every_boot():
    """tasks.json 落在 /workspace (用户自己的目录) —— 每次覆盖等于抹掉他的改动。"""
    boot = products.boot_script("claude-code")
    assert "if [ ! -f /workspace/.vscode/tasks.json ]" in boot


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
