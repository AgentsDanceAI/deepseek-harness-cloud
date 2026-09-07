"""BoxBackend 的接线守卫 (每人一台云电脑, docs/design/personal-box.md)。

钉的全是**静默错法** —— 每一条漏了都不会在日志里说自己错了:
  · 隧道地址复用: 两台盒子同一个地址, WireGuard 随机丢一台的包, 两边都不报错
  · 忘了带 org 头: 请求记到个人钱包上, 症状是"付过钱却只能开 2 台"
  · 端口绑 0.0.0.0: 把用户的工作台开到公网, 而产品里一律没有第二道登录墙
  · 用户数据放错地方: /data 不进快照, 停一次机就没
  · 栈产品悄悄起一半: 宁可明确拒绝, 也不要让用户对着转圈
"""

from __future__ import annotations

import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="dhc-box-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import boxbackend, config, db, products
from app.workbackend import backend_named


def test_backend_named_knows_box():
    assert isinstance(backend_named("box"), boxbackend.BoxBackend)


def test_unknown_backend_still_rejected():
    with pytest.raises(ValueError):
        backend_named("nope")


def test_tunnel_ip_never_reuses_and_skips_reserved():
    """.1 是应用机, .2 是备用节点 —— 池子必须从 .10 起, 且不给出已占用的。"""
    first = boxbackend._next_tunnel_ip(set())
    assert first.endswith(".10")
    assert boxbackend._next_tunnel_ip({first}) != first
    # 全占满要报错, 不能绕回去发一个已经在用的
    taken = {f"{config.BOX_TUNNEL_NET}.{n}" for n in range(10, 250)}
    with pytest.raises(boxbackend.BoxError):
        boxbackend._next_tunnel_ip(taken)


def test_data_root_is_under_home_user():
    """/data 不进 Box 的快照 (2026-09-07 实测), 放那儿停一次机数据就没。"""
    assert boxbackend.DATA_ROOT.startswith("/home/user/")


def test_org_header_is_sent(monkeypatch):
    monkeypatch.setattr(config, "BOX_API_KEY", "k")
    monkeypatch.setattr(config, "BOX_ORG", "team_abc")
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, headers=None, content=None):
            seen.update(headers or {})
            return _Resp()

    monkeypatch.setattr(boxbackend.httpx, "AsyncClient", _Client)
    import asyncio

    asyncio.run(boxbackend.BoxBackend()._api("GET", "/limits"))
    assert seen.get("X-Box-Org") == "team_abc", "不带 org 就记在个人钱包上, 而且不会报错"


def test_no_key_fails_loudly(monkeypatch):
    monkeypatch.setattr(config, "BOX_API_KEY", "")
    import asyncio

    with pytest.raises(boxbackend.BoxError):
        asyncio.run(boxbackend.BoxBackend()._api("GET", "/limits"))


def test_stack_products_are_refused_not_half_started(monkeypatch):
    """Dify/Coze 那种十容器栈在盒子里要一整套编排, 不是一个 docker run。"""
    import asyncio

    b = boxbackend.BoxBackend()
    with pytest.raises(boxbackend.BoxError) as e:
        asyncio.run(b.create("u1~dify", boot="x", env={}, boot_fp="fp", image="img",
                             sidecars=({"name": "db"},)))
    assert "栈产品" in str(e.value)


def test_container_name_is_per_product():
    assert boxbackend.container_name("claude-code") == "dsh-claude-code"
    assert boxbackend.container_name("pi") != boxbackend.container_name("codex")


def test_user_boxes_table_exists_and_ip_is_unique():
    db.ensure_schema()
    now = db.now()
    with db.tx() as conn:
        conn.execute("DELETE FROM user_boxes")
        conn.execute("INSERT INTO user_boxes (user_id,box_id,tunnel_ip,box_type,state,created,updated) "
                     "VALUES (?,?,?,?,?,?,?)", ("u1", "bx_1", "10.99.1.10", "default", "", now, now))
    rows = db.query("SELECT * FROM user_boxes WHERE user_id=?", ("u1",))
    assert rows and rows[0]["tunnel_ip"] == "10.99.1.10"
    # 同一个地址不能发给第二个人 —— 撞了之后 WireGuard 是随机丢包, 查起来极难
    with pytest.raises(Exception):
        with db.tx() as conn:
            conn.execute("INSERT INTO user_boxes (user_id,box_id,tunnel_ip,box_type,state,created,updated) "
                         "VALUES (?,?,?,?,?,?,?)", ("u2", "bx_2", "10.99.1.10", "default", "", now, now))
    with db.tx() as conn:
        conn.execute("DELETE FROM user_boxes")


def test_split_key_roundtrip_is_what_backend_relies_on():
    """BoxBackend 全靠 split_key 从工作台键里取出 (用户, 产品) —— 一个用户一台机器,
    但一格一个容器。这个契约变了整个后端都要跟着改。"""
    key = products.wskey("u1", "claude-code")
    assert products.split_key(key) == ("u1", "claude-code")
