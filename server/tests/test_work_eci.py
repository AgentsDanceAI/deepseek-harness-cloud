"""ECI 后端。

这里盯的都是**错了也不会报错**的地方: release 变成 stop 会让实例永远漏着计费,
NAS 没挂上容器照样起得来只是用户的东西会随回收消失, 镜像缓存不匹配只是慢 50 秒。
每一条都不会在日志里喊, 所以只能靠测试。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app import config, workbackend  # noqa: E402
from app.workbackend import EciBackend, WorkInfo, cname  # noqa: E402


class FakeEci:
    """记下每次调用, 并按脚本回放响应。"""

    def __init__(self, groups=None):
        self.calls = []  # [(action, params)]
        self.groups = groups or []
        self.deleted = []
        self.total_count = None  # 覆盖它即可模拟"服务端说还有更多"
        self.next_token = None

    async def call(self, action, params=None):
        p = dict(params or {})
        self.calls.append((action, p))
        if action == "DescribeContainerGroups":
            gs = self.groups
            if p.get("ContainerGroupName"):
                gs = [g for g in gs if g["ContainerGroupName"] == p["ContainerGroupName"]]
            if p.get("Status"):
                gs = [g for g in gs if g.get("Status") == p["Status"]]
            out = {
                "ContainerGroups": gs,
                "TotalCount": self.total_count if self.total_count is not None else len(gs),
            }
            if self.next_token:
                out["NextToken"] = self.next_token
                self.next_token = None
            return out
        if action == "DeleteContainerGroup":
            self.deleted.append(p["ContainerGroupId"])
            self.groups = [g for g in self.groups if g["ContainerGroupId"] != p["ContainerGroupId"]]
            return {}
        if action == "CreateContainerGroup":
            return {"ContainerGroupId": "eci-created"}
        return {}

    def params_of(self, action):
        return [p for a, p in self.calls if a == action]


def _group(
    uid="u_abc",
    status="Running",
    ip="172.29.0.5",
    fp="deadbeef",
    image="ghcr.io/x/dsh:rc8",
    gid="eci-1",
    created="2026-08-24T05:24:15Z",
):
    return {
        "ContainerGroupId": gid,
        "CreationTime": created,
        "ContainerGroupName": cname(uid),
        "Status": status,
        "IntranetIp": ip,
        "Containers": [{"Image": image}],
        "Tags": [{"Key": "dshwork-user", "Value": uid}, {"Key": "dshwork-bootcfg", "Value": fp}],
    }


@pytest.fixture()
def eci(monkeypatch):
    monkeypatch.setattr(config, "ECI_REGION_ID", "ap-southeast-1")
    monkeypatch.setattr(config, "ECI_VSWITCH_ID", "vsw-x")
    monkeypatch.setattr(config, "ECI_SECURITY_GROUP_ID", "sg-x")
    monkeypatch.setattr(config, "ECI_ACCESS_KEY_ID", "ak")
    monkeypatch.setattr(config, "ECI_ACCESS_KEY_SECRET", "sk")
    monkeypatch.setattr(config, "WORK_IMAGE_REF", "ghcr.io/x/dsh:rc8")
    monkeypatch.setattr(config, "WORK_NAS_SERVER", "")
    b = EciBackend()
    fake = FakeEci()
    monkeypatch.setattr(b, "_call", fake.call)
    return b, fake


# --- 签名: 已对着真实 API 验过, 这里钉住容易被"整理"掉的编码规则 -------------


def test_signature_encoding_follows_the_rules_aliyun_actually_uses():
    """RFC3986 之外阿里云还要求 + -> %20、* -> %2A、~ 保留原样。用标准
    quote() 会得到 + 和 *, 签名就对不上, 而报错是 SignatureDoesNotMatch ——
    完全看不出是编码问题。"""
    assert workbackend._pe(" ") == "%20"
    assert workbackend._pe("*") == "%2A"
    assert workbackend._pe("~") == "~"
    assert workbackend._pe("/") == "%2F"


def test_signature_is_stable_for_the_same_input():
    p = {"Action": "X", "B": "2", "A": "1"}
    assert workbackend._sign(p, "s") == workbackend._sign(dict(reversed(list(p.items()))), "s")


# --- 状态映射 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_instance_reports_its_vpc_ip_as_the_host(eci):
    b, fake = eci
    fake.groups = [_group(ip="172.29.181.220")]
    info = await b.inspect("u_abc")
    assert info == WorkInfo(
        running=True, boot_fp="deadbeef", image_id="ghcr.io/x/dsh:rc8", host="172.29.181.220", state="Running"
    )


@pytest.mark.asyncio
async def test_pending_instance_is_not_running_but_still_exists(eci):
    """Pending 期间必须报"存在但没 running" —— 若报成不存在, ensure_workspace
    会在冷启动的 19 秒里反复再建, 每次都是一台新的计费实例。"""
    b, fake = eci
    fake.groups = [_group(status="Scheduling", ip="")]
    info = await b.inspect("u_abc")
    assert info is not None and info.running is False


@pytest.mark.asyncio
async def test_terminal_instance_is_cleaned_up_and_reported_gone(eci):
    """ECI 不能 start, 所以终态实例只能删掉重建 —— 而它还占着名字, 不删下一次
    CreateContainerGroup 会一直撞名。"""
    b, fake = eci
    fake.groups = [_group(status="Failed")]
    assert await b.inspect("u_abc") is None
    assert fake.deleted == ["eci-1"]


# --- 回收语义: 这条错了会永远漏计费 -----------------------------------------


@pytest.mark.asyncio
async def test_release_deletes_because_eci_cannot_stop(eci):
    """ECI 没有"停止但保留"。若谁把 release 改成一个 stop 类调用, 实例会一直
    Running 下去 —— 按秒计费, 而闲置回收看起来"成功了"。"""
    b, fake = eci
    fake.groups = [_group()]
    await b.release("u_abc")
    assert fake.deleted == ["eci-1"]
    assert [a for a, _ in fake.calls if a not in ("DescribeContainerGroups", "DeleteContainerGroup")] == []


@pytest.mark.asyncio
async def test_destroying_something_absent_is_not_an_error(eci):
    b, fake = eci
    await b.destroy("u_nobody")
    assert fake.deleted == []


# --- 创建参数 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_asks_for_the_image_cache(eci):
    """没有 AutoMatchImageCache 就退回全量拉取: 冷启动 19s -> 50s, 不报错、
    不告警, 只是每个用户每次都多等半分钟。"""
    b, fake = eci
    await b.create(
        "u_abc",
        boot="echo hi",
        env={},
        boot_fp="fp1",
        image="dsh-local:test",
        image_ref="ghcr.io/x/dsh:rc8",
    )
    p = fake.params_of("CreateContainerGroup")[0]
    assert p["AutoMatchImageCache"] == "true"
    # image_ref 优先于 image: ECI 要能从仓库拉, 本机 tag 对它没意义
    assert p["Container.1.Image"] == "ghcr.io/x/dsh:rc8"
    assert p["RestartPolicy"] == "Never"


@pytest.mark.asyncio
async def test_create_stamps_user_and_boot_fingerprint_as_tags(eci):
    b, fake = eci
    await b.create("u_abc", boot="echo hi", env={}, boot_fp="fp1", image="dsh-local:test")
    p = fake.params_of("CreateContainerGroup")[0]
    tags = {p["Tag.1.Key"]: p["Tag.1.Value"], p["Tag.2.Key"]: p["Tag.2.Value"]}
    assert tags == {"dshwork-user": "u_abc", "dshwork-bootcfg": "fp1"}


@pytest.mark.asyncio
async def test_boot_script_and_env_reach_the_container(eci):
    b, fake = eci
    await b.create(
        "u_abc", boot="exec dsh web", env={"A": "1", "B": "2"}, boot_fp="fp", image="dsh-local:test"
    )
    p = fake.params_of("CreateContainerGroup")[0]
    assert [p["Container.1.Command.1"], p["Container.1.Arg.1"], p["Container.1.Arg.2"]] == [
        "sh",
        "-c",
        "exec dsh web",
    ]
    got = {
        p[f"Container.1.EnvironmentVar.{i}.Key"]: p[f"Container.1.EnvironmentVar.{i}.Value"] for i in (1, 2)
    }
    assert got == {"A": "1", "B": "2"}


@pytest.mark.asyncio
async def test_without_nas_no_volume_is_mounted(eci, caplog):
    """允许无 NAS 跑 (冒烟验证用), 但必须喊一声: 那种形态下工作台是一次性的。"""
    b, fake = eci
    with caplog.at_level("WARNING"):
        await b.create("u_abc", boot="x", env={}, boot_fp="fp", image="dsh-local:test")
    p = fake.params_of("CreateContainerGroup")[0]
    assert not [k for k in p if k.startswith("Volume.")]
    assert "WORK_NAS_SERVER" in caplog.text


@pytest.mark.asyncio
async def test_nas_mounts_home_and_workspace_under_per_user_subpaths(eci, monkeypatch):
    """两个用户共用一个 NFS 卷、各自一个 SubPath。若 SubPath 丢了, 所有人会挂到
    同一个目录上 —— 互相看得见、改得动对方的文件。"""
    b, fake = eci
    monkeypatch.setattr(config, "WORK_NAS_SERVER", "nas.example.com")
    monkeypatch.setattr(config, "WORK_NAS_PATH", "/dshwork")
    await b.create("u_abc", boot="x", env={}, boot_fp="fp", image="dsh-local:test")
    p = fake.params_of("CreateContainerGroup")[0]
    assert p["Volume.1.Type"] == "NFSVolume"
    assert p["Volume.1.NFSVolume.Server"] == "nas.example.com"
    assert p["Volume.1.NFSVolume.Path"] == "/dshwork"
    hexid = cname("u_abc")[len("dshwork-") :]
    mounts = {
        p[f"Container.1.VolumeMount.{i}.MountPath"]: p[f"Container.1.VolumeMount.{i}.SubPath"] for i in (1, 2)
    }
    assert mounts == {"/root": f"{hexid}/home", "/workspace": f"{hexid}/workspace"}


@pytest.mark.asyncio
async def test_two_users_never_share_a_subpath(eci, monkeypatch):
    b, fake = eci
    monkeypatch.setattr(config, "WORK_NAS_SERVER", "nas.example.com")
    await b.create("u_aaa", boot="x", env={}, boot_fp="fp", image="dsh-local:test")
    await b.create("u_bbb", boot="x", env={}, boot_fp="fp", image="dsh-local:test")
    subs = [p["Container.1.VolumeMount.1.SubPath"] for p in fake.params_of("CreateContainerGroup")]
    assert subs[0] != subs[1]


# --- 计量口径 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_users_comes_from_tags_not_names(eci):
    b, fake = eci
    fake.groups = [_group(uid="u_aaa"), dict(_group(uid="u_bbb"), ContainerGroupId="eci-2")]
    assert sorted(await b.running_users()) == ["u_aaa", "u_bbb"]
    p = fake.params_of("DescribeContainerGroups")[0]
    assert p["Status"] == "Running"
    # 此 API 将 Limit 与 ContainerGroupId 绑定；全量列举不能单独传 Limit。
    assert "Limit" not in p


@pytest.mark.asyncio
async def test_pagination_follows_the_next_token(eci):
    b, fake = eci
    fake.groups = [_group(uid="u_aaa")]
    fake.next_token = "page2"
    await b.running_users()
    assert fake.params_of("DescribeContainerGroups")[1]["NextToken"] == "page2"


@pytest.mark.asyncio
async def test_a_truncated_listing_is_shouted_about(eci, caplog):
    """少列出来的那些工作台会白用且永不回收。安静地少算钱是最坏的失败方式,
    所以宁可吵。"""
    b, fake = eci
    fake.groups = [_group(uid="u_aaa")]
    fake.total_count = 7  # 服务端说有 7 个, 却只给了 1 个且没有续页
    with caplog.at_level("ERROR"):
        await b.running_users()
    assert "1/7" in caplog.text


@pytest.mark.asyncio
async def test_eci_is_not_resumable():
    """workspace.py 靠这个区分"能 start"和"只能重建"。"""
    assert EciBackend.resumable is False
    assert workbackend.DockerBackend.resumable is True


# --- 用户看到的等待时间, 必须跟后端一致 --------------------------------------


def test_boot_wait_hint_follows_the_backend(monkeypatch):
    """等待提示按后端启动方式区分：重启容器比新建远程实例更快。"""
    from app import workspace

    monkeypatch.setattr(workspace, "_backend", workbackend.DockerBackend())
    assert workspace._boot_wait_hint() == "5–20 秒"
    monkeypatch.setattr(workspace, "_backend", EciBackend())
    assert workspace._boot_wait_hint() == "20–40 秒"


# --- 挂载失败: 唯一的症状是"一直在启动" --------------------------------------


@pytest.mark.asyncio
async def test_a_stuck_mount_is_reported_once(eci, caplog):
    """挂载失败会让实例停在 Pending，必须记录一次明确诊断。"""
    b, fake = eci
    g = _group(status="Pending", ip="")
    g["Events"] = [
        {
            "Type": "Warning",
            "Message": 'MountVolume.SetUp failed for volume "dshwork-nas" : file does not exist',
        }
    ]
    fake.groups = [g]
    with caplog.at_level("ERROR"):
        info = await b.inspect("u_abc")
    assert info is not None and info.running is False  # 仍然报"在起", 别把它当不存在
    assert "WORK_NAS_PATH" in caplog.text

    caplog.clear()
    with caplog.at_level("ERROR"):
        await b.inspect("u_abc")
    assert caplog.text == ""  # 冷路径每 30 秒来一次, 不能每次都刷


@pytest.mark.asyncio
async def test_a_healthy_pending_says_nothing(eci, caplog):
    b, fake = eci
    fake.groups = [_group(status="Pending", ip="")]
    with caplog.at_level("ERROR"):
        await b.inspect("u_abc")
    assert caplog.text == ""


# --- 「個人成品」的离线视图: 走岔了不会报错, 只会永远空白 --------------------


def test_offline_dir_layout_matches_what_gets_mounted(eci, monkeypatch, tmp_path):
    """离线视图的路径和创建实例时的 SubPath 是同一个位置的两种写法。
    走岔了不报错, 只是那个页面永远列不出东西 —— 而在 ECI 上容器闲置即销毁,
    那个页面是用户唯一能看见自己文件的地方。"""
    b, fake = eci
    monkeypatch.setattr(config, "WORK_NAS_LOCAL_MOUNT", str(tmp_path))
    monkeypatch.setattr(config, "WORK_NAS_SERVER", "nas.example.com")
    hexid = cname("u_abc")[len("dshwork-") :]
    (tmp_path / hexid / "workspace").mkdir(parents=True)

    got = b.offline_workspace_dir("u_abc")
    assert got == tmp_path / hexid / "workspace"

    # 与 create() 真正下发的 SubPath 对齐
    import asyncio

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        b.create("u_abc", boot="x", env={}, boot_fp="fp", image="dsh-local:test")
    )
    p = fake.params_of("CreateContainerGroup")[0]
    sub = p["Container.1.VolumeMount.2.SubPath"]  # /workspace 那条
    assert p["Container.1.VolumeMount.2.MountPath"] == "/workspace"
    assert str(got).endswith(sub), f"离线路径 {got} 与挂载 SubPath {sub} 对不上"


def test_offline_dir_is_none_without_a_local_mount(eci, monkeypatch):
    b, _ = eci
    monkeypatch.setattr(config, "WORK_NAS_LOCAL_MOUNT", "")
    assert b.offline_workspace_dir("u_abc") is None


def test_offline_dir_is_none_when_the_user_has_nothing_yet(eci, monkeypatch, tmp_path):
    b, _ = eci
    monkeypatch.setattr(config, "WORK_NAS_LOCAL_MOUNT", str(tmp_path))
    assert b.offline_workspace_dir("u_neverran") is None


# --- 同名重复实例 ------------------------------------------------------------
# 2026-08-24 05:24:15/16 真的建出过两台同名且都 Running 的实例。ECI 不保证
# ContainerGroupName 唯一, 而这里每一条错法都只表现为账单变大, 不会报错。


def _dupes(uid="u_abc"):
    """线上那次的形状: 同名、同 boot_fp、创建时间差一秒、都在跑。"""
    return [
        _group(uid=uid, gid="eci-new", created="2026-08-24T05:24:16Z", ip="172.29.181.245"),
        _group(uid=uid, gid="eci-old", created="2026-08-24T05:24:15Z", ip="172.29.181.244"),
    ]


@pytest.mark.asyncio
async def test_a_duplicated_instance_is_metered_once_not_twice(eci):
    """计量遍历的是这个列表, 同一个人出现两次就等于一分钟扣两分钟机时。"""
    b, fake = eci
    fake.groups = _dupes()
    assert await b.running_users() == ["u_abc"]


@pytest.mark.asyncio
async def test_inspect_deletes_the_extra_instance_and_keeps_one(eci, caplog):
    """多出来的那台按秒烧钱、各占一个 EIP, 而用户只看得到一台。"""
    b, fake = eci
    fake.groups = _dupes()
    with caplog.at_level("ERROR"):
        info = await b.inspect("u_abc")
    assert fake.deleted == ["eci-old"], f"没把多余的实例删掉: deleted={fake.deleted}"
    assert info is not None and info.host == "172.29.181.245"
    assert "重复计费" in caplog.text, "重复计费必须喊出来, 否则只有账单知道"


@pytest.mark.asyncio
async def test_which_duplicate_survives_does_not_depend_on_listing_order(eci):
    """并发自愈必须收敛到同一个选择, 否则两边各删一台就把两台都删了。"""
    b, fake = eci
    fake.groups = list(reversed(_dupes()))
    info = await b.inspect("u_abc")
    assert fake.deleted == ["eci-old"]
    assert info is not None and info.host == "172.29.181.245"


@pytest.mark.asyncio
async def test_a_terminal_duplicate_does_not_take_the_running_one_with_it(eci):
    """ "终态实例占着名字要删掉"不能顺手把还在跑的那台删了。"""
    b, fake = eci
    fake.groups = [
        _group(gid="eci-dead", status="Succeeded", created="2026-08-24T05:24:16Z"),
        _group(gid="eci-live", status="Running", created="2026-08-24T05:24:15Z", ip="172.29.0.9"),
    ]
    info = await b.inspect("u_abc")
    assert fake.deleted == ["eci-dead"]
    assert info is not None and info.running and info.host == "172.29.0.9"


@pytest.mark.asyncio
async def test_destroy_removes_every_instance_with_that_name(eci):
    """只删第一个, 剩下那台就永远按秒计费 —— 没人看得见, 也不会再被回收。"""
    b, fake = eci
    fake.groups = _dupes()
    await b.destroy("u_abc")
    assert sorted(fake.deleted) == ["eci-new", "eci-old"]
    assert fake.groups == []


@pytest.mark.asyncio
async def test_release_also_removes_every_instance_with_that_name(eci):
    b, fake = eci
    fake.groups = _dupes()
    await b.release("u_abc")
    assert sorted(fake.deleted) == ["eci-new", "eci-old"]


# --- 自有的实例台账 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_creating_an_instance_is_logged_with_who_it_was_for(eci, caplog):
    """每台实例自动创建一个 EIP, 所以这行日志就是"谁在什么时候占了一个 EIP"的
    唯一自有记录。

    没有它, "最近这些 EIP 都是谁申请的"只能去翻操作审计或账单 —— 两个接口都要
    额外的 RAM 权限, 而且都只会显示同一个子账号在调, 落不到具体用户身上。
    """
    b, _fake = eci
    with caplog.at_level("INFO", logger="dhc.work"):
        await b.create("u_abc", boot="x", env={}, boot_fp="fp", image="dsh-local:test")
    assert "eci-created" in caplog.text
    assert "u_abc" in caplog.text


@pytest.mark.asyncio
async def test_deleting_an_instance_is_logged_too(eci, caplog):
    """建和删要成对, 否则日志只能看出占了多少个 EIP, 看不出还了没有。"""
    b, _fake = eci
    _fake.groups = [_group()]
    with caplog.at_level("INFO", logger="dhc.work"):
        await b.release("u_abc")
    assert "eci-1" in caplog.text


# ---- 多容器栈 (Coze/Dify/Penpot 这类 compose 栈产品的底座) ----------------


@pytest.mark.asyncio
async def test_stack_product_emits_sidecar_containers(eci):
    """伴随容器要真的进 CreateContainerGroup 的参数。

    这里的每一项都是"错了也不会报错"的: 少一个容器就是应用连不上数据库 (一堆
    500); RestartPolicy 还是 Never 的话, 应用容器在中间件就绪前崩一次就永远
    躺平 —— ECI 没有 depends_on, 全靠 Always 拉起来。
    """
    from app.products import Sidecar

    b, fake = eci
    await b.create(
        "u_stack",
        boot="run-app",
        env={"A": "1"},
        boot_fp="fp",
        image="app:x",
        image_ref="ghcr.io/x/app:x",
        mem_mb=4096,
        cpus=2.0,
        sidecars=(
            Sidecar(
                name="db",
                image_ref="postgres:15-alpine",
                env=(("POSTGRES_PASSWORD", "pw"),),
                mounts=(("dify/pg", "/var/lib/postgresql/data"),),
            ),
            Sidecar(name="cache", image_ref="redis:7-alpine", cmd=("redis-server", "--save", "60", "1")),
        ),
    )
    action, p = next(c for c in fake.calls if c[0] == "CreateContainerGroup")
    # 主容器不动
    assert p["Container.1.Name"] == "app"
    # 伴随容器逐个到位
    assert p["Container.2.Name"] == "db"
    assert p["Container.2.Image"] == "postgres:15-alpine"
    assert p["Container.2.EnvironmentVar.1.Key"] == "POSTGRES_PASSWORD"
    assert p["Container.3.Name"] == "cache"
    assert p["Container.3.Command.1"] == "redis-server"
    # 中间件的数据要跟着用户走: NAS 卷 + 用户 hexid 前缀的子路径
    hexid = cname("u_stack")[len("dshwork-"):]
    assert p["Container.2.VolumeMount.1.Name"] == "dshwork-nas"
    assert p["Container.2.VolumeMount.1.SubPath"] == f"{hexid}/dify/pg"
    assert p["Container.2.VolumeMount.1.MountPath"] == "/var/lib/postgresql/data"
    # 栈产品必须 Always —— 崩溃重试是唯一的启动顺序机制
    assert p["RestartPolicy"] == "Always"


@pytest.mark.asyncio
async def test_init_container_seeds_a_shared_volume_for_the_whole_group(eci, monkeypatch):
    """初始化容器 + EmptyDir 种子卷要真的进参数, 编号还要跟 NAS 的挂载接得上。

    这是把 compose 的 bind mount 搬进 ECI 的那条路 (见 products.InitContainer)。
    每一项都是"错了也不报错"的:

      · 少了 InitContainer -> 卷是空的, 各容器读不到配置。症状散在别处:
        mysql 没有建库 SQL、ES 没有分词器、nginx 起不来 —— 没有一条指向种子卷。
      · VolumeMount 编号跳号 -> 阿里云按"到此为止"截断, 后面的挂载**静默消失**。
        所以这里钉死: 配了 NAS 时主容器的种子挂载从 3 开始 (1、2 是 NAS 的两个),
        没配 NAS 时从 1 开始。
    """
    from app.products import InitContainer, Sidecar

    b, fake = eci
    monkeypatch.setattr(config, "WORK_NAS_SERVER", "nas.example.com")
    monkeypatch.setattr(config, "WORK_NAS_PATH", "/dshwork")
    await b.create(
        "u_seed",
        boot="run-app", env={}, boot_fp="fp", image="web:x", image_ref="web:x",
        init_containers=(InitContainer(
            name="seed", image_ref="ghcr.io/x/coze-assets:t",
            cmd=("sh", "-c", "cp -a /assets/. /seed/"),
        ),),
        seeds=(("nginx", "/seed"),),
        sidecars=(Sidecar(name="db", image_ref="mysql:8.4.5",
                          mounts=(("coze/mysql", "/var/lib/mysql"),),
                          seeds=(("", "/seed"), ("conf", "/app/resources/conf"))),),
    )
    _, p = next(c for c in fake.calls if c[0] == "CreateContainerGroup")

    # NAS 是 Volume.1, 种子卷接在它后面
    assert p["Volume.1.Type"] == "NFSVolume"
    assert p["Volume.2.Name"] == "dshwork-seed"
    assert p["Volume.2.Type"] == "EmptyDirVolume"

    assert p["InitContainer.1.Name"] == "seed"
    assert p["InitContainer.1.Image"] == "ghcr.io/x/coze-assets:t"
    assert p["InitContainer.1.Command.3"] == "cp -a /assets/. /seed/"
    assert p["InitContainer.1.VolumeMount.1.Name"] == "dshwork-seed"
    assert p["InitContainer.1.VolumeMount.1.MountPath"] == "/seed"

    # 主容器: NAS 占了 1、2, 种子接 3 —— 不能跳号
    assert p["Container.1.VolumeMount.3.Name"] == "dshwork-seed"
    assert p["Container.1.VolumeMount.3.MountPath"] == "/seed"
    assert p["Container.1.VolumeMount.3.SubPath"] == "nginx"

    # 伴随容器: 它自己的 NAS 挂载占 1, 两个种子挂载接 2、3
    assert p["Container.2.VolumeMount.1.Name"] == "dshwork-nas"
    assert p["Container.2.VolumeMount.2.Name"] == "dshwork-seed"
    assert "Container.2.VolumeMount.2.SubPath" not in p, "子路径为空 = 挂整卷, 不该发 SubPath"
    assert p["Container.2.VolumeMount.3.SubPath"] == "conf"
    assert p["Container.2.VolumeMount.3.MountPath"] == "/app/resources/conf"


@pytest.mark.asyncio
async def test_seed_volume_takes_slot_one_when_there_is_no_nas(eci):
    """没配 NAS 时种子卷就是 Volume.1, 主容器的种子挂载从 1 开始。

    留空号 (跳到 Volume.2 / VolumeMount.3) 的后果是整组参数被截断, 而阿里云
    照样返回成功 —— 实例起来了, 卷是空的。"""
    from app.products import InitContainer

    b, fake = eci  # 这个 fixture 里 WORK_NAS_SERVER 是空的
    await b.create(
        "u_nonas", boot="run", env={}, boot_fp="fp", image="web:x", image_ref="web:x",
        init_containers=(InitContainer(name="seed", image_ref="a:t", cmd=("sh",)),),
        seeds=(("nginx", "/seed"),),
    )
    _, p = next(c for c in fake.calls if c[0] == "CreateContainerGroup")
    assert "Volume.2.Name" not in p
    assert p["Volume.1.Name"] == "dshwork-seed"
    assert p["Volume.1.Type"] == "EmptyDirVolume"
    assert p["Container.1.VolumeMount.1.Name"] == "dshwork-seed"


@pytest.mark.asyncio
async def test_stack_host_aliases_land_in_etc_hosts(eci):
    """compose 服务名 -> 127.0.0.1 要真的进 HostAliase 参数。

    少了它, 镜像配置模板里写死的 "http://api:5001" 解析不了, 应用容器起来就
    连不上兄弟服务 —— 而这在 ECI 侧一个错误都不报, 只有应用日志里一堆
    connection refused。"""
    b, fake = eci
    await b.create(
        "u_alias", boot="x", env={}, boot_fp="fp", image="a:1", image_ref="r/a:1",
        mem_mb=4096, cpus=2.0,
        sidecars=(__import__("app.products", fromlist=["Sidecar"]).Sidecar(
            name="db", image_ref="postgres:15"),),
        host_aliases=("mysql", "redis", "api", "web"),
    )
    _, p = next(c for c in fake.calls if c[0] == "CreateContainerGroup")
    assert p["HostAliase.1.Ip"] == "127.0.0.1"
    assert p["HostAliase.1.Hostname.1"] == "mysql"
    assert p["HostAliase.1.Hostname.4"] == "web"


@pytest.mark.asyncio
async def test_single_container_products_keep_restart_never(eci):
    """单容器保持 Never: 它退出就是真的死了, Always 会把一个坏容器变成永动的
    计费器 —— 用户看着 502, 钱照扣。"""
    b, fake = eci
    await b.create(
        "u_single", boot="x", env={}, boot_fp="fp", image="a:1", image_ref="r/a:1",
        mem_mb=1024, cpus=0.5,
    )
    _, p = next(c for c in fake.calls if c[0] == "CreateContainerGroup")
    assert p["RestartPolicy"] == "Never"
    assert "Container.2.Name" not in p, "没有伴随容器却拼出了 Container.2"


def test_docker_backend_refuses_stack_products():
    """docker 后端拼不出共享网络命名空间的容器组 —— 必须明确拒绝。
    静默忽略伴随容器 = 应用起了连不上库, 一堆 500, 谁也想不到是后端不支持。"""
    import asyncio

    from app.products import Sidecar
    from app.workbackend import DockerBackend

    b = DockerBackend.__new__(DockerBackend)  # 不碰 docker socket
    with pytest.raises(RuntimeError, match="ECI"):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            b.create("u_x", boot="b", env={}, boot_fp="f", image="i",
                     sidecars=(Sidecar(name="db", image_ref="postgres:15"),))
        )
