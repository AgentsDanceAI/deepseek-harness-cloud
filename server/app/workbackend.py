"""Where a user's workspace container actually runs.

Three backends, same contract:

  docker  本机 (或自部署) 的 docker 引擎, 经受限 socket 代理。容器可以 stop 后
          再 start, 命名卷保留状态。
  eci     阿里云弹性容器实例。**没有"停止但保留"这个状态** —— 只能创建和删除,
          于是"闲置回收"等于销毁, 而恢复是一次完整冷启动。
          正因如此, ECI 后端下 /root 与 /workspace 必须落在 NAS 上: 容器一删,
          容器内的任何东西都不再存在。
  k8s     一台常驻节点上的 Kubernetes (k3s)。语义与 ECI 一样是创建/删除, 但节点
          是热的: 镜像已在本地, 没有机房调度那 25 秒, Pod 起来就是应用自己的
          启动时间。用户文件落在一个 PVC 上, 布局与 NAS 一致。

抽象出这层不是为了好看, 是因为 deploy/selfhost/ 那条路只有 docker: 把 docker
换掉等于把自部署删掉。三边都实现同一个 Backend, 由 WORK_BACKEND 选; 还可以
按产品分派 (WORK_BACKEND_PRODUCTS, 见 RoutedBackend)。
"""

from __future__ import annotations

import abc
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import pathlib
import re
import ssl
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from . import config

log = logging.getLogger("dhc.work")


#: 组内共享的种子卷名 (EmptyDir)。初始化容器往这里铺资产, 常规容器从这里取 ——
#: 见 products.InitContainer。每次冷启动重铺, 所以换资产镜像就是换资产, 不会
#: 留下"NAS 上还是上一版"这种半新半旧的状态。
_SEED_VOLUME = "dshwork-seed"


def cname(user_id: str) -> str:
    """Workspace name. Doubles as a docker-DNS hostname for Caddy's dynamic
    upstream, and as the ECI ContainerGroupName — both want the same charset."""
    return "dshwork-" + re.sub(r"[^a-zA-Z0-9]", "", user_id)


@dataclass(frozen=True)
class WorkInfo:
    """What the app needs to know about one workspace, backend-independent."""

    running: bool
    boot_fp: str  # boot-script digest stamped at create time
    image_id: str  # whatever identifies the image THIS instance was born from
    host: str  # hostname or IP that answers on :3081
    state: str = ""  # backend's own word for it, for /api/work/status only


class Backend(abc.ABC):
    #: docker can stop-and-resume; ECI cannot (create/delete only)
    resumable: bool = True
    #: 启动等待页上那句"通常需要多久", 按后端说实话 (docker 是 stop/start)。
    boot_hint: str = "5–20 秒"

    @abc.abstractmethod
    async def inspect(self, user_id: str) -> WorkInfo | None: ...

    @abc.abstractmethod
    async def current_image_id(self, image: str) -> str:
        """Identity of the image a NEW workspace would be born from. Compared
        against WorkInfo.image_id to spot a workspace left on an old runtime.
        Empty string means "could not resolve" — callers treat that as
        not-stale, because destroying a working container over a failed lookup
        is far worse than running one build behind."""

    @abc.abstractmethod
    async def create(
        self,
        user_id: str,
        *,
        boot: str,
        env: dict[str, str],
        boot_fp: str,
        image: str,
        image_ref: str = "",
        mem_mb: int = 0,
        cpus: float = 0.0,
        sidecars: tuple = (),
        host_aliases: tuple = (),
        init_containers: tuple = (),
        seeds: tuple = (),
        run_as_user: int | None = None,
    ) -> None: ...

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

    def for_product(self, product_id: str) -> Backend:
        """这个产品的工作台实际由哪个后端管。单后端就是自己; RoutedBackend 按
        产品分派。给那些手里只有产品、没有工作台键的调用方用 (镜像陈旧判定)。"""
        return self

    def offline_workspace_dir(self, user_id: str) -> pathlib.Path | None:
        """应用机上能只读看到这个用户 /workspace 的路径, 没有则 None。

        工作区停止或删除后，文件列表仍需要从持久卷读取；ECI 实例不存在时
        尤其不能依赖容器文件系统。
        """
        return None


# --- docker: the original backend, unchanged in behaviour --------------------

LABEL = "dshwork.user"
# The boot script is baked into the container's Cmd at CREATE time, so an
# existing container keeps rewriting the settings.yaml it was born with.
# Stamping the script's digest lets ensure_workspace spot
# a stale container and rebuild it; /root and /workspace survive, so nothing the
# user made is lost.
CFG_LABEL = "dshwork.bootcfg"


