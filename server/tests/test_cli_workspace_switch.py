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

from app import config, model_catalog, products  # noqa: E402


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


#: 接线类的 ANTHROPIC_* —— 这几个**永远**不能由 products.py 给新外壳 (见下面那条测试)。
#: 与它们相对的是 ANTHROPIC_DEFAULT_*_MODEL 那一组: 那是**菜单**, 不是接线。
_WIRING_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
)


def test_new_shell_is_not_given_the_old_shell_s_wiring(slot):
    """**两套接法不能混给。** 新外壳自己拿 DSH_GATEWAY_BASE 去接网关
    (server/cli/gateway.ts); 再给一份接线用的 ANTHROPIC_* 的话, 谁生效说不清 ——
    而错的那个会绕过在售目录, 表现为"换了型号不生效"。

    ⚠️ 2026-09-13 放宽了一格: ANTHROPIC_DEFAULT_*_MODEL 那一组**是菜单不是接线**
    (见 test_terminal_menu_lists_the_catalog)。它们只决定终端里 /model 列哪几个型号,
    并且优先级低于镜像里写死的 ANTHROPIC_MODEL —— 接线该由谁给, 一点没变。
    原来这条写的是"一个 ANTHROPIC 开头的都不许有", 那是**按前缀**判的, 而要防的事
    是**按语义**的: 再加菜单变量时别把这条改成放行整个前缀。
    """
    _, env = slot(True)
    assert not [k for k in env if k in _WIRING_KEYS], "接线类的 ANTHROPIC_* 混进来了"
    assert "OPENAI_API_KEY" not in env
    stray = [k for k in env if k.startswith("ANTHROPIC") and not k.startswith("ANTHROPIC_DEFAULT_")]
    assert not stray, f"既不是接线也不是菜单的 ANTHROPIC_* : {stray}"


def test_terminal_menu_lists_the_catalog(monkeypatch):
    """终端里 `/model` 的菜单要列**在售目录**, 而不是 CLI 自带的 Anthropic 牌名。

    创始人 2026-09-13 看到的那一屏: 六行里五行是 Opus 4.8 / Sonnet 4.6 / Haiku 4.5 之类,
    点哪行都是网关 404 (那些名字不在在售目录里), 只有最后一行 "claude-sonnet-5 · Custom"
    能用。claude 2.1.193 把这四个别名做成了可重定向的槽, 所以不用改镜像。

    ⚠️ **四个槽都要设满**: 漏一个, 菜单里那一行就退回 CLI 自带的牌名 —— 实测留下的
    "5. Haiku (Haiku 4.5)" 点一下就是 404。这条断言钉的就是"别漏槽"。
    """
    monkeypatch.setattr(config, "USE_CLI_WORKSPACE", True)
    env = products.env_for("claude-code", "TOK", "")
    slots = [k for k in env if k.endswith("_MODEL") and k.startswith("ANTHROPIC_DEFAULT_")]
    assert len(slots) == 4, f"槽没设满, 菜单里会留下点了必 404 的行: {sorted(slots)}"
    for key in slots:
        mid = env[key]
        entry = model_catalog.resolve(mid)
        assert entry is not None, f"{key}={mid} 不在在售目录里 —— 菜单上摆一个点了必 404 的型号"
        assert str(entry.get("provider")) == "Anthropic", f"{key} 指向了非 Anthropic 型号 {mid}"
        # 名字与说明都要给: 不给 _NAME 菜单显示裸 id, 不给 _DESCRIPTION 显示英文
        # "Custom Opus model" —— 两样都是"能用但看不懂"。
        assert env.get(f"{key}_NAME"), f"{key} 少了 _NAME"
        assert "倍积分" in env.get(f"{key}_DESCRIPTION", ""), f"{key} 的说明里要写清倍率"
    # 便宜的那档必须同时占住 SONNET 与 HAIKU: HAIKU 我们没有对等物, 留空=404 行。
    cheap = min(
        (m for m in model_catalog.catalog().values() if str(m.get("provider")) == "Anthropic"),
        key=lambda m: float(m.get("multiplier") or 0),
    )["id"]
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == cheap
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == cheap
    # codex 那格不该拿到 claude 的菜单 (它走 models_cache.json, 见下一条)
    assert not [k for k in products.env_for("codex", "TOK", "") if k.startswith("ANTHROPIC")]


