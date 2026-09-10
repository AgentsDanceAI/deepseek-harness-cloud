"""把服务端那份产品拓扑翻译成一份**本机可执行的计划**。

为什么这件事必须在服务端做: `products.py` 已经把 16 格的完整拓扑写成了数据 ——
主容器、sidecar 栈、初始化容器、就绪路径、网络别名、种子文件。客户端再抄一份就
是第二份真相, 而镜像 tag 一直在动 (2026-09-10 一天里重建了五个工作台镜像)。抄一
份的下场不是报错, 是拉到一个过期镜像然后一切"正常"。

`scripts/local/aistore-local.py` 原来就硬编码着五格和它们的 tag —— 那份表在写下
来的当天就已经旧了。这个端点是来收掉它的。

**计划里不含任何凭据。** 需要令牌的地方一律写成 `${AISTORE_TOKEN}`, 由本机那一侧
自己替换成它手上那个设备令牌。所以这个端点不发放任何新能力, 只发编排 —— 拿到计划
的人并不因此多出调用网关的资格。

组内网络语义与云端一致: sidecar **共享主容器的网络命名空间** (k8s pod 语义),
彼此用 127.0.0.1 互访。Docker 上对应 `--network container:<主容器>`; compose 里
是 `network_mode: "service:<主容器>"`。用 compose 默认的服务名 DNS 会把上游那些
写死 127.0.0.1 的配置全部打穿。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException

from . import config, products, work_access
from .accounts import resolve_user

router = APIRouter(prefix="/api/local", tags=["local"])

#: 令牌占位符。计划里凡是该填令牌的地方都是这个串, 本机替换。
TOKEN_PLACEHOLDER = "${AISTORE_TOKEN}"

#: 本机运行器目前只会起单容器。多容器栈 (Dify 11 个、Coze 10 个、Hermes 2 个)
#: 的编排信息计划里照样给全 —— 先把数据备齐, 执行端跟上就能直接用。
_SINGLE_ONLY = "runner_no_stack"


def available() -> list[products.Product]:
    """本机能考虑跑的格子。

    与 `products.enabled()` 只差一条: **不看 domain**。domain 是这个产品在云端的
    工作台子域 —— 本机跑根本不经过它, 而自部署实例可能一个子域都没配, 镜像却照样
    拉得动。其余判据(没被下架、主镜像与初始化镜像齐全)与云端同源。
    """
    off = products.disabled_ids()
    return [
        p
        for p in products.registry().values()
        if p.id not in off and p.image and all(ic.image_ref for ic in p.init_containers)
    ]


def _runnable(p: products.Product) -> tuple[str, str]:
    """**运行器**能不能起这一格 —— 只谈技术, 不谈资格。返回 (状态, 人话)。

    权限单独一个字段: 把"起不动"和"没买"混成一个状态, 就会出现"这格是 11 个
    容器的栈"却提示用户去买通行证 —— 买完照样起不动。
    """
    if p.sidecars or p.init_containers:
        n = 1 + len(p.sidecars) + len(p.init_containers)
        return _SINGLE_ONLY, f"{n} 个容器的栈, 本机运行器还只会起单容器"
    return "ready", ""


def _locked_for(p: products.Product, user: dict) -> bool:
    """这一格上了锁而这个人没有通行证。"""
    return products.is_locked(p.id) and not work_access.can_open_locked(user, p.id)


def _containers(p: products.Product) -> list[dict]:
    """主容器 + 伴随容器, 按启动顺序。"""
    out: list[dict] = []
    for ic in products.resolve_init_containers(p.init_containers, token=TOKEN_PLACEHOLDER):
        d = asdict(ic)
        d.update(role="init", network="own")
        out.append(d)
    out.append(
        {
            "role": "main",
            "name": p.id,
            # image_ref 是完整仓库地址; 留空才回落到 image (与 workbackend 同一判据)。
            "image_ref": p.image_ref or p.image,
            # boot_script 是一段 shell, 不是 argv —— 必须由 sh -c 执行。
            "cmd": ["sh", "-c", products.boot_script(p.id)],
            "args": [],
            "env": products.env_for(p.id, TOKEN_PLACEHOLDER),
            "port": p.port,
            "network": "own",
            "run_as_user": p.run_as_user,
        }
    )
    for sc in p.sidecars:
        d = asdict(sc)
        d.update(role="sidecar", network="share:main")
        d["env"] = dict(d.get("env") or ())
        out.append(d)
    return out


@router.get("/catalog")
def catalog(user: dict = Depends(resolve_user)):
    """十六格里哪些能在这台机器上跑, 不能的那些为什么。"""
    items = []
    for p in available():
        state, why = _runnable(p)
        items.append(
            {
                "id": p.id,
                "name": p.name,
                "port": p.port,
                "mem_mb": p.mem_mb,
                "runnable": state,
                "reason": why,
                "locked": _locked_for(p, user),
                "containers": 1 + len(p.sidecars) + len(p.init_containers),
            }
        )
    items.sort(key=lambda x: (x["runnable"] != "ready", x["id"]))
    return {"products": items, "gateway": config.PUBLIC_BASE.rstrip("/")}


@router.get("/plan/{product_id}")
def plan(product_id: str, user: dict = Depends(resolve_user)):
    """一格的完整启动计划。凭据用 ${AISTORE_TOKEN} 占位, 本机自己替换。"""
    p = products.get(product_id)
    if p is None or p.id not in {x.id for x in available()}:
        raise HTTPException(404, "unknown_product")
    if _locked_for(p, user):
        raise HTTPException(402, "要先买这一格的通行证")
    state, why = _runnable(p)
    return {
        "product": p.id,
        "name": p.name,
        "port": p.port,
        "ready_path": p.ready_path,
        "gateway": config.PUBLIC_BASE.rstrip("/"),
        "token_placeholder": TOKEN_PLACEHOLDER,
        "runnable": state,
        "reason": why,
        "host_aliases": list(p.host_aliases),
        "seeds": [list(s) for s in p.seeds],
        "containers": _containers(p),
    }