def host_free_mb() -> int | None:
    """宿主可用内存 (MB)。读不到返回 None。

    容器里的 /proc/meminfo 反映宿主内存，因此无需额外挂载或探针。
    用 MemAvailable 而不是 MemFree: 后者把可回收的 page cache 算成"已用",
    可能导致容量检查长期误判为内存不足。"""
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

    async def _api(
        self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> httpx.Response:
        async with httpx.AsyncClient(base_url=config.DOCKER_PROXY_URL, timeout=30.0) as client:
            return await client.request(method, path, json=json_body, params=params)

    async def inspect(self, user_id: str) -> WorkInfo | None:
        r = await self._api("GET", f"/containers/{cname(user_id)}/json")
        if r.status_code != 200:
            return None
        d = r.json()
        labels = (d.get("Config") or {}).get("Labels") or {}
        return WorkInfo(
            running=(d.get("State") or {}).get("Status", "") == "running",
            # no stamp at all predates the mechanism -> stale by definition
            boot_fp=labels.get(CFG_LABEL, ""),
            image_id=d.get("Image", ""),
            host=cname(user_id),
            state=(d.get("State") or {}).get("Status", ""),
        )

    async def current_image_id(self, image: str) -> str:
        """Resolved image *ID*, not the tag string: the usual upgrade here is
        `docker build -t image:tag` — sometimes the same tag rebuilt — and a
        tag comparison would call that unchanged, leaving every existing
        workspace on the old runtime forever."""
        r = await self._api("GET", f"/images/{image}/json")
        if r.status_code != 200:
            log.warning("[work] 解析不了镜像 %s (%s), 跳过镜像陈旧判定", image, r.status_code)
            return ""
        return (r.json() or {}).get("Id", "")

    async def create(
        self,
        user_id: str,
        *,
        boot: str,
        env: dict[str, str],
        boot_fp: str,
        image: str,
        image_ref: str = "",
        mem_mb: int = 0,
        cpus: float = 0.0,
        sidecars: tuple = (),
        host_aliases: tuple = (),
        init_containers: tuple = (),
        seeds: tuple = (),
        run_as_user: int | None = None,
    ) -> None:
        if sidecars or init_containers:
            # docker 后端一个容器就是一个容器, 拼不出共享网络命名空间的容器组,
            # 也没有初始化容器与组内共享卷。明确拒绝 —— 静默忽略的结果是应用起了
            # 但连不上数据库/读不到配置, 表现为一堆 500, 谁也想不到是后端不支持。
            raise RuntimeError("多容器栈产品只有 ECI 后端能跑 (WORK_BACKEND=eci)")
        hexid = cname(user_id)[len("dshwork-") :]
        body = {
            "Image": image,
            "Cmd": ["sh", "-c", boot],
            "WorkingDir": "/workspace",
            "Labels": {LABEL: user_id, CFG_LABEL: boot_fp},
            "Env": [f"{k}={v}" for k, v in env.items()],
            "HostConfig": {
                "Memory": (mem_mb or config.WORK_MEM_LIMIT_MB) * 1024 * 1024,
                "NanoCpus": int((cpus or config.WORK_CPUS) * 1e9),
                "PidsLimit": 512,
                # Prefer reclaiming a restartable workspace over unrelated host
                # services when the kernel must select an OOM victim.
                "OomScoreAdj": config.WORK_OOM_SCORE_ADJ,
                "NetworkMode": config.WORK_NETWORK,
                "RestartPolicy": {"Name": "no"},
                "Mounts": [
                    {"Type": "volume", "Source": f"dshwork-home-{hexid}", "Target": "/root"},
                    {"Type": "volume", "Source": f"dshwork-ws-{hexid}", "Target": "/workspace"},
                ],
            },
        }
        r = await self._api("POST", "/containers/create", json_body=body, params={"name": cname(user_id)})
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
        r = await self._api("GET", "/containers/json", params={"filters": f'{{"label":["{LABEL}"]}}'})
        if r.status_code != 200:
            return []
        return [uid for c in r.json() if (uid := (c.get("Labels") or {}).get(LABEL, ""))]

    def capacity_reason(self) -> str:
        """起新工作台前的容量判定, 返回空串表示可以起。

        静态并发上限之外还要检查宿主内存余量，避免工作区影响同机服务。

        内存读不到时**放行**: 与本模块其余闸门同一姿态 (检查自身故障不该拦人),
        而且一个读不到 /proc 的进程更可能是环境异常而不是真的没内存。"""
        free = host_free_mb()
        if free is None:
            return ""
        need = config.WORK_MEM_LIMIT_MB + config.WORK_MIN_FREE_MB
        if free < need:
            log.warning(
                "[work] 宿主可用内存 %dMB < 需要 %dMB (容器 %d + 保留 %d), 拒起新工作台",
                free,
                need,
                config.WORK_MEM_LIMIT_MB,
                config.WORK_MIN_FREE_MB,
            )
            return f"memory:{free}<{need}"
        return ""

    def offline_workspace_dir(self, user_id: str) -> pathlib.Path | None:
        root = (config.WORK_VOLUME_ROOT or "").strip()
        if not root:
            return None
        hexid = cname(user_id)[len("dshwork-") :]
        d = pathlib.Path(root) / f"dshwork-ws-{hexid}" / "_data"
        return d if d.is_dir() else None


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
    return base64.b64encode(hmac.new((secret + "&").encode(), sts.encode(), hashlib.sha1).digest()).decode()


class EciError(RuntimeError):
    pass


class EciBackend(Backend):
    """ECI 没有 stop/start —— release 就是 delete。

    因此 /root 与 /workspace **必须**落在 NAS 上, 否则闲置回收等于抹掉用户的
    全部文件和会话。WORK_NAS_SERVER 为空时仍可运行 (用于冒烟验证), 但每次创建
    都会告警: 那种形态下工作台是一次性的。
    """

    resumable = False
    boot_hint = "20–40 秒"  # 每次都是新实例: 调度 + 建 EIP + 挂缓存 ≈ 25 秒起

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
            raise EciError(f"{action}: HTTP {r.status_code}, 响应不是 JSON: {r.text[:200]}") from None
        # 阿里云把错误也放在 200 之外的码里, 但 Code 字段才是权威的 —— 只看
        # status_code 会把 "参数错误" 读成成功。
        if r.status_code != 200 or body.get("Code"):
            raise EciError(
                f"{action}: {body.get('Code') or r.status_code} {body.get('Message', r.text[:200])}"
            )
        return body

    # -- helpers ------------------------------------------------------------
    def _volume_params(self, user_id: str) -> dict:
        """NAS 卷。空配置时返回 {} —— 容器仍能起, 但一删就什么都不剩。"""
        server = (config.WORK_NAS_SERVER or "").strip()
        if not server:
            if not self._warned_no_nas:
                log.warning(
                    "[work] ECI 后端未配置 WORK_NAS_SERVER: 工作台是一次性的, 闲置回收会抹掉用户的文件与会话"
                )
                self._warned_no_nas = True
            return {}
        hexid = cname(user_id)[len("dshwork-") :]
        # 这个目录**必须已经存在于 NAS 上** —— ECI 不会替你建, 挂载会以
        # "file does not exist" 失败, 而实例只是一直 Pending, 不报错。
        # SubPath 嵌套目录由挂载方按需创建。
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
        # 一个共享卷配合用户子路径，也避免触发每实例卷数量限制。
        for i, (sub, path) in enumerate(
            ((f"{hexid}/home", "/root"), (f"{hexid}/workspace", "/workspace")), start=1
        ):
            p[f"Container.1.VolumeMount.{i}.Name"] = "dshwork-nas"
            p[f"Container.1.VolumeMount.{i}.MountPath"] = path
            p[f"Container.1.VolumeMount.{i}.SubPath"] = sub
        return p

    @staticmethod
    def _tags_of(group: dict) -> dict[str, str]:
        return {t.get("Key", ""): t.get("Value", "") for t in (group.get("Tags") or [])}

    @staticmethod
    def _alive(group: dict) -> bool:
        status = group.get("Status", "")
        return status in _ECI_RUNNING or status in _ECI_COMING_UP

    async def _find_all(self, user_id: str) -> list[dict]:
        """同名的**所有**容器组, 新的在前。

        ECI 不保证 ContainerGroupName 唯一。这里原先写的是"一个名字最多对应一个
        活着的组", 是错的: 2026-08-24 05:24:15 和 :16 各建出一台同名且都 Running
        的实例 (forward_auth 在冷启动时并发扇出, 每个请求都看到"没有实例")。
        错的后果全是钱:
          - running_users() 把同一个人算两遍, 每分钟扣两分钟机时;
          - destroy() 只删第一个, 另一个继续按秒计费, 而用户只看得到一台;
          - 两台各自自动创建一个 EIP, 账号级配额也按两个算。
        排序让"留哪一台"是确定的 —— 并发的自愈必须收敛到同一个选择, 否则两边
        各删一台就把两台都删了。CreationTime 相同时用 Id 兜底。

        不传 Limit: 这个参数组合里带上它, ECI 会反过来要求 ContainerGroupId。
        """
        body = await self._call("DescribeContainerGroups", {"ContainerGroupName": cname(user_id)})
        groups = [g for g in body.get("ContainerGroups", []) if g.get("ContainerGroupName") == cname(user_id)]
        groups.sort(
            key=lambda g: (g.get("CreationTime") or "", g.get("ContainerGroupId") or ""), reverse=True
        )
        return groups

    async def _find(self, user_id: str) -> dict | None:
        groups = await self._find_all(user_id)
        return groups[0] if groups else None

    async def _delete(self, group: dict) -> None:
        try:
            await self._call("DeleteContainerGroup", {"ContainerGroupId": group["ContainerGroupId"]})
            # 与"建实例"配对: 一台实例的生死就是一个 EIP 的申请与释放。
            log.info(
                "[work] 删实例 %s (%s), 随它释放一个 EIP",
                group.get("ContainerGroupId"),
                group.get("Status"),
            )
        except Exception as e:  # noqa: BLE001
            # 并发自愈时另一边可能已经删掉了 —— 那正是想要的结果, 不是故障。
            # 但真删不掉的实例会一直计费, 所以还是要留下痕迹。
            log.warning("[work] 删实例 %s 失败: %s", group.get("ContainerGroupId"), e)

    # -- Backend ------------------------------------------------------------
    async def inspect(self, user_id: str) -> WorkInfo | None:
        groups = await self._find_all(user_id)
        alive = [g for g in groups if self._alive(g)]
        keep = alive[0] if alive else None
        if len(alive) > 1:
            log.error(
                "[work] %s 有 %d 台同名实例在跑 —— 正在重复计费。留 %s, 删 %s",
                user_id,
                len(alive),
                keep.get("ContainerGroupId"),
                [g.get("ContainerGroupId") for g in alive[1:]],
            )
        # 留下的那台之外一概删掉: 多余的活实例在按秒烧钱, 终态 (Succeeded/
        # Failed/...) 的实例白占着名字会让下一次 CreateContainerGroup 撞名。
        for g in groups:
            if g is keep:
                continue
            if not self._alive(g):
                log.info("[work] %s 处于终态 %s, 清掉以便重建", user_id, g.get("Status"))
            await self._delete(g)
        if keep is None:
            return None
        status = keep.get("Status", "")
        if status in _ECI_COMING_UP:
            self._report_stuck_mount(user_id, keep)
        else:
            self._mount_reported.discard(user_id)
        containers = keep.get("Containers") or [{}]
        return WorkInfo(
            running=status in _ECI_RUNNING,
            boot_fp=self._tags_of(keep).get(_TAG_BOOTCFG, ""),
            image_id=containers[0].get("Image", ""),
            host=keep.get("IntranetIp", ""),
            state=status,
        )

    def _report_stuck_mount(self, user_id: str, group: dict) -> None:
        """挂载失败不会让实例退出, 它会一直 Pending。

        对上层来说这和“正在启动”相同，因此必须从事件中报告挂载告警。
        SubPath 的嵌套目录可以自动创建，但 WORK_NAS_PATH 的基础路径必须存在。
        """
        if user_id in self._mount_reported:
            return
        for e in group.get("Events") or []:
            msg = e.get("Message", "")
            if e.get("Type") == "Warning" and "MountVolume" in msg:
                self._mount_reported.add(user_id)
                log.error(
                    "[work] %s 卡在 %s: 挂载失败 —— %s。"
                    "检查 WORK_NAS_PATH=%r 在 NAS 上是否存在, "
                    "以及挂载点权限组是否放行本交换机网段",
                    user_id,
                    group.get("Status"),
                    msg[:200],
                    config.WORK_NAS_PATH,
                )
                return

    async def current_image_id(self, image: str) -> str:
        """ECI 上镜像身份就是仓库引用本身。

        不像本机 docker 能拿到解析后的镜像 ID —— 同名重推（tag 不变但
        tag 没变) 这里察觉不到。可接受: 工作台镜像走版本号 tag, 而重建镜像缓存
        本来就是发版流程的一步。"""
        return image

    async def create(
        self,
        user_id: str,
        *,
        boot: str,
        env: dict[str, str],
        boot_fp: str,
        image: str,
        image_ref: str = "",
        mem_mb: int = 0,
        cpus: float = 0.0,
        sidecars: tuple = (),
        host_aliases: tuple = (),
        init_containers: tuple = (),
        seeds: tuple = (),
        run_as_user: int | None = None,
    ) -> None:
        p = {
            "ContainerGroupName": cname(user_id),
            "ZoneId": config.ECI_ZONE_ID or None,
            "VSwitchId": config.ECI_VSWITCH_ID,
            "SecurityGroupId": config.ECI_SECURITY_GROUP_ID,
            "Cpu": cpus or config.WORK_CPUS,
            "Memory": round((mem_mb or config.WORK_MEM_LIMIT_MB) / 1024, 2),
            "ComputeCategory.1": config.ECI_COMPUTE_CATEGORY or None,
            # 栈产品 (带伴随容器) 用 Always: ECI 没有 depends_on, 应用容器在
            # 中间件就绪前会崩溃退出, 靠重启拉起来。单容器保持 Never —— 它退出
            # 就是真的死了, Always 会把一个坏容器变成永动的计费器。
            "RestartPolicy": "Always" if sidecars else "Never",
            # Rebuild the cache for each immutable image release. A cache miss
            # falls back to a full image pull and must be monitored separately.
            "AutoMatchImageCache": "true",
            "AutoCreateEip": "true",
            "EipBandwidth": config.ECI_EIP_BANDWIDTH,
            "Container.1.Name": "app",
            "Container.1.Image": image_ref or image,
            **(
                {"Container.1.SecurityContext.RunAsUser": str(run_as_user)} if run_as_user is not None else {}
            ),
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
        nas = self._volume_params(user_id)
        p.update(nas)
        # 种子卷: 初始化容器把资产镜像的内容铺进来, 常规容器再从这里取 ——
        # compose 的 bind mount 在 ECI 上的等价物 (见 products.InitContainer)。
        # 初始化容器**跑完**常规容器才起, 所以不需要任何等待循环。
        hexid = cname(user_id)[len("dshwork-") :]
        if init_containers:
            vi = 2 if nas else 1
            p[f"Volume.{vi}.Name"] = _SEED_VOLUME
            p[f"Volume.{vi}.Type"] = "EmptyDirVolume"
            for k, ic in enumerate(init_containers, start=1):
                p[f"InitContainer.{k}.Name"] = ic.name
                p[f"InitContainer.{k}.Image"] = ic.image_ref
                for j, arg in enumerate(ic.cmd, start=1):
                    p[f"InitContainer.{k}.Command.{j}"] = arg
                p[f"InitContainer.{k}.VolumeMount.1.Name"] = _SEED_VOLUME
                p[f"InitContainer.{k}.VolumeMount.1.MountPath"] = ic.seed_mount
                for j, (subdir, path) in enumerate(ic.mounts, start=2):
                    p[f"InitContainer.{k}.VolumeMount.{j}.Name"] = "dshwork-nas"
                    p[f"InitContainer.{k}.VolumeMount.{j}.MountPath"] = path
                    p[f"InitContainer.{k}.VolumeMount.{j}.SubPath"] = f"{hexid}/{subdir}"
        # 主容器的种子挂载接在 NAS 那两个之后 —— 编号是连续的, 空一格整个
        # 请求就被阿里云按"到此为止"截断, 后面的挂载静默消失。
        for j, (sub, path) in enumerate(seeds, start=(2 if nas else 0) + 1):
            p[f"Container.1.VolumeMount.{j}.Name"] = _SEED_VOLUME
            p[f"Container.1.VolumeMount.{j}.MountPath"] = path
            if sub:
                p[f"Container.1.VolumeMount.{j}.SubPath"] = sub
        # compose 服务名 -> 回环。写进组的 /etc/hosts, 于是镜像里写死的
        # "http://api:5001" 这类地址不用改就能通。
        if host_aliases:
            p["HostAliase.1.Ip"] = "127.0.0.1"
            for j, name in enumerate(host_aliases, start=1):
                p[f"HostAliase.1.Hostname.{j}"] = name
        # 伴随容器: 用上游原生镜像, 与主容器共享网络命名空间 (互相 127.0.0.1)。
        # 不给容器级 cpu/mem —— 组给总量, 容器之间自己挤, 与 compose 默认一致。
        for ci, sc in enumerate(sidecars, start=2):
            p[f"Container.{ci}.Name"] = sc.name
            p[f"Container.{ci}.Image"] = sc.image_ref
            if sc.run_as_user is not None:
                p[f"Container.{ci}.SecurityContext.RunAsUser"] = str(sc.run_as_user)
            for j, arg in enumerate(sc.cmd, start=1):
                p[f"Container.{ci}.Command.{j}"] = arg
            for j, arg in enumerate(sc.args, start=1):
                p[f"Container.{ci}.Arg.{j}"] = arg
            for j, (k, v) in enumerate(sc.env, start=1):
                p[f"Container.{ci}.EnvironmentVar.{j}.Key"] = k
                p[f"Container.{ci}.EnvironmentVar.{j}.Value"] = v
            for j, (subdir, path) in enumerate(sc.mounts, start=1):
                p[f"Container.{ci}.VolumeMount.{j}.Name"] = "dshwork-nas"
                p[f"Container.{ci}.VolumeMount.{j}.MountPath"] = path
                p[f"Container.{ci}.VolumeMount.{j}.SubPath"] = f"{hexid}/{subdir}"
            for j, (sub, path) in enumerate(sc.seeds, start=len(sc.mounts) + 1):
                p[f"Container.{ci}.VolumeMount.{j}.Name"] = _SEED_VOLUME
                p[f"Container.{ci}.VolumeMount.{j}.MountPath"] = path
                if sub:
                    p[f"Container.{ci}.VolumeMount.{j}.SubPath"] = sub
        # 私有仓库的镜像要带凭据才拉得动 (见 config.registry_credential)。空配置
        # 时这里什么都不加, 行为与从前一致。
        p |= config.registry_credential()
        body = await self._call("CreateContainerGroup", p)
        # 每台实例都自动创建一个 EIP, 所以这一行就是"谁在什么时候占了一个 EIP"的
        # 唯一自有记录。没有它, 想回答"最近这些 EIP 都是谁申请的"只能去翻操作审计
        # 或账单 —— 而那两个接口都要额外的 RAM 权限, 而且只会显示同一个子账号在调,
        # 落不到具体用户身上。
        log.info(
            "[work] 建实例 %s user=%s cpu=%s mem=%sGiB (自动创建 EIP)",
            body.get("ContainerGroupId", "?"),
            user_id,
            p["Cpu"],
            p["Memory"],
        )

    async def start(self, user_id: str) -> None:
        """ECI 上没有这个动作 —— 实例一创建就在起。inspect 报 running=False 时
        它已经在 Pending/Scheduling, 该做的只是等。"""

    async def release(self, user_id: str) -> None:
        # 停不了, 只能删。用户的东西在 NAS 上, 下次访问重建。
        await self.destroy(user_id)

    async def destroy(self, user_id: str) -> None:
        # 删**全部**同名实例, 不是第一个: 名字不唯一 (见 _find_all), 少删一台
        # 就留下一台按秒计费、没人看得见、也不会再被回收的实例。
        for g in await self._find_all(user_id):
            await self._delete(g)

    def offline_workspace_dir(self, user_id: str) -> pathlib.Path | None:
        """NAS 上该用户的 workspace 子目录, 前提是应用机把同一个 NAS 挂了起来。

        布局与 _volume_params 的 SubPath 必须一致 —— 两处写的是同一个位置,
        走岔了不会报错, 只会让「個人成品」永远是空的。
        """
        root = (config.WORK_NAS_LOCAL_MOUNT or "").strip()
        if not root:
            return None
        hexid = cname(user_id)[len("dshwork-") :]
        d = pathlib.Path(root) / hexid / "workspace"
        return d if d.is_dir() else None

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
            log.error(
                "[work] ECI 只回了 %d/%d 个运行中实例且没有续页令牌 —— 这批工作台不会被计量也不会被回收",
                seen,
                total,
            )
        # 按人去重。同名重复实例 (见 _find_all) 会让调用方把同一个人遍历两遍 ——
        # 计量那边一分钟就扣两分钟机时。回收靠 inspect 收敛, 这里只保证不多扣钱。
        uniq = list(dict.fromkeys(users))
        if len(uniq) != len(users):
            log.error(
                "[work] 运行中实例比人多 %d 台 (有人被建重了), 已按人去重以免重复扣机时",
                len(users) - len(uniq),
            )
        return uniq


# --- k8s: 常驻节点上的 Kubernetes (k3s) -------------------------------------

#: Pod 上的标签只用来"找出我们的 Pod"。**身份放注解**: label 值的字符集
#: ([A-Za-z0-9-_.]) 装不下工作台键里的 "~" (products.wskey)。
K8S_LABEL = "dshwork"
K8S_ANN_USER = "dshwork/user"
K8S_ANN_BOOTCFG = "dshwork/bootcfg"
_K8S_DATA_VOLUME = "dshwork-data"
#: 活着 = 还会变好。终态 (Succeeded / Failed / Unknown) 与 ECI 同一处理: 当作
#: 不存在, 清掉以便重建 —— 对一个不可 start 的后端, "坏了就重建"是唯一的自愈。
_K8S_ALIVE = {"Pending", "Running"}
#: 这些等待原因不会自己好: 拉不到镜像、引用了不存在的 Secret (pull secret 名字
#: 写错)。Pod 会一直 Pending, 对上层来说与"正在启动"一模一样, 所以要报出来。
_K8S_STUCK_REASONS = {
    "ErrImagePull",
    "ImagePullBackOff",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
}


#: k8s 的 hostAliases 只收 RFC 1123 主机名 (小写字母数字、-、.)。compose 栈里常见的
#: api_websocket / db_postgres 这种带下划线的服务名, ECI 照单全收, k8s 直接拒掉
#: 整个 Pod (422) —— 2026-09-03 Dify 全切时就栽在这三个名字上。
_RFC1123 = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")


#: 同步容器统一的 rclone 参数。--metadata 把 mode/uid/gid/mtime 存进对象元数据
#: (uid 1000 的产品、可执行文件都靠它); --links 把符号链接存成 .rclonelink 再还原;
#: 目录标记 + 建空目录: S3 语义里没有目录, 而 Postgres 数据目录里有一堆必须存在的
#: 空目录, 少一个就起不来。/.dsh-* 是我们自己的标记文件, 不进 OSS。
_K8S_RCLONE_FLAGS = (
    "--metadata --links --fast-list --transfers 16 --checkers 32 "
    "--s3-directory-markers --create-empty-src-dirs --exclude '/.dsh-*' "
    "--stats-one-line --stats 60s"
)

#: 初始化容器: 起动前把用户目录从 OSS 拉回本地卷。OSS 是正本 —— 本地有而 OSS 没有
#: 的东西会被删掉 (上次没推上去的改动就是这么丢的, 见 README "还没做的")。
#: 拉失败**不落标记**, 同步器见不到标记就不推: 半份数据推上去会把正本也弄坏。
#: OSS 上没有这个用户 (新用户, 或迁移前) 时保留本地、落标记 —— 本地就是正本。
_K8S_RESTORE_SH = r"""set -u
R="$DSH_SYNC_REMOTE/$DSH_HEXID"
mkdir -p /data/home /data/workspace
rm -f /data/.dsh-restored
if rclone lsf "$R" --max-depth 1 2>/dev/null | grep -q .; then
  echo "restore: $R -> /data"
  if rclone sync "$R" /data %(flags)s; then
    : > /data/.dsh-restored
    echo "restore: done"
  else
    echo "restore: FAILED, syncer will not push until a clean restore" >&2
  fi
else
  echo "restore: nothing under $R, local is authoritative"
  : > /data/.dsh-restored
fi
exit 0
"""

#: 原生 sidecar: 运行中每隔 DSH_SYNC_INTERVAL 秒推 home/workspace; 收到 TERM (应用
#: 容器已经退出) 后全量推一次再退出 —— 数据库目录只在这一刻推, 推的是停机后的
#: 一致状态。周期推送在前台跑, TERM 来了 trap 会等它跑完再执行 (sh 在命令之间处理
#: 信号), 所以两次推送不会重叠。
_K8S_SYNCER_SH = r"""set -u
R="$DSH_SYNC_REMOTE/$DSH_HEXID"
final() {
  if [ -e /data/.dsh-restored ]; then
    echo "final push: /data -> $R"
    rclone sync /data "$R" %(flags)s && echo "final push: done" || echo "final push: FAILED" >&2
  else
    echo "final push: skipped (no clean restore)" >&2
  fi
  exit 0
}
trap final TERM INT
echo "syncer: up, interval ${DSH_SYNC_INTERVAL}s"
while :; do
  sleep "$DSH_SYNC_INTERVAL" & wait $!
  [ -e /data/.dsh-restored ] || continue
  for d in home workspace; do
    [ -d "/data/$d" ] || continue
    rclone sync "/data/$d" "$R/$d" %(flags)s || echo "push $d: FAILED" >&2
  done
done
"""


def pod_name(user_id: str) -> str:
    """Pod 名: cname 的小写形式。k8s 的对象名是 DNS-1123 (只许小写), 而 cname 为
    docker DNS 与 ECI 保留了大小写。用户 id 本身全小写 (u_ + 十六进制), 不会撞。"""
    return cname(user_id).lower()


def _millicores(v: float) -> str:
    return f"{max(1, int(round(v * 1000)))}m"


class K8sError(RuntimeError):
    pass


class K8sBackend(Backend):
    """一台常驻节点上的 Kubernetes (k3s), dhc-server 直接调 API server。

    与 ECI 同一套"创建/删除"语义 —— release 就是删 Pod; 用户的 /root 与
    /workspace 落在 K8S_DATA_PVC 那个卷的 <hexid>/home、<hexid>/workspace 子路径
    上 (布局与 NAS 一致, 子路径目录由 kubelet 按需创建)。不同的是节点是热的:
    镜像已在本地, 没有机房调度那 25 秒, Pod 起来就是应用自己的启动时间。

    产品的 cpu/mem 给在 **Pod 级** (k8s ≥ 1.34 的 pod-level resources): 组给总量,
    容器之间自己挤 —— 与 ECI 容器组、compose 默认是同一个语义, 栈产品不用给
    十个容器各算一份。CPU 只按上限的 1/4 预留 (request): 工作台大多数时间在等
    模型回话, 按上限预留会把节点"算满"而实际空转; 内存按上限预留, 因为节点是
    共享机, 超卖内存等于拿别人的服务赌。
    """

    resumable = False
    boot_hint = "5–15 秒"  # 镜像在节点上, Pod 一秒起, 剩下是应用自己的启动

    def __init__(self) -> None:
        self._ns = config.K8S_NAMESPACE or "dsh"
        self._pods = f"/api/v1/namespaces/{self._ns}/pods"
        self._tok: str | None = None
        self._ssl: ssl.SSLContext | bool | None = None
        self._warned_no_pvc = False
        self._pull_secret_ready = False
        self._sync_secret_ready = False
        self._stuck_reported: set[str] = set()

    # -- transport ----------------------------------------------------------
    def _token(self) -> str:
        if self._tok is None:
            f = (config.K8S_TOKEN_FILE or "").strip()
            self._tok = pathlib.Path(f).read_text(encoding="ascii").strip() if f else config.K8S_TOKEN
        return self._tok

    def _verify(self) -> ssl.SSLContext | bool:
        if self._ssl is None:
            ca = (config.K8S_CA_FILE or "").strip()
            self._ssl = ssl.create_default_context(cafile=ca) if ca else True
        return self._ssl

    async def _api(
        self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=config.K8S_API_URL,
            verify=self._verify(),
            timeout=30.0,
            headers={"Authorization": f"Bearer {self._token()}"},
        ) as client:
            return await client.request(method, path, json=json_body, params=params)

    async def _get(self, user_id: str) -> dict | None:
        r = await self._api("GET", f"{self._pods}/{pod_name(user_id)}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise K8sError(f"get pod {pod_name(user_id)}: {r.status_code} {r.text[:200]}")
        return r.json()

    async def _delete(self, user_id: str) -> None:
        # 5 秒宽限: 工作台没有需要优雅收尾的东西 (状态在卷上), 而名字要等 Pod
        # 真正消失才空出来 —— 拖着的删除就是拖着的下一次创建。
        r = await self._api("DELETE", f"{self._pods}/{pod_name(user_id)}", params={"gracePeriodSeconds": 5})
        if r.status_code == 404:
            return  # 并发自愈时另一边可能已经删掉了 —— 那正是想要的结果
        if r.status_code not in (200, 202):
            log.warning("[work] 删 Pod %s 失败: %s %s", pod_name(user_id), r.status_code, r.text[:200])
            return
        log.info("[work] 删 Pod %s", pod_name(user_id))

    # -- helpers ------------------------------------------------------------
    async def _ensure_pull_secret(self) -> None:
        """私有仓库的拉取凭据由 dhc-server **自己**写进命名空间。

        与 ECI 那边 config.registry_credential 同源 (WORK_REGISTRY_*): 密码已经在
        dhc-server 手里, 不用人再到节点上敲一遍。每个进程只写一次; 没配凭据就
        什么都不写 —— 那种形态下 Secret 要么是别人建好的, 要么镜像是公开的。
        写失败**不拦**创建: Pod 会卡在 ImagePullBackOff, inspect 会把那个报出来。
        """
        name = (config.K8S_IMAGE_PULL_SECRET or "").strip()
        if self._pull_secret_ready or not name:
            return
        server, user, password = (
            config.WORK_REGISTRY_SERVER,
            config.WORK_REGISTRY_USERNAME,
            config.WORK_REGISTRY_PASSWORD,
        )
        if not (server and user and password):
            self._pull_secret_ready = True
            return
        auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        dockercfg = {"auths": {server: {"username": user, "password": password, "auth": auth}}}
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": self._ns},
            "type": "kubernetes.io/dockerconfigjson",
            "data": {".dockerconfigjson": base64.b64encode(json.dumps(dockercfg).encode()).decode()},
        }
        secrets = f"/api/v1/namespaces/{self._ns}/secrets"
        r = await self._api("POST", secrets, json_body=body)
        if r.status_code == 409:
            r = await self._api("PUT", f"{secrets}/{name}", json_body=body)
        if r.status_code in (200, 201):
            self._pull_secret_ready = True
            log.info("[work] 拉取凭据 Secret %s/%s 已就位 (%s)", self._ns, name, server)
        else:
            log.warning("[work] 写拉取凭据 Secret %s 失败: %s %s", name, r.status_code, r.text[:200])

    @staticmethod
    def _sync_enabled() -> bool:
        return bool(
            (config.K8S_SYNC_OSS_BUCKET or "").strip()
            and config.K8S_SYNC_OSS_ACCESS_KEY_ID
            and config.K8S_SYNC_OSS_ACCESS_KEY_SECRET
        )

    async def _ensure_sync_secret(self) -> None:
        """OSS 凭据写成命名空间里的 Secret, 同步容器用 envFrom 取; 每个进程写一次。
        密钥不进 Pod 清单 (清单里只有 Secret 名), 也不进应用容器的 env。"""
        name = (config.K8S_SYNC_SECRET or "").strip()
        if self._sync_secret_ready or not name or not self._sync_enabled():
            return
        remote = (
            f"oss:{config.K8S_SYNC_OSS_BUCKET.strip()}/{(config.K8S_SYNC_OSS_PREFIX or 'dshwork').strip('/')}"
        )
        kv = {
            "RCLONE_CONFIG_OSS_TYPE": "s3",
            "RCLONE_CONFIG_OSS_PROVIDER": "Alibaba",
            "RCLONE_CONFIG_OSS_ENDPOINT": config.K8S_SYNC_OSS_ENDPOINT,
            "RCLONE_CONFIG_OSS_ACCESS_KEY_ID": config.K8S_SYNC_OSS_ACCESS_KEY_ID,
            "RCLONE_CONFIG_OSS_SECRET_ACCESS_KEY": config.K8S_SYNC_OSS_ACCESS_KEY_SECRET,
            # 密钥只对桶内对象有权限, 查桶 (HeadBucket) 会被拒 —— rclone 于是以为桶不存在,
            # 去 CreateBucket, 撞 409 BucketAlreadyExists, 一个文件也传不上去 (2026-09-03
            # 首次联调)。跳过查桶/建桶。
            "RCLONE_CONFIG_OSS_NO_CHECK_BUCKET": "true",
            "DSH_SYNC_REMOTE": remote,
        }
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": self._ns},
            "type": "Opaque",
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in kv.items()},
        }
        secrets = f"/api/v1/namespaces/{self._ns}/secrets"
        r = await self._api("POST", secrets, json_body=body)
        if r.status_code == 409:
            r = await self._api("PUT", f"{secrets}/{name}", json_body=body)
        if r.status_code in (200, 201):
            self._sync_secret_ready = True
            log.info("[work] OSS 同步凭据 Secret %s/%s 已就位 (%s)", self._ns, name, remote)
        else:
            log.warning("[work] 写 OSS 同步凭据 Secret %s 失败: %s %s", name, r.status_code, r.text[:200])

    def _sync_containers(self, hexid: str) -> tuple[dict, dict]:
        """(恢复初始化容器, 同步器原生 sidecar)。两个都挂用户整棵目录 (<hexid>/) 到 /data。"""
        common = {
            "image": config.K8S_SYNC_IMAGE,
            "envFrom": [{"secretRef": {"name": (config.K8S_SYNC_SECRET or "").strip()}}],
            "env": [
                {"name": "DSH_HEXID", "value": hexid},
                {"name": "DSH_SYNC_INTERVAL", "value": str(config.K8S_SYNC_INTERVAL_S)},
            ],
            "volumeMounts": [{"name": _K8S_DATA_VOLUME, "mountPath": "/data", "subPath": hexid}],
        }
        flags = _K8S_RCLONE_FLAGS
        restore = {
            "name": "dsh-restore",
            "command": ["sh", "-c", _K8S_RESTORE_SH % {"flags": flags}],
            **common,
        }
        syncer = {
            "name": "dsh-syncer",
            # 原生 sidecar: 在其它初始化容器之前起、在应用容器退出之后才停。
            "restartPolicy": "Always",
            "command": ["sh", "-c", _K8S_SYNCER_SH % {"flags": flags}],
            **common,
        }
        return restore, syncer

    def _data_volume(self) -> dict:
        """用户目录挂在什么上面 —— 跟着"正本在哪"走。

        开了 OSS 同步: 本地只是工作副本, 用 **emptyDir 并带容量上限**。三个好处:
        Pod 一删空间就释放 (否则每个"用户 × 产品"的本地副本永远留着, 实测 22 个目录
        717MB 而当时零个工作台在跑); 超限 kubelet 驱逐, 一个人写不满整块盘 (那块盘上
        还有别人的服务); PVC 没有任何配额机制, 这是唯一能设上限的形态。

        没开同步: 数据只有本地这一份, 必须是 PVC —— 那时不能设上限, 因为超限即销毁。
        """
        if self._sync_enabled():
            return {"name": _K8S_DATA_VOLUME, "emptyDir": {"sizeLimit": f"{config.K8S_WORK_DISK_GB}Gi"}}
        pvc = (config.K8S_DATA_PVC or "").strip()
        if not pvc:
            if not self._warned_no_pvc:
                log.warning(
                    "[work] k8s 后端未配置 K8S_DATA_PVC 也没开 OSS 同步: 工作台是一次性的, "
                    "闲置回收会抹掉用户的文件与会话"
                )
                self._warned_no_pvc = True
            return {"name": _K8S_DATA_VOLUME, "emptyDir": {}}
        return {"name": _K8S_DATA_VOLUME, "persistentVolumeClaim": {"claimName": pvc}}

    @staticmethod
    def _gvisor_for(product_id: str) -> bool:
        """K8S_GVISOR_PRODUCTS: 逗号分隔的产品 id; `*` = 全部; `-id` = 从全部里排除。
        `*` 是默认该有的形态 —— 新接的产品不该因为忘了加名单就掉回共享内核。"""
        items = [p.strip() for p in (config.K8S_GVISOR_PRODUCTS or "").split(",") if p.strip()]
        if "*" in items:
            return f"-{product_id}" not in items
        return product_id in items

    @staticmethod
    def _drop_caps() -> list[str]:
        return [c.strip().upper() for c in (config.K8S_DROP_CAPS or "").split(",") if c.strip()]

    #: 伴随容器 (上游中间件镜像) 保留的 capability。bitnami 那一系 (redis/etcd/elasticsearch)
    #: 的入口脚本以 root 起、再用 `chroot --userspec=1001:1001 /` 降权 —— 去掉 SYS_CHROOT
    #: 它们一个都起不来 (`chroot: cannot change root directory to '/': Operation not
    #: permitted`), 2026-09-04 全员回归 Coze 三个中间件各重启 10 次就是这个。
    #: 用户的代码不在伴随容器里跑, 给它们留这一个不亏什么。
    _SIDECAR_KEEP_CAPS = frozenset({"SYS_CHROOT"})

    def _harden(self, container: dict, *, sidecar: bool = False) -> dict:
        """每个容器都去掉用不着的 capability。与 runAsUser 合并进同一个 securityContext。"""
        drop = [c for c in self._drop_caps() if not (sidecar and c in self._SIDECAR_KEEP_CAPS)]
        if drop:
            sc = container.setdefault("securityContext", {})
            sc["capabilities"] = {"drop": drop}
        return container

    def _manifest(
        self,
        user_id: str,
        *,
        boot: str,
        env: dict[str, str],
        boot_fp: str,
        image: str,
        image_ref: str,
        mem_mb: int,
        cpus: float,
        sidecars: tuple,
        host_aliases: tuple,
        init_containers: tuple,
        seeds: tuple,
        run_as_user: int | None,
    ) -> dict:
        hexid = cname(user_id)[len("dshwork-") :]
        cpu = cpus or config.WORK_CPUS
        mem = mem_mb or config.WORK_MEM_LIMIT_MB

        def data_mount(sub: str, path: str) -> dict:
            return {"name": _K8S_DATA_VOLUME, "mountPath": path, "subPath": f"{hexid}/{sub}"}

        def seed_mount(sub: str, path: str) -> dict:
            m = {"name": _SEED_VOLUME, "mountPath": path}
            if sub:
                m["subPath"] = sub
            return m

        def env_list(items) -> list[dict]:
            return [{"name": k, "value": str(v)} for k, v in items]

        app: dict = {
            "name": "app",
            "image": image_ref or image,
            "command": ["sh", "-c", boot],
            "workingDir": "/workspace",
            "env": env_list(env.items()),
            "volumeMounts": [
                data_mount("home", "/root"),
                data_mount("workspace", "/workspace"),
                *(seed_mount(s, p) for s, p in seeds),
            ],
        }
        if run_as_user is not None:
            app["securityContext"] = {"runAsUser": run_as_user}
        containers = [self._harden(app)]
        # 伴随容器: 同一个 Pod 里共享网络命名空间 (互相 127.0.0.1), 与 ECI 容器组同义。
        for sc in sidecars:
            c: dict = {"name": sc.name, "image": sc.image_ref}
            if sc.cmd:
                c["command"] = list(sc.cmd)
            if sc.args:
                c["args"] = list(sc.args)
            if sc.env:
                c["env"] = env_list(sc.env)
            if sc.run_as_user is not None:
                c["securityContext"] = {"runAsUser": sc.run_as_user}
            mounts = [data_mount(sub, p) for sub, p in sc.mounts] + [seed_mount(s, p) for s, p in sc.seeds]
            if mounts:
                c["volumeMounts"] = mounts
            containers.append(self._harden(c, sidecar=True))
        inits = []
        for ic in init_containers:
            c = {
                "name": ic.name,
                "image": ic.image_ref,
                "volumeMounts": [
                    {"name": _SEED_VOLUME, "mountPath": ic.seed_mount},
                    *(data_mount(sub, p) for sub, p in ic.mounts),
                ],
            }
            if ic.cmd:
                c["command"] = list(ic.cmd)
            inits.append(self._harden(c))
        volumes = [self._data_volume()]
        if init_containers or seeds or any(sc.seeds for sc in sidecars):
            volumes.append({"name": _SEED_VOLUME, "emptyDir": {}})
        # OSS 同步: 恢复容器排在**所有**初始化容器之前 (产品的初始化容器可能往用户目录
        # 写预置配置, 得先有正本再写); 同步器紧随其后起, 免得先推了一份空的。
        sync = self._sync_enabled()
        if sync:
            restore, syncer = self._sync_containers(hexid)
            inits = [self._harden(restore), self._harden(syncer), *inits]
        spec: dict = {
            # 与 ECI 同一条规则: 栈产品 Always (应用容器在中间件就绪前会崩, 靠重启
            # 拉起来), 单容器 Never (它退出就是真的死了, 终态由 inspect 清掉重建)。
            "restartPolicy": "Always" if sidecars else "Never",
            # 5 秒: 工作台没有要优雅收尾的东西 (状态在卷上)。开了 OSS 同步就不同了:
            # 应用退出 + 全量推送要在这段时间里完成, 超时被杀等于推了一半。
            "terminationGracePeriodSeconds": config.K8S_SYNC_GRACE_S if sync else 5,
            # 工作台里跑的是用户的智能体 —— 不给它集群凭据, 也不注入服务发现变量。
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "resources": {
                "requests": {"cpu": _millicores(cpu / 4), "memory": f"{mem}Mi"},
                "limits": {"cpu": _millicores(cpu), "memory": f"{mem}Mi"},
            },
            "containers": containers,
            "volumes": volumes,
        }
        if inits:
            spec["initContainers"] = inits
        # gVisor: 按产品开。工作台键里带产品 id (products.wskey)。
        from .products import split_key  # 延迟导入, 保持单向依赖

        _, product_id = split_key(user_id)
        if self._gvisor_for(product_id) and (config.K8S_RUNTIME_CLASS or "").strip():
            spec["runtimeClassName"] = config.K8S_RUNTIME_CLASS.strip()
        if host_aliases:
            ok = [h for h in host_aliases if _RFC1123.fullmatch(h)]
            bad = [h for h in host_aliases if h not in ok]
            if bad:
                # 栈内互相走回环, 别名只兜"漏改的服务名引用"; 少几个不致命, 而带上
                # 它们整个 Pod 都建不出来。记一笔, 好知道哪些名字没兜住。
                log.warning("[work] %s: 主机别名 %s 不是合法 DNS 名, k8s 不收, 已跳过", user_id, bad)
            if ok:
                spec["hostAliases"] = [{"ip": "127.0.0.1", "hostnames": ok}]
        secret = (config.K8S_IMAGE_PULL_SECRET or "").strip()
        if secret:
            spec["imagePullSecrets"] = [{"name": secret}]
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name(user_id),
                "namespace": self._ns,
                "labels": {K8S_LABEL: "workspace"},
                "annotations": {K8S_ANN_USER: user_id, K8S_ANN_BOOTCFG: boot_fp},
            },
            "spec": spec,
        }

    def _report_stuck(self, user_id: str, pod: dict) -> None:
        if user_id in self._stuck_reported:
            return
        st = pod.get("status") or {}
        for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
            w = (cs.get("state") or {}).get("waiting") or {}
            if w.get("reason") in _K8S_STUCK_REASONS:
                self._stuck_reported.add(user_id)
                log.error(
                    "[work] %s 卡在 Pending: 容器 %s %s —— %s",
                    user_id,
                    cs.get("name"),
                    w.get("reason"),
                    (w.get("message") or "")[:200],
                )
                return

    # -- Backend ------------------------------------------------------------
    async def inspect(self, user_id: str) -> WorkInfo | None:
        pod = await self._get(user_id)
        if pod is None:
            return None
        meta = pod.get("metadata") or {}
        ann = meta.get("annotations") or {}
        status = pod.get("status") or {}
        spec = pod.get("spec") or {}
        phase = status.get("phase", "")
        image = ((spec.get("containers") or [{}])[0]).get("image", "")
        if meta.get("deletionTimestamp"):
            # 正在删。名字还被占着, 现在建会撞名 —— 报"存在但没在跑", 调用方会
            # 当它在启动而等下一轮; 等它真消失了, inspect 答 None, 那时才重建。
            return WorkInfo(
                running=False,
                boot_fp=ann.get(K8S_ANN_BOOTCFG, ""),
                image_id=image,
                host="",
                state="Terminating",
            )
        if phase not in _K8S_ALIVE:
            log.info("[work] %s 处于终态 %s, 清掉以便重建", user_id, phase or "?")
            await self._delete(user_id)
            self._stuck_reported.discard(user_id)
            return None
        if phase == "Pending":
            self._report_stuck(user_id, pod)
        else:
            self._stuck_reported.discard(user_id)
        return WorkInfo(
            running=phase == "Running",
            boot_fp=ann.get(K8S_ANN_BOOTCFG, ""),
            image_id=image,
            host=status.get("podIP", ""),
            state=phase,
        )

    async def current_image_id(self, image: str) -> str:
        """与 ECI 相同: 镜像身份就是仓库引用本身 (同名重推察觉不到, 镜像走版本号 tag)。"""
        return image

    async def create(
        self,
        user_id: str,
        *,
        boot: str,
        env: dict[str, str],
        boot_fp: str,
        image: str,
        image_ref: str = "",
        mem_mb: int = 0,
        cpus: float = 0.0,
        sidecars: tuple = (),
        host_aliases: tuple = (),
        init_containers: tuple = (),
        seeds: tuple = (),
        run_as_user: int | None = None,
    ) -> None:
        body = self._manifest(
            user_id,
            boot=boot,
            env=env,
            boot_fp=boot_fp,
            image=image,
            image_ref=image_ref,
            mem_mb=mem_mb,
            cpus=cpus,
            sidecars=sidecars,
            host_aliases=host_aliases,
            init_containers=init_containers,
            seeds=seeds,
            run_as_user=run_as_user,
        )
        await self._ensure_pull_secret()
        await self._ensure_sync_secret()
        r = await self._api("POST", self._pods, json_body=body)
        if r.status_code == 409:
            # 同名 Pod 还在 —— 多半是上一台正在删 (名字要等它走完才空出来)。
            # 等它消失再建一次, 而不是把"撞名"当成功: 那样用户会连到旧的那台。
            for _ in range(30):
                await asyncio.sleep(0.5)
                if await self._get(user_id) is None:
                    break
            r = await self._api("POST", self._pods, json_body=body)
        if r.status_code == 403 and "exceeded quota" in r.text:
            # 命名空间配额满了。上层把 "capacity" 翻成"名额已满"给用户看, 与
            # WORK_MAX_CONCURRENT 撞顶是同一个页面 —— 这本来就是同一件事。
            log.warning("[work] k8s 配额已满, 拒起 %s: %s", user_id, r.text[:200])
            raise RuntimeError("capacity")
        if r.status_code not in (200, 201, 202):
            raise K8sError(f"pod create failed: {r.status_code} {r.text[:300]}")
        res = body["spec"]["resources"]["limits"]
        log.info(
            "[work] 建 Pod %s user=%s cpu=%s mem=%s", pod_name(user_id), user_id, res["cpu"], res["memory"]
        )

    async def start(self, user_id: str) -> None:
        """与 ECI 相同: 没有这个动作 —— Pod 一创建就在起。"""

    async def release(self, user_id: str) -> None:
        # 停不了, 只能删。用户的东西在卷上, 下次访问重建。
        await self.destroy(user_id)

    async def destroy(self, user_id: str) -> None:
        await self._delete(user_id)

    async def running_users(self) -> list[str]:
        """计量与回收都遍历这个列表, 所以**漏掉一条 = 有人白用且永不回收**。
        按 label 列出我们的 Pod, 身份从注解读; 靠 continue 令牌翻页。"""
        users: list[str] = []
        params: dict = {"labelSelector": f"{K8S_LABEL}=workspace"}
        while True:
            r = await self._api("GET", self._pods, params=params)
            if r.status_code != 200:
                raise K8sError(f"list pods: {r.status_code} {r.text[:200]}")
            body = r.json()
            for pod in body.get("items") or []:
                meta = pod.get("metadata") or {}
                if meta.get("deletionTimestamp"):
                    continue
                if (pod.get("status") or {}).get("phase") != "Running":
                    continue
                uid = (meta.get("annotations") or {}).get(K8S_ANN_USER, "")
                if uid:
                    users.append(uid)
            cont = (body.get("metadata") or {}).get("continue")
            if not cont:
                break
            params = {**params, "continue": cont}
        return list(dict.fromkeys(users))


