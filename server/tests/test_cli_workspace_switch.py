"""claude-code / codex 两格的外壳开关 (config.USE_CLI_WORKSPACE)。

这个开关有**两个分支**, 只验一个等于没验: 默认那条是线上正在跑的东西 (不能
被顺手改坏), 打开那条是还没上线的新外壳 (改坏了没人会发现, 直到真切过去)。

两个外壳的端口不同 (agentui 8080 / pi-web-ui 8787), env 更是两套完全不同的 ——
混着给的症状是容器起来了、页面能开, 而对话一直连不上或者每一发都 401。

**这条测试第一版漏了启动脚本, 于是真切换时当场炸了** (2026-09-11): 镜像/端口/env
三样都换了, boot_script 还返回 agentui 那份 Python 启动命令, pod 起来、init 全过、
2/2 Running 了几十秒, 然后 app 容器 exitCode 127 (`sh: exec: uvicorn: not found`)。
所以下面按"**四样必须一起换**"来钉, 不是三样。
"""

from __future__ import annotations

import pytest

from . import test_webpages as _env

_ENV_READY = _env is not None

from app import config, products  # noqa: E402


@pytest.fixture()
def slot(monkeypatch):
    def pick(on: bool):
        monkeypatch.setattr(config, "USE_CLI_WORKSPACE", on)
        return products.get("codex"), products.env_for("codex", "TOK", "")

    return pick


def test_default_is_the_shell_that_is_live_today(slot):
    """默认必须还是自研 agentui —— 新镜像没构建推送之前切过去, 两格当场拉不到镜像。"""
    p, env = slot(False)
    assert "agentui" in p.image
    assert p.port == products.AGENTUI_PORT
    # agentui 那条路靠 ANTHROPIC_* 接网关
    assert env["ANTHROPIC_BASE_URL"].endswith("/llm/anthropic")
    assert "PI_WEB_ENGINE" not in env
    assert "uvicorn" in products.boot_script("codex"), "默认那条的启动脚本被改坏了"


def test_flag_switches_image_port_env_and_boot_together(slot):
    """**四样必须一起换**: 镜像 / 端口 / env / 启动脚本。
    端口对不上 → 容器起来了但就绪探针一直超时; 启动脚本不换 → exitCode 127,
    而两者在 kubectl 第一眼看到的都是 Running。"""
    p, env = slot(True)
    assert "workspace-cli" in p.image
    assert p.image == p.image_ref, "image 与 image_ref 必须同源, 否则拉的和记的不是一个"
    assert p.port == config.CLI_WORKSPACE_PORT == 8787
    assert env["PI_WEB_ENGINE"] == "codex"
    assert env["DSH_GATEWAY_BASE"] and env["DSH_CLOUD_TOKEN"] == "TOK"
    boot = products.boot_script("codex")
    assert "node dist/server/index.js" in boot, "启动脚本没跟着换 —— 会 exitCode 127"
    assert "uvicorn" not in boot, "还在用 agentui 那份 Python 启动命令"


@pytest.mark.parametrize(("pid", "cli"), [("claude-code", "claude"), ("codex", "codex")])
def test_terminal_boots_into_this_slots_cli(monkeypatch, pid, cli):
    """点进 [终端] 应该直接是这一格那个 CLI 的界面。
    老板连提两次 —— 上一版只做了"免登录", 没做"自动唤起"。
    唤起哪个必须跟着**本格的引擎**走: claude 那格唤起 claude, codex 那格唤起 codex。"""
    monkeypatch.setattr(config, "USE_CLI_WORKSPACE", True)
    assert products.env_for(pid, "TOK", "")["PI_WEB_TERMINAL_BOOT_CMD"] == cli


@pytest.mark.parametrize(("pid", "theme"), [("claude-code", "cyberpunk"), ("codex", "dazzle")])
def test_each_slot_ships_its_own_skin(monkeypatch, pid, theme):
    """两格开箱皮肤不同 —— 开着一堆标签页时一眼认得出哪个是哪个。
    只是默认值: 用户选过以他为准 (前端那半在 pi-web-ui 的 instance-theme.test.ts)。
    要先翻开关 —— 皮肤是新外壳才有的东西, agentui 那条路没有这个概念。"""
    monkeypatch.setattr(config, "USE_CLI_WORKSPACE", True)
    env = products.env_for(pid, "TOK", "")
    assert env.get("PI_WEB_DEFAULT_THEME") == theme
    monkeypatch.setattr(config, "USE_CLI_WORKSPACE", False)
    assert "PI_WEB_DEFAULT_THEME" not in products.env_for(pid, "TOK", "")


def test_root_container_lets_claude_skip_permissions(slot):
    """容器以 root 跑, 而引擎拿 --dangerously-skip-permissions 起 claude ——
    Claude Code 2.1.x 对 root 一律拒: `cannot be used with root/sudo privileges`。
    2026-09-11 切到 r2 后生产实测: claude-code 那格发什么都没回音, 退出码 1。
    IS_SANDBOX=1 是它给容器留的口子; 我们的 pod 本来就在 gVisor 里。
    钉在**容器 env** 上: 引擎子进程与 [终端] 都是 ...process.env 展开的。"""
    _, env = slot(True)
    assert env.get("IS_SANDBOX") == "1", "少了它, claude-code 那格的对话面板在 root 容器里是废的"


def test_new_shell_is_not_given_the_old_shell_s_wiring(slot):
    """**两套接法不能混给。** 新外壳自己拿 DSH_GATEWAY_BASE 去接网关
    (server/cli/gateway.ts); 再给一份 ANTHROPIC_* 的话, 谁生效说不清 ——
    而错的那个会绕过在售目录, 表现为"换了型号不生效"。"""
    _, env = slot(True)
    assert not [k for k in env if k.startswith("ANTHROPIC")]
    assert "OPENAI_API_KEY" not in env


@pytest.mark.parametrize("pid", ("claude-code", "codex"))
def test_both_slots_move_together(slot, monkeypatch, pid):
    """两格共用一个开关。只切一格的话, 用户在两个产品间来回时看到两套界面。"""
    monkeypatch.setattr(config, "USE_CLI_WORKSPACE", True)
    p = products.get(pid)
    assert "workspace-cli" in p.image and p.port == 8787
    assert products.env_for(pid, "TOK", "")["PI_WEB_ENGINE"] == (
        "claude" if pid == "claude-code" else "codex"
    )