def test_codex_boot_replaces_its_model_menu_with_the_catalog(monkeypatch):
    """codex 那格的菜单**烧在二进制里**, 只能靠开机写 models_cache.json 换掉。

    不换的后果是实测出来的: 菜单里混着 gpt-5.5 与 gpt-5.2 两个我们不卖的型号, 选中
    发一句话 = 网关 404 (而且 codex 会重试 5 次, 一次选错打六发)。

    ⚠️ 清单是在容器里用 codex 自带的 `codex debug models --bundled` 现取再过滤的 ——
    **别把它抄成仓库里的常量**: 那份内置目录每条都带 OpenAI 整份系统提示词。
    ⚠️ 版本号也必须现问 (`codex --version`): 写死的话 Dockerfile 里 CODEX_VERSION 一升,
    整份缓存**静默作废**, 菜单不声不响退回原样。
    """
    monkeypatch.setattr(config, "USE_CLI_WORKSPACE", True)
    boot = products.boot_script("codex")
    assert "models_cache.json" in boot, "没写型号清单, 菜单还是 codex 自带那份"
    assert "codex debug models --bundled" in boot or "'--bundled'" in boot
    assert "codex','--version'" in boot.replace(" ", ""), "版本号要现问, 不能写死"
    for m in model_catalog.catalog().values():
        if str(m.get("provider")) == "OpenAI":
            assert m["id"] in boot, f"在售的 {m['id']} 没进菜单"
    # 这一段只属于 codex: claude 那格走环境变量, 不该多这段开机脚本
    assert "models_cache.json" not in products.boot_script("claude-code")


@pytest.mark.parametrize("pid", ("claude-code", "codex"))
def test_both_slots_move_together(slot, monkeypatch, pid):
    """两格共用一个开关。只切一格的话, 用户在两个产品间来回时看到两套界面。"""
    monkeypatch.setattr(config, "USE_CLI_WORKSPACE", True)
    p = products.get(pid)
    assert "workspace-cli" in p.image and p.port == 8787
    assert products.env_for(pid, "TOK", "")["PI_WEB_ENGINE"] == (
        "claude" if pid == "claude-code" else "codex"
    )


# ── OpenManus 也跟着同一个开关走 (2026-09-12) ─────────────────────────────────


def test_openmanus_switch_moves_image_port_env_and_boot_together(monkeypatch):
    """与 claude-code / codex 同一条铁律: **四样一起换**。少一样的症状都在
    claude-code 那格踩过 (端口不换 -> 就绪探针超时; 启动脚本不换 -> exitCode 127)。"""
    monkeypatch.setattr(config, "USE_CLI_WORKSPACE", True)
    p = products.get("openmanus")
    env = products.env_for("openmanus", "TOK", "")
    boot = products.boot_script("openmanus")
    # 同一个包的 -openmanus 标签 (不另开包: 新包默认私有, 节点拉不动)
    assert "/workspace-cli:" in p.image and p.image.endswith("-openmanus") and p.image == p.image_ref
    assert p.port == config.CLI_WORKSPACE_PORT == 8787
    assert env["PI_WEB_ENGINE"] == "openmanus"
    assert env["DSH_GATEWAY_BASE"] and env["DSH_CLOUD_TOKEN"] == "TOK"
    assert "exec node dist/server/index.js" in boot
    assert "uvicorn" not in boot, "还在起 agentui 的 Python 外壳"
    # 引擎自己写 config.toml —— 启动脚本别再写一份, 两份谁生效说不清。
    assert "config/config.toml" not in boot
    # 工作目录软链那条不能丢 (它的 workspace_root 写死, 见 _frameworks_boot 的注释)。
    assert "ln -s /workspace /opt/openmanus/workspace" in boot
    assert "[ -L /opt/openmanus/workspace ] ||" in boot


def test_openmanus_switch_off_is_the_frameworks_shell(monkeypatch):
    """开关关着 = 原来那份 (frameworks 镜像 + agentui 外壳 + 8080), 一个字不变。"""
    monkeypatch.setattr(config, "USE_CLI_WORKSPACE", False)
    p = products.get("openmanus")
    env = products.env_for("openmanus", "TOK", "")
    boot = products.boot_script("openmanus")
    assert p.image == config.FRAMEWORKS_IMAGE_REF and p.port == 8080
    assert "PI_WEB_ENGINE" not in env and env["DSH_DEFAULT_CLI"] == "openmanus"
    assert "uvicorn app.main:app" in boot


def test_openmanus_terminal_boots_into_its_own_entry(monkeypatch):
    """点进 [终端] 直接是 OpenManus 自己的交互入口 —— 与 agentui 时代 term_cmd 一致。"""
    monkeypatch.setattr(config, "USE_CLI_WORKSPACE", True)
    env = products.env_for("openmanus", "TOK", "")
    assert env["PI_WEB_TERMINAL_BOOT_CMD"].endswith("python main.py")
    assert env["PI_WEB_DEFAULT_THEME"] == "translucent"