# --- 按产品分派 ---------------------------------------------------------------


class RoutedBackend(Backend):
    """把不同产品的工作台交给不同后端 (WORK_BACKEND_PRODUCTS)。

    工作台键里带产品 id (products.wskey), 所以带键的调用都能自己找到该去哪;
    running_users 是各后端的并集 —— 计量与回收按人遍历, 少一边就是有人白用。
    没有键的那几个 (capacity_reason / resumable) 按默认后端答: 前者只有 docker
    后端真的会看宿主内存, 后者只用于启动等待页的一句提示。
    """

    def __init__(self, default: Backend, by_product: dict[str, Backend]) -> None:
        self.default = default
        self.by_product = dict(by_product)
        self.resumable = default.resumable
        self.boot_hint = default.boot_hint

    def _pick(self, user_id: str) -> Backend:
        from .products import split_key  # 延迟导入: products 不依赖这里, 但保持单向

        _, pid = split_key(user_id)
        return self.by_product.get(pid, self.default)

    def for_product(self, product_id: str) -> Backend:
        return self.by_product.get(product_id, self.default)

    async def inspect(self, user_id: str) -> WorkInfo | None:
        return await self._pick(user_id).inspect(user_id)

    async def current_image_id(self, image: str) -> str:
        # 没有键可以路由; 调用方应走 for_product(...).current_image_id。这里按
        # 默认后端答, 让老调用方不至于炸掉。
        return await self.default.current_image_id(image)

    async def create(self, user_id: str, **kw) -> None:  # type: ignore[override]
        await self._pick(user_id).create(user_id, **kw)

    async def start(self, user_id: str) -> None:
        await self._pick(user_id).start(user_id)

    def _all(self, first: Backend | None = None) -> list[Backend]:
        """每个后端一次, 不重复; first 排最前。"""
        out: list[Backend] = []
        for b in [first, self.default, *self.by_product.values()]:
            if b is not None and all(b is not o for o in out):
                out.append(b)
        return out

    async def release(self, user_id: str) -> None:
        # 回收要**问遍所有后端**, 不只是该产品现在派给的那个: 把一个产品从 ECI
        # 切到 k8s 的那一刻, 它在 ECI 上还有实例在跑 —— running_users 数得到它
        # (并集), 回收器于是来 release, 只删 k8s 那边等于删了个不存在的 Pod,
        # ECI 那台按秒计费到天荒地老。删不存在的东西在每个后端都是空操作。
        for b in self._all(self._pick(user_id)):
            await b.release(user_id)

    async def destroy(self, user_id: str) -> None:
        for b in self._all(self._pick(user_id)):
            await b.destroy(user_id)

    async def running_users(self) -> list[str]:
        """各后端的并集。一个后端挂了 (k8s 节点掉线、凭据读不了) **不能拖着别的
        后端一起不计量不回收** —— 那边的实例还在按秒烧钱。所以按后端隔开: 坏的
        那个大声记一条错然后跳过, 好的照常。漏掉的那批等它恢复就回来。
        2026-09-03 首次上线就撞上: CA 文件容器里读不了, 回收循环整轮抛异常,
        ECI 上所有工作台跟着停止计量与回收。"""
        users: list[str] = []
        for b in self._all():
            try:
                users.extend(await b.running_users())
            except Exception:  # noqa: BLE001
                log.exception(
                    "[work] %s.running_users 失败 —— 这个后端上的工作台这一轮不计量不回收",
                    type(b).__name__,
                )
        return list(dict.fromkeys(users))

    def capacity_reason(self) -> str:
        return self.default.capacity_reason()

    def offline_workspace_dir(self, user_id: str) -> pathlib.Path | None:
        return self._pick(user_id).offline_workspace_dir(user_id)


def backend_named(name: str) -> Backend:
    n = (name or "docker").strip().lower()
    if n == "docker":
        return DockerBackend()
    if n == "eci":
        return EciBackend()
    if n == "k8s":
        return K8sBackend()
    raise ValueError(f"未知的工作台后端 {name!r} (可选 docker / eci / k8s)")


def make_backend() -> Backend:
    default = backend_named(config.WORK_BACKEND)
    spec = (config.WORK_BACKEND_PRODUCTS or "").strip()
    if not spec:
        return default
    instances: dict[str, Backend] = {(config.WORK_BACKEND or "docker").strip().lower(): default}
    by_product: dict[str, Backend] = {}
    for item in spec.split(","):
        if not item.strip():
            continue
        pid, sep, name = item.partition("=")
        pid, name = pid.strip(), name.strip().lower()
        if not sep or not pid or not name:
            raise ValueError(f"WORK_BACKEND_PRODUCTS 格式错误: {item!r} (应为 产品=后端, 逗号分隔)")
        if name not in instances:
            instances[name] = backend_named(name)
        by_product[pid] = instances[name]
    return RoutedBackend(default, by_product)
