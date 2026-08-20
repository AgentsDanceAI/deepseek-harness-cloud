"""Where a user's workspace container actually runs.

Two backends, same contract:

  docker  本机 (或自部署) 的 docker 引擎, 经受限 socket 代理。容器可以 stop 后
          再 start, 命名卷留在原地, 恢复只要几秒。
  eci     阿里云弹性容器实例。**没有"停止但保留"这个状态** —— 只能创建和删除,
          于是"闲置回收"等于销毁, 而恢复是一次完整冷启动 (实测中位数 19s)。
          正因如此, ECI 后端下 /root 与 /workspace 必须落在 NAS 上: 容器一删,
          容器内的任何东西都不再存在。

抽象出这层不是为了好看, 是因为 deploy/selfhost/ 那条路只有 docker: 把 docker
换掉等于把自部署删掉。两边都实现同一个 Backend, 由 WORK_BACKEND 选。
"""
from __future__ import annotations

import abc
import base64
import hashlib
import hmac
import logging
import re
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from . import config

log = logging.getLogger("dhc.work")


def cname(user_id: str) -> str:
    """Workspace name. Doubles as a docker-DNS hostname for Caddy's dynamic
    upstream, and as the ECI ContainerGroupName — both want the same charset."""
    return "dshwork-" + re.sub(r"[^a-zA-Z0-9]", "", user_id)


@dataclass(frozen=True)
class WorkInfo:
    """What the app needs to know about one workspace, backend-independent."""
    running: bool
    boot_fp: str      # boot-script digest stamped at create time
    image_id: str     # whatever identifies the image THIS instance was born from
    host: str         # hostname or IP that answers on :3081
    state: str = ""   # backend's own word for it, for /api/work/status only


class Backend(abc.ABC):
    #: docker can stop-and-resume; ECI cannot (create/delete only)
    resumable: bool = True

    @abc.abstractmethod
    async def inspect(self, user_id: str) -> WorkInfo | None: ...

    @abc.abstractmethod
    async def current_image_id(self) -> str:
        """Identity of the image a NEW workspace would be born from. Compared
        against WorkInfo.image_id to spot a workspace left on an old runtime.
        Empty string means "could not resolve" — callers treat that as
        not-stale, because destroying a working container over a failed lookup
        is far worse than running one build behind."""

    @abc.abstractmethod
    async def create(self, user_id: str, *, boot: str, env: dict[str, str],
                     boot_fp: str) -> None: ...

    @abc.abstractmethod
    async def start(self, user_id: str) -> None: ...

    @abc.abstractmethod
    async def release(self, user_id: str) -> None:
        """Idle reclaim. docker: stop (volumes stay, resume is seconds).
        eci: delete (state lives on NAS, resume is a cold start)."""

    @abc.abstractmethod
    async def destroy(self, user_id: str) -> None: ...

    @abc.abstractmethod
    async def running_users(self) -> list[str]: ...

    def capacity_reason(self) -> str:
        return ""


# --- docker: the original backend, unchanged in behaviour --------------------

LABEL = "dshwork.user"
# The boot script is baked into the container's Cmd at CREATE time, so an
# existing container keeps rewriting the settings.yaml it was born with — the
# catalog grew from 2 models to 20 and every already-provisioned workspace
# stayed on the old two. Stamping the script's digest lets ensure_workspace spot
# a stale container and rebuild it; /root and /workspace survive, so nothing the
# user made is lost.
CFG_LABEL = "dshwork.bootcfg"


def host_free_mb() -> int | None:
    """宿主可用内存 (MB)。读不到返回 None。

    容器里读 /proc/meminfo 拿到的就是**宿主**的数 (它不做 namespace 隔离, 实测
    MemTotal 与宿主逐字节相同), 所以不需要把 /proc 挂进来或多开一个探针。
    用 MemAvailable 而不是 MemFree: 后者把可回收的 page cache 算成"已用",
    在一台跑了半个月的机器上会永远显示没内存, 于是这道闸门变成永远关闭。"""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        log.exception("[work] 读 /proc/meminfo 失败")
    return None


