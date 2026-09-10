"""本机运行计划接口 —— 目录与编排都由服务端出, 客户端只负责执行。

这一层存在的理由是消掉第二份真相: `scripts/local/aistore-local.py` 原先硬编码
着五格和它们的镜像 tag, 而镜像每次重建都在动 (2026-09-10 一天五个)。抄一份的
下场不是报错, 是拉到过期镜像然后一切"正常"。
"""

from __future__ import annotations

import json
import os
import tempfile

os.environ["DHC_DEV"] = "1"
os.environ["AUTH_SECRET"] = "test"
os.environ.setdefault("DHC_DATA_DIR", tempfile.mkdtemp(prefix="dhc-local-test-"))

import pytest
from fastapi.testclient import TestClient

from app import local_api, products
from app.main import app

from ._signup import signup


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def signed_in(client):
    signup(client, "local-plan@test.local")
    return client


def test_plan_carries_no_credentials(signed_in):
    """**计划不是凭据。** 该填令牌的地方必须是占位符, 而不是真令牌。

    这个端点如果把调用者的令牌回显进 env, 它就从"发编排"变成了"发能力" ——
    一个只该读目录的调用方会凭空拿到一把能花钱的钥匙。占位符让这件事在结构上
    不可能发生, 而不是靠调用方自觉。
    """
    r = signed_in.get("/api/local/plan/codex")
    assert r.status_code == 200, r.text
    plan = r.json()
    main = next(c for c in plan["containers"] if c["role"] == "main")
    assert main["env"]["DSH_CLOUD_TOKEN"] == "${AISTORE_TOKEN}"

    # 整份计划里不许出现任何像令牌的东西。会话 cookie 是最容易被顺手带出去的那个。
    blob = json.dumps(plan, ensure_ascii=False)
    for cookie in signed_in.cookies.values():
        assert cookie not in blob, "计划里回显了调用方的会话凭据"


def test_plan_image_and_boot_come_from_the_registry(signed_in):
    """镜像与启动命令必须与 products.py 同源 —— 这是这个端点的全部意义。"""
    p = products.get("codex")
    plan = signed_in.get("/api/local/plan/codex").json()
    main = next(c for c in plan["containers"] if c["role"] == "main")
    assert main["image_ref"] == (p.image_ref or p.image)
    assert main["port"] == p.port
    assert plan["ready_path"] == p.ready_path
    # boot_script 是一段 shell, 必须交给 sh -c —— 当 argv 传会直接 exec 失败。
    assert main["cmd"][:2] == ["sh", "-c"]
    assert main["cmd"][2] == products.boot_script("codex")


def test_stack_products_are_described_but_flagged(signed_in):
    """多容器栈的编排照样给全, 只是标明本机运行器还起不动。

    先把数据备齐、执行端跟上就能用; 反过来(接口先不给)会逼客户端再抄一遍。
    """
    plan = signed_in.get("/api/local/plan/dify").json()
    # 多容器栈现在能跑了 (共享网络命名空间), 但 reason 要说清楚要起几个
    assert plan["runnable"] == "ready"
    assert "个容器的栈" in plan["reason"]
    roles = [c["role"] for c in plan["containers"]]
    assert roles.count("main") == 1
    assert roles.count("sidecar") == len(products.get("dify").sidecars) > 1
    # 组内共享网络命名空间 —— 上游那些写死 127.0.0.1 的配置全靠这一条成立。
    assert {c["network"] for c in plan["containers"] if c["role"] == "sidecar"} == {"share:main"}


def test_locked_product_needs_a_pass(signed_in, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "WORK_LOCKED_PRODUCTS", "codex,dify")
    assert signed_in.get("/api/local/plan/codex").status_code == 402, "上锁的格子没买通行证也发了计划"
    # 权限和"运行器起不动"是两件事: dify 是 11 个容器的栈, 买了通行证也起不动。
    # 混成一个状态就会提示用户去买一个买完也没用的东西。
    assert signed_in.get("/api/local/plan/dify").status_code == 402
    row = next(x for x in signed_in.get("/api/local/catalog").json()["products"] if x["id"] == "dify")
    assert row["locked"] is True and row["runnable"] == "ready"


def test_catalog_lists_every_enabled_product(signed_in):
    body = signed_in.get("/api/local/catalog").json()
    assert {p["id"] for p in body["products"]} == {p.id for p in local_api.available()}
    assert body["gateway"].startswith("http")
    # 能跑的排前面 —— 这一页用户是从上往下读的
    states = [p["runnable"] for p in body["products"]]
    assert states == sorted(states, key=lambda s: s != "ready")


def test_plan_requires_auth(client):
    assert client.get("/api/local/plan/codex").status_code in (401, 403)


def test_display_name_follows_the_shelf_catalog(signed_in):
    """展示名以 apps_catalog 为准, 不是 products.name。

    2026-09-09 那次改名 (DSH → DeepSeek Harness、pi → Pi Agent) 只落进了
    apps_catalog —— 于是首页写「DeepSeek Harness」, 桌面端货架写「DSH」, 同一个
    产品两个叫法, 靠一张截图才发现。
    """
    from app import apps_catalog, products

    body = signed_in.get("/api/local/catalog").json()
    names = {p["id"]: p["name"] for p in body["products"]}
    assert names.get("dsh") == "DeepSeek Harness"
    assert names.get("pi") == "Pi Agent"
    # 不是只钉这两个: 凡是货架目录里有的, 名字必须一致
    for entry in apps_catalog.CATALOG:
        if entry.id in names:
            assert names[entry.id] == entry.name, f"{entry.id} 两处叫法不一致"
    assert products.get("dsh").name != names["dsh"], "products.name 还是旧的, 这条用例才有意义"


def test_stack_products_get_a_real_autologin_secret(signed_in):
    """栈产品的免登录口令不能是空的。

    2026-09-10 本机跑 hermes 撞到: `_containers` 没把 secret 传下去, 于是
    `HERMES_PASS` 是空串, 免登录脚本看到空口令直接 exit 0 不写就绪标记 ——
    三个容器全起来了、`/__dsh_ready` 永远 503、页面永远「启动中」, 一行错都没有。
    与 2026-09-09 Dify 那次同一形状 (见 dsh-workspace-readiness-gate)。
    """
    plan = signed_in.get("/api/local/plan/hermes").json()
    main = next(c for c in plan["containers"] if c["role"] == "main")
    assert main["env"].get("HERMES_PASS"), "免登录口令是空的 —— 就绪探针会永远 503"
    assert main["env"].get("HERMES_USER") == "owner"
