"""工作台跑在「每人一台云电脑」上 —— ascii.dev Box 后端。

与另外三个后端 (docker / eci / k8s, 见 workbackend.py) 的根本区别: 那三个的单位是
**一个 (用户, 产品) 一个容器**, 这个的单位是**一个用户一台机器**, 产品是那台机器里的
容器。设计与实测见 docs/design/personal-box.md。

一句话链路:

    浏览器 → Caddy → <盒子的隧道地址>:<产品端口> → 盒子里的产品容器

隧道地址这一段是关键: 上游契约 (`WorkInfo.host` + 产品端口) 和 k8s 后端**完全一样**,
所以 Caddy、就绪探针、会话注入那一整层一行都不用改。盒子经 WireGuard 出站拨到应用机
(deploy/box-node/tunnel-wg-144.sh), 它的公网地址每次 resume 都会变而隧道地址不变 ——
这正是当初选 WireGuard 而不是 ssh 隧道的原因。

实测的三个数 (2026-09-07, 同一个产品同一个 boot 脚本):

    今天 k8s 建 Pod → 就绪          2.3 秒
    盒子**开着**时起容器 → 就绪     2.8 秒     ← 开一格没有退步
    盒子**停着**时多付的一次开机    ~20 秒     ← 唯一的新增, 靠预测性唤醒消掉

停机免费, 所以"闲置回收"在这里是真的省钱 —— 但也因此, 用户回来的第一次要等那 20 秒。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time

import httpx

from . import config, db
from .workbackend import Backend, WorkInfo

log = logging.getLogger("dhc.box")

#: 盒子里跑产品容器的固定名字 —— 一个用户一台机器, 一格一个容器。
CONTAINER_PREFIX = "dsh-"
#: 用户数据在盒子里的根。**必须在 /home/user 下**: 实测 /data 不进快照, 停一次机就没
#: (2026-09-07, 见 docs/design/personal-box.md 决策 3)。
DATA_ROOT = "/home/user/dsh"
READY_STATES = ("ready", "idle", "running")


class BoxError(RuntimeError):
    pass


def _api_base() -> str:
    return (config.BOX_API_BASE or "https://ascii.dev/api/box/v1").rstrip("/")


def container_name(product_id: str) -> str:
    return CONTAINER_PREFIX + product_id


def _row(user_id: str) -> dict | None:
    r = db.query_one("SELECT * FROM user_boxes WHERE user_id=?", (user_id,))
    return dict(r) if r else None


def _next_tunnel_ip(taken: set[str]) -> str:
    """隧道地址池。.1 是应用机, .2 是备用节点, 用户从 .10 起。

    地址用完不能悄悄复用 —— 两台盒子拿到同一个地址时, WireGuard 会把两者的
    AllowedIPs 都指到那里, 结果是**随机**有一台收不到包, 而两边都不报错。
    """
    net = config.BOX_TUNNEL_NET or "10.99.1"
    for n in range(10, 250):
        ip = f"{net}.{n}"
        if ip not in taken:
            return ip
    raise BoxError("隧道地址池用完了 (一个 /24 只有 240 个可用)")


class BoxBackend(Backend):
    """一个用户一台 Box; 产品是盒子里的 docker 容器。"""

    #: 盒子能停机再开机且盘还在 —— 与 docker 后端同, 和 eci/k8s 的"只能建和删"不同。
    resumable = True
    #: 盒子开着时 2–3 秒; 停着时要先开机。等待页按最坏情况说, 别让人以为卡死了。
    boot_hint = "3 秒 (电脑休眠时约 25 秒)"

    def __init__(self) -> None:
        self._key = (config.BOX_API_KEY or "").strip()
        self._org = (config.BOX_ORG or "").strip()

    # -- transport ----------------------------------------------------------
    async def _api(
        self, method: str, path: str, body: dict | None = None, timeout: float = 90.0
    ) -> tuple[int, dict]:
        if not self._key:
            raise BoxError("BOX_API_KEY 没配")
        headers = {"authorization": f"Bearer {self._key}", "content-type": "application/json"}
        # ⚠️ 不带 org = 这次请求记在**个人钱包**上, 而订阅多半买在组织上 —— 症状是
        # "明明付过钱却只能开 2 台", 而且没有任何一处会说破 (2026-09-06 栽过)。
        if self._org:
            headers["X-Box-Org"] = self._org
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, _api_base() + path, headers=headers,
                                content=json.dumps(body) if body is not None else None)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {}

    async def _run(self, box_id: str, command: str, timeout: float = 180.0) -> tuple[int, str]:
        """在盒子里跑一条命令。

        ⚠️ 它以盒子里的 `user` 身份跑, 不是 root —— 读 root 权限的文件会拿到**空串
        而不是报错** (2026-09-07 栽过: 读 /opt/dsh-wg/dsh0.pub 拿到空串, 一路静默传到
        登记对端才炸)。要 root 就自己写 sudo。
        """
        st, body = await self._api("POST", f"/boxes/{box_id}/commands", {"command": command}, timeout)
        if st >= 300:
            raise BoxError(f"命令失败 ({st}): {str(body.get('message') or '')[:200]}")
        return int(body.get("exitCode") or 0), str(body.get("stdout") or "") + str(body.get("stderr") or "")

    # -- 盒子生命周期 --------------------------------------------------------
    async def _state(self, box_id: str) -> str:
        st, body = await self._api("GET", f"/boxes/{box_id}", timeout=30)
        if st == 404:
            return "deleted"
        return str(((body or {}).get("box") or {}).get("state") or "unknown").lower()

    async def _wake(self, box_id: str, budget_s: float = 180.0) -> str:
        """开机并等到能用。

        ⚠️ **不能拿 API 的 state 当就绪信号** —— 实测状态 7–9 秒就报 ready, 而服务还要
        再等 10–15 秒 (docs/design/personal-box.md)。这里只等到"命令通道能用", 产品是否
        就绪由上层探 Product.ready_path。
        """
        t0 = time.time()
        state = "unknown"
        while time.time() - t0 < budget_s:
            state = await self._state(box_id)
            if state in READY_STATES:
                try:
                    code, _ = await self._run(box_id, "echo ok", timeout=30)
                    if code == 0:
                        return state
                except BoxError:
                    pass
            elif state in ("archived", "stopped"):
                await self._api("POST", f"/boxes/{box_id}/resume", timeout=60)
            elif state in ("error", "deleted"):
                return state
            await asyncio.sleep(2.5)
        return state

    async def _ensure_box(self, user_id: str) -> dict:
        row = _row(user_id)
        if row:
            return row
        taken = {str(r["tunnel_ip"]) for r in db.query("SELECT tunnel_ip FROM user_boxes")}
        ip = _next_tunnel_ip(taken)
        st, body = await self._api(
            "POST", "/boxes",
            {"type": config.BOX_TYPE or "default", "ttlSeconds": config.BOX_TTL_SECONDS or None,
             **({"from": config.BOX_TEMPLATE} if config.BOX_TEMPLATE else {})},
            timeout=180,
        )
        if st >= 300:
            raise BoxError(f"开盒子失败 ({st}): {str((body or {}).get('message') or '')[:200]}")
        box_id = str(((body or {}).get("box") or {}).get("id") or "")
        if not box_id:
            raise BoxError("开盒子的返回里没有 id")
        now = db.now()
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO user_boxes (user_id,box_id,tunnel_ip,box_type,state,created,updated) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, box_id, ip, config.BOX_TYPE or "default", "creating", now, now),
            )
        log.info("[box] 给 %s 开了 %s, 隧道地址 %s", user_id, box_id, ip)
        return _row(user_id) or {}

    def _touch(self, user_id: str, **cols) -> None:
        if not cols:
            return
        cols["updated"] = db.now()
        sets = ", ".join(f"{k}=?" for k in cols)
        with db.tx() as conn:
            conn.execute(f"UPDATE user_boxes SET {sets} WHERE user_id=?", (*cols.values(), user_id))

    # -- Backend 契约 --------------------------------------------------------
    async def inspect(self, user_id: str) -> WorkInfo | None:
        from .products import split_key

        uid, pid = split_key(user_id)
        row = _row(uid)
        if not row:
            return None
        state = await self._state(row["box_id"])
        if state == "deleted":
            with db.tx() as conn:
                conn.execute("DELETE FROM user_boxes WHERE user_id=?", (uid,))
            return None
        if state not in READY_STATES:
            # 盒子睡着: 有这台电脑, 但这一格没在跑。host 照给 —— 上层拿它探就绪,
            # 探不通就走启动等待页, 那正是我们要的表现。
            return WorkInfo(False, "", "", str(row["tunnel_ip"]), state)
        code, out = await self._run(
            row["box_id"],
            "sudo docker inspect -f "
            "'{{.State.Running}} {{.Config.Image}} {{index .Config.Labels \"dsh.bootfp\"}}' "
            f"{shlex.quote(container_name(pid))} 2>/dev/null || true",
        )
        parts = (out or "").strip().split()
        running = bool(parts) and parts[0] == "true"
        image_id = parts[1] if len(parts) > 1 else ""
        boot_fp = parts[2] if len(parts) > 2 else ""
        return WorkInfo(running, boot_fp, image_id, str(row["tunnel_ip"]), state)

    async def current_image_id(self, image: str) -> str:
        # 盒子里认的就是镜像引用本身 (带 tag)。拿它与 WorkInfo.image_id 比, 换了 tag
        # 就判过期 —— 与 k8s 后端按 digest 比不同, 但对"换版要重建"这个判定够用。
        return image or ""

    async def create(self, user_id: str, *, boot: str, env: dict, boot_fp: str, image: str,
                     image_ref: str = "", mem_mb: int = 0, cpus: float = 0.0, sidecars: tuple = (),
                     host_aliases: tuple = (), init_containers: tuple = (), seeds: tuple = (),
                     run_as_user: int | None = None) -> None:
        from .products import split_key

        uid, pid = split_key(user_id)
        if sidecars:
            # 栈产品 (Dify/Coze 那种十容器) 第一版不接: 它们在盒子里要一整套编排,
            # 而不是一个 docker run。宁可明确拒绝, 也不要起一半让用户看转圈。
            raise BoxError(f"{pid} 是栈产品, Box 后端还不支持 (需要盒内编排)")
        row = await self._ensure_box(uid)
        state = await self._wake(row["box_id"])
        if state not in READY_STATES:
            raise BoxError(f"电脑开不了机 (状态 {state})")

        ref = image_ref or image
        name = container_name(pid)
        home = f"{DATA_ROOT}/{pid}/home"
        ws = f"{DATA_ROOT}/{pid}/workspace"
        envs = " ".join("-e " + shlex.quote(f"{k}={v}") for k, v in (env or {}).items())
        script = "\n".join([
            "set -e",
            f"sudo docker rm -f {shlex.quote(name)} >/dev/null 2>&1 || true",
            f"sudo install -d {shlex.quote(home)} {shlex.quote(ws)} {shlex.quote(DATA_ROOT + '/shared')}",
            # 镜像不在就拉。分层修好之后换版只拉增量 (见 docs/design/personal-box.md),
            # 所以这一步平时是毫秒级, 只有真换了版才花时间。
            f"sudo docker image inspect {shlex.quote(ref)} >/dev/null 2>&1 "
            f"|| sudo docker pull -q {shlex.quote(ref)}",
            # 端口只绑在隧道地址上 —— 绑 0.0.0.0 等于把用户的工作台开到公网, 而产品
            # 里一律没有第二道登录墙。
            f"sudo docker run -d --name {shlex.quote(name)} --restart=always "
            f"-p {row['tunnel_ip']}:{{port}}:{{port}} {envs} "
            f"--label dsh.bootfp={shlex.quote(boot_fp)} "
            f"-v {shlex.quote(home)}:/root -v {shlex.quote(ws)}:/workspace "
            f"-v {shlex.quote(DATA_ROOT + '/shared')}:/shared "
            f"{shlex.quote(ref)} sh -c {shlex.quote(boot)}",
        ])
        from .products import registry

        port = registry()[pid].port
        code, out = await self._run(row["box_id"], script.replace("{port}", str(port)), timeout=600)
        if code != 0:
            raise BoxError(f"起 {pid} 失败: {out[-300:]}")
        self._touch(uid, state="running")
        log.info("[box] %s 在 %s 上起了 %s", uid, row["box_id"], pid)

    async def start(self, user_id: str) -> None:
        """没有单独的 start —— create 里已经把容器起起来了 (与 eci/k8s 同)。"""
        return None

    async def release(self, user_id: str) -> None:
        """闲置回收: 停掉这一格的容器; 这台电脑上没别的格子在跑就顺手停机 (停机免费)。"""
        from .products import split_key

        uid, pid = split_key(user_id)
        row = _row(uid)
        if not row:
            return
        if await self._state(row["box_id"]) not in READY_STATES:
            return
        try:
            stop = f"sudo docker stop -t 30 {shlex.quote(container_name(pid))} >/dev/null 2>&1 || true"
            await self._run(row["box_id"], stop)
            code, out = await self._run(row["box_id"], "sudo docker ps -q | wc -l")
            if code == 0 and out.strip().splitlines()[-1].strip() == "0":
                await self._api("POST", f"/boxes/{row['box_id']}/stop", timeout=120)
                self._touch(uid, state="archived")
                log.info("[box] %s 的电脑没别的格子在跑, 已停机 (免费)", uid)
        except BoxError:
            log.warning("[box] 回收 %s 出错", user_id, exc_info=True)

    async def destroy(self, user_id: str) -> None:
        from .products import split_key

        uid, pid = split_key(user_id)
        row = _row(uid)
        if not row:
            return
        try:
            rm = f"sudo docker rm -f {shlex.quote(container_name(pid))} >/dev/null 2>&1 || true"
            await self._run(row["box_id"], rm)
        except BoxError:
            pass  # 盒子可能停着 —— 容器本来就不在, 没什么可删的

    async def running_users(self) -> list[str]:
        """计量与回收按人遍历, 少一个就是有人白用 —— 所以这里问的是每台开着的电脑上
        实际在跑哪几格, 而不是数据库里记了什么。"""
        from .products import wskey

        out: list[str] = []
        for r in db.query("SELECT user_id, box_id FROM user_boxes"):
            uid, box_id = r["user_id"], r["box_id"]
            try:
                if await self._state(box_id) not in READY_STATES:
                    continue
                code, txt = await self._run(box_id, "sudo docker ps --format '{{.Names}}'", timeout=60)
            except BoxError:
                continue
            if code != 0:
                continue
            for line in txt.splitlines():
                n = line.strip()
                if n.startswith(CONTAINER_PREFIX):
                    out.append(wskey(uid, n[len(CONTAINER_PREFIX):]))
        return out

    def capacity_reason(self) -> str:
        return ""