class DockerBackend(Backend):
    resumable = True

    async def _api(self, method: str, path: str, *, json_body: dict | None = None,
                   params: dict | None = None) -> httpx.Response:
        async with httpx.AsyncClient(base_url=config.DOCKER_PROXY_URL, timeout=30.0) as client:
            return await client.request(method, path, json=json_body, params=params)

    async def inspect(self, user_id: str) -> WorkInfo | None:
        r = await self._api("GET", f"/containers/{cname(user_id)}/json")
        if r.status_code != 200:
            return None
        d = r.json()
        labels = ((d.get("Config") or {}).get("Labels") or {})
        return WorkInfo(
            running=(d.get("State") or {}).get("Status", "") == "running",
            # no stamp at all predates the mechanism -> stale by definition
            boot_fp=labels.get(CFG_LABEL, ""),
            image_id=d.get("Image", ""),
            host=cname(user_id),
            state=(d.get("State") or {}).get("Status", ""),
        )

    async def current_image_id(self) -> str:
        """Resolved image *ID*, not the tag string: the usual upgrade here is
        `docker build -t dsh-local:rc8` — sometimes the same tag rebuilt — and a
        tag comparison would call that unchanged, leaving every existing
        workspace on the old runtime forever."""
        r = await self._api("GET", f"/images/{config.WORK_IMAGE}/json")
        if r.status_code != 200:
            log.warning("[work] 解析不了镜像 %s (%s), 跳过镜像陈旧判定",
                        config.WORK_IMAGE, r.status_code)
            return ""
        return (r.json() or {}).get("Id", "")

    async def create(self, user_id: str, *, boot: str, env: dict[str, str],
                     boot_fp: str) -> None:
        hexid = cname(user_id)[len("dshwork-"):]
        body = {
            "Image": config.WORK_IMAGE,
            "Cmd": ["sh", "-c", boot],
            "WorkingDir": "/workspace",
            "Labels": {LABEL: user_id, CFG_LABEL: boot_fp},
            "Env": [f"{k}={v}" for k, v in env.items()],
            "HostConfig": {
                "Memory": config.WORK_MEM_LIMIT_MB * 1024 * 1024,
                "NanoCpus": int(config.WORK_CPUS * 1e9),
                "PidsLimit": 512,
                # 内存到悬崖时让 OOM killer 先挑工作台: 它可随时重启、卷还在, 而
                # 默认规则是按占用挑, 会先杀同机最大的进程 (postgres /
                # elasticsearch) —— 那是别人的数据库, 而且不是它闯的祸。
                "OomScoreAdj": config.WORK_OOM_SCORE_ADJ,
                "NetworkMode": config.WORK_NETWORK,
                "RestartPolicy": {"Name": "no"},
                "Mounts": [
                    {"Type": "volume", "Source": f"dshwork-home-{hexid}", "Target": "/root"},
                    {"Type": "volume", "Source": f"dshwork-ws-{hexid}", "Target": "/workspace"},
                ],
            },
        }
        r = await self._api("POST", "/containers/create", json_body=body,
                            params={"name": cname(user_id)})
        if r.status_code not in (201, 409):  # 409 = already exists (race)
            raise RuntimeError(f"container create failed: {r.status_code} {r.text[:200]}")

    async def start(self, user_id: str) -> None:
        r = await self._api("POST", f"/containers/{cname(user_id)}/start")
        if r.status_code not in (204, 304):
            raise RuntimeError(f"container start failed: {r.status_code} {r.text[:200]}")

    async def release(self, user_id: str) -> None:
        await self._api("POST", f"/containers/{cname(user_id)}/stop", params={"t": 5})

    async def destroy(self, user_id: str) -> None:
        await self._api("POST", f"/containers/{cname(user_id)}/stop", params={"t": "5"})
        await self._api("DELETE", f"/containers/{cname(user_id)}")

    async def running_users(self) -> list[str]:
        r = await self._api("GET", "/containers/json",
                            params={"filters": '{"label":["%s"]}' % LABEL})
        if r.status_code != 200:
            return []
        return [uid for c in r.json()
                if (uid := (c.get("Labels") or {}).get(LABEL, ""))]

    def capacity_reason(self) -> str:
        """起新工作台前的容量判定, 返回空串表示可以起。

        静态并发上限之外还要看**宿主内存余量** —— 静态上限不知道同机还跑着
        a sibling production system 全栈, 8 × 512M 的额度在对方峰值时就是压垮线。

        内存读不到时**放行**: 与本模块其余闸门同一姿态 (检查自身故障不该拦人),
        而且一个读不到 /proc 的进程更可能是环境异常而不是真的没内存。"""
        free = host_free_mb()
        if free is None:
            return ""
        need = config.WORK_MEM_LIMIT_MB + config.WORK_MIN_FREE_MB
        if free < need:
            log.warning("[work] 宿主可用内存 %dMB < 需要 %dMB (容器 %d + 保留 %d), 拒起新工作台",
                        free, need, config.WORK_MEM_LIMIT_MB, config.WORK_MIN_FREE_MB)
            return f"memory:{free}<{need}"
        return ""


