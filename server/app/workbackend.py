"""Where a user's workspace container actually runs.

Two backends, same contract:

  docker  本机 (或自部署) 的 docker 引擎, 经受限 socket 代理。容器可以 stop 后
          再 start, 命名卷保留状态。
  eci     阿里云弹性容器实例。**没有"停止但保留"这个状态** —— 只能创建和删除,
          于是"闲置回收"等于销毁, 而恢复是一次完整冷启动。
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
import pathlib
import re
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
            **({"Container.1.SecurityContext.RunAsUser": str(run_as_user)}
               if run_as_user is not None else {}),
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
        hexid = cname(user_id)[len("dshwork-") :]
        for ci, sc in enumerate(sidecars, start=2):
            p[f"Container.{ci}.Name"] = sc.name
            p[f"Container.{ci}.Image"] = sc.image_ref
            if sc.run_as_user is not None:
                p[f"Container.{ci}.SecurityContext.RunAsUser"] = str(sc.run_as_user)
            for j, arg in enumerate(sc.cmd, start=1):
                p[f"Container.{ci}.Command.{j}"] = arg
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


def make_backend() -> Backend:
    if (config.WORK_BACKEND or "docker").lower() == "eci":
        return EciBackend()
    return DockerBackend()
