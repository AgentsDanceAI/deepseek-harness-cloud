"""claude-code / codex 两格的外壳开关 (config.USE_CLI_WORKSPACE)。

这个开关有**两个分支**, 只验一个等于没验: 默认那条是线上正在跑的东西 (不能
被顺手改坏), 打开那条是还没上线的新外壳 (改坏了没人会发现, 直到真切过去)。

两个外壳的端口不同 (agentui 8080 / pi-web-ui 8787), env 更是两套完全不同的 ——
混着给的症状是容器起来了、页面能开, 而对话一直连不上或者每一发都 401。
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


def test_flag_switches_image_port_and_env_together(slot):
    """三样必须一起换。端口对不上的症状是容器起来了、就绪探针一直超时, 而日志
    里一切正常 —— 最难查的那种。"""
    p, env = slot(True)
    assert "workspace-cli" in p.image
    assert p.image == p.image_ref, "image 与 image_ref 必须同源, 否则拉的和记的不是一个"
    assert p.port == config.CLI_WORKSPACE_PORT == 8787
    assert env["PI_WEB_ENGINE"] == "codex"
    assert env["DSH_GATEWAY_BASE"] and env["DSH_CLOUD_TOKEN"] == "TOK"


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