# --- eci: 阿里云弹性容器实例 -------------------------------------------------

_ECI_VERSION = "2018-08-08"
# ECI 的生命周期状态。没有列进这两组的 (Succeeded / Failed / ScheduleFailed /
# Expired / Terminating) 一律当作"不存在", 于是 ensure_workspace 会重建 ——
# 对一个不可 start 的后端来说, "坏了就重建"是唯一能自愈的动作。
_ECI_RUNNING = {"Running"}
_ECI_COMING_UP = {"Pending", "Scheduling", "Updating", "Restarting"}

_TAG_USER = "dshwork-user"
_TAG_BOOTCFG = "dshwork-bootcfg"


def _pe(s) -> str:
    """RFC3986 percent-encoding as阿里云 signs it (+ -> %20, * -> %2A, ~ kept)."""
    return quote(str(s), safe="~-._").replace("+", "%20").replace("*", "%2A")


def _sign(params: dict, secret: str, method: str = "POST") -> str:
    canon = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in sorted(params.items()))
    sts = f"{method}&{_pe('/')}&{_pe(canon)}"
    return base64.b64encode(
        hmac.new((secret + "&").encode(), sts.encode(), hashlib.sha1).digest()
    ).decode()


class EciError(RuntimeError):
    pass


class EciBackend(Backend):
    """ECI 没有 stop/start —— release 就是 delete。

    因此 /root 与 /workspace **必须**落在 NAS 上, 否则闲置回收等于抹掉用户的
    全部文件和会话。WORK_NAS_SERVER 为空时仍可运行 (用于冒烟验证), 但每次创建
    都会告警: 那种形态下工作台是一次性的。
    """
    resumable = False

    def __init__(self) -> None:
        self._ak = config.ECI_ACCESS_KEY_ID
        self._sk = config.ECI_ACCESS_KEY_SECRET
        self._region = config.ECI_REGION_ID
        self._warned_no_nas = False
        self._mount_reported: set[str] = set()

    # -- transport ----------------------------------------------------------
    async def _call(self, action: str, params: dict | None = None) -> dict:
        p = {
            "Action": action,
            "Version": _ECI_VERSION,
            "Format": "JSON",
            "AccessKeyId": self._ak,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": uuid.uuid4().hex,
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "RegionId": self._region,
        }
        p.update({k: v for k, v in (params or {}).items() if v is not None})
        p["Signature"] = _sign(p, self._sk)
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"https://eci.{self._region}.aliyuncs.com/", data=p)
        try:
            body = r.json()
        except ValueError:
            raise EciError(f"{action}: HTTP {r.status_code}, 响应不是 JSON: {r.text[:200]}")
        # 阿里云把错误也放在 200 之外的码里, 但 Code 字段才是权威的 —— 只看
        # status_code 会把 "参数错误" 读成成功。
        if r.status_code != 200 or body.get("Code"):
            raise EciError(f"{action}: {body.get('Code') or r.status_code} "
                           f"{body.get('Message', r.text[:200])}")
        return body

    # -- helpers ------------------------------------------------------------
    def _volume_params(self, user_id: str) -> dict:
        """NAS 卷。空配置时返回 {} —— 容器仍能起, 但一删就什么都不剩。"""
        server = (config.WORK_NAS_SERVER or "").strip()
        if not server:
            if not self._warned_no_nas:
                log.warning("[work] ECI 后端未配置 WORK_NAS_SERVER: 工作台是一次性的, "
                            "闲置回收会抹掉用户的文件与会话")
                self._warned_no_nas = True
            return {}
        hexid = cname(user_id)[len("dshwork-"):]
        # 这个目录**必须已经存在于 NAS 上** —— ECI 不会替你建, 挂载会以
        # "file does not exist" 失败, 而实例只是一直 Pending, 不报错。
        # (SubPath 的嵌套目录倒是会自动创建, 实测过。)
        root = (config.WORK_NAS_PATH or "/").rstrip("/") or "/"
        p = {
            "Volume.1.Name": "dshwork-nas",
            "Volume.1.Type": "NFSVolume",
            "Volume.1.NFSVolume.Server": server,
            "Volume.1.NFSVolume.Path": root,
            "Volume.1.NFSVolume.ReadOnly": "false",
        }
        # SubPath 而不是给每个用户一个 Volume: NFS 的子目录要先存在才挂得上,
        # 而 SubPath 沿用 k8s 语义, 由挂载方按需创建。一个卷 + 两个子路径,
        # 也省掉"用户数 = 卷数"这条会撞上 ECI 卷数量上限的路。
        for i, (sub, path) in enumerate(((f"{hexid}/home", "/root"),
                                         (f"{hexid}/workspace", "/workspace")), start=1):
            p[f"Container.1.VolumeMount.{i}.Name"] = "dshwork-nas"
            p[f"Container.1.VolumeMount.{i}.MountPath"] = path
            p[f"Container.1.VolumeMount.{i}.SubPath"] = sub
        return p

    @staticmethod
    def _tags_of(group: dict) -> dict[str, str]:
        return {t.get("Key", ""): t.get("Value", "") for t in (group.get("Tags") or [])}

    async def _find(self, user_id: str) -> dict | None:
        # 不传 Limit: 实测单独传它会被要求 ContainerGroupId
        # ("MissingParameter ... ContainerGroupId")。按名字查本来也至多一条。
        body = await self._call("DescribeContainerGroups",
                                {"ContainerGroupName": cname(user_id)})
        for g in body.get("ContainerGroups", []):
            if g.get("ContainerGroupName") == cname(user_id):
                return g
        return None

    # -- Backend ------------------------------------------------------------
    async def inspect(self, user_id: str) -> WorkInfo | None:
        g = await self._find(user_id)
        if g is None:
            return None
        status = g.get("Status", "")
        if status not in _ECI_RUNNING and status not in _ECI_COMING_UP:
            # 终态 (Succeeded/Failed/...) 的实例还占着名字, 必须先删掉再重建,
            # 否则 CreateContainerGroup 会一直撞名。
            log.info("[work] %s 处于终态 %s, 清掉以便重建", user_id, status)
            await self.destroy(user_id)
            return None
        if status in _ECI_COMING_UP:
            self._report_stuck_mount(user_id, g)
        else:
            self._mount_reported.discard(user_id)
        containers = g.get("Containers") or [{}]
        return WorkInfo(
            running=status in _ECI_RUNNING,
            boot_fp=self._tags_of(g).get(_TAG_BOOTCFG, ""),
            image_id=containers[0].get("Image", ""),
            host=g.get("IntranetIp", ""),
            state=status,
        )

    def _report_stuck_mount(self, user_id: str, group: dict) -> None:
        """挂载失败不会让实例退出, 它会一直 Pending。

        对上层来说这和"正在启动"长得一模一样, 于是 ensure_workspace 永远返回
        starting, 用户永远看着转圈, 日志里一个字都没有。实测踩过一次:
        WORK_NAS_PATH 指了个 NAS 上不存在的目录 ("file does not exist") ——
        注意 SubPath 的嵌套目录 ECI 会自动建, 但基础 Path 必须先存在。
        """
        if user_id in self._mount_reported:
            return
        for e in group.get("Events") or []:
            msg = e.get("Message", "")
            if e.get("Type") == "Warning" and "MountVolume" in msg:
                self._mount_reported.add(user_id)
                log.error("[work] %s 卡在 %s: 挂载失败 —— %s。"
                          "检查 WORK_NAS_PATH=%r 在 NAS 上是否存在, "
                          "以及挂载点权限组是否放行本交换机网段",
                          user_id, group.get("Status"), msg[:200], config.WORK_NAS_PATH)
                return

    async def current_image_id(self) -> str:
        """ECI 上镜像身份就是仓库引用本身。

        不像本机 docker 能拿到解析后的镜像 ID —— 同名重推 (`:rc8` 内容变了但
        tag 没变) 这里察觉不到。可接受: 工作台镜像走版本号 tag, 而重建镜像缓存
        本来就是发版流程的一步。"""
        return config.WORK_IMAGE_REF or config.WORK_IMAGE

    async def create(self, user_id: str, *, boot: str, env: dict[str, str],
                     boot_fp: str) -> None:
        p = {
            "ContainerGroupName": cname(user_id),
            "ZoneId": config.ECI_ZONE_ID or None,
            "VSwitchId": config.ECI_VSWITCH_ID,
            "SecurityGroupId": config.ECI_SECURITY_GROUP_ID,
            "Cpu": config.WORK_CPUS,
            "Memory": round(config.WORK_MEM_LIMIT_MB / 1024, 2),
            "ComputeCategory.1": config.ECI_COMPUTE_CATEGORY or None,
            "RestartPolicy": "Never",
            # 命中镜像缓存是 50s -> 19s 的全部差别。缓存要在发版时按新镜像重建,
            # 否则这里静默退回全量拉取, 只是慢, 不会报错 —— 也就不会有人发现。
            "AutoMatchImageCache": "true",
            "AutoCreateEip": "true",
            "EipBandwidth": config.ECI_EIP_BANDWIDTH,
            "Container.1.Name": "dsh",
            "Container.1.Image": config.WORK_IMAGE_REF or config.WORK_IMAGE,
            "Container.1.WorkingDir": "/workspace",
            "Container.1.Command.1": "sh",
            "Container.1.Arg.1": "-c",
            "Container.1.Arg.2": boot,
            "Tag.1.Key": _TAG_USER,
            "Tag.1.Value": user_id,
            "Tag.2.Key": _TAG_BOOTCFG,
            "Tag.2.Value": boot_fp,
        }
        for i, (k, v) in enumerate(env.items(), start=1):
            p[f"Container.1.EnvironmentVar.{i}.Key"] = k
            p[f"Container.1.EnvironmentVar.{i}.Value"] = v
        p.update(self._volume_params(user_id))
        await self._call("CreateContainerGroup", p)

    async def start(self, user_id: str) -> None:
        """ECI 上没有这个动作 —— 实例一创建就在起。inspect 报 running=False 时
        它已经在 Pending/Scheduling, 该做的只是等。"""

    async def release(self, user_id: str) -> None:
        # 停不了, 只能删。用户的东西在 NAS 上, 下次访问重建。
        await self.destroy(user_id)

    async def destroy(self, user_id: str) -> None:
        g = await self._find(user_id)
        if g is None:
            return
        await self._call("DeleteContainerGroup",
                         {"ContainerGroupId": g["ContainerGroupId"]})

    async def running_users(self) -> list[str]:
        """计量与回收都遍历这个列表, 所以**漏掉一条 = 有人白用且永不回收**。

        不传 Limit (见 _find 里的注释), 靠 NextToken 翻页。最后拿 TotalCount
        对一次账: 少了却又没有续页令牌, 说明这个 API 以我们没预料到的方式截断了
        —— 那种情况下宁可吵一声, 也不要安静地少算钱。
        """
        users: list[str] = []
        seen = 0
        total = None
        token = None
        while True:
            p: dict = {"Status": "Running"}
            if token:
                p["NextToken"] = token
            body = await self._call("DescribeContainerGroups", p)
            groups = body.get("ContainerGroups", [])
            seen += len(groups)
            if total is None:
                total = body.get("TotalCount")
            for g in groups:
                uid = self._tags_of(g).get(_TAG_USER, "")
                if uid:
                    users.append(uid)
            token = body.get("NextToken")
            if not token:
                break
        if isinstance(total, int) and seen < total:
            log.error("[work] ECI 只回了 %d/%d 个运行中实例且没有续页令牌 —— "
                      "这批工作台不会被计量也不会被回收", seen, total)
        return users


def make_backend() -> Backend:
    if (config.WORK_BACKEND or "docker").lower() == "eci":
        return EciBackend()
    return DockerBackend()
