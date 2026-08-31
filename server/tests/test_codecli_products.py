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
    for retired in ("allowInsecureAuth",):
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
    assert "dangerouslyDisableDeviceAuth" in boot
