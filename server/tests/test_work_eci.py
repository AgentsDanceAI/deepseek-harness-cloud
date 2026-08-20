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
        self.calls = []            # [(action, params)]
        self.groups = groups or []
        self.deleted = []
        self.total_count = None    # 覆盖它即可模拟"服务端说还有更多"
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
            out = {"ContainerGroups": gs, "TotalCount": self.total_count
                   if self.total_count is not None else len(gs)}
            if self.next_token:
                out["NextToken"] = self.next_token
                self.next_token = None
            return out
        if action == "DeleteContainerGroup":
            self.deleted.append(p["ContainerGroupId"])
            self.groups = [g for g in self.groups if g["ContainerGroupId"] != p["ContainerGroupId"]]
            return {}
        return {}

    def params_of(self, action):
        return [p for a, p in self.calls if a == action]


def _group(uid="u_abc", status="Running", ip="172.29.0.5", fp="deadbeef",
           image="ghcr.io/x/dsh:rc8"):
    return {
        "ContainerGroupId": "eci-1",
        "ContainerGroupName": cname(uid),
        "Status": status,
        "IntranetIp": ip,
        "Containers": [{"Image": image}],
        "Tags": [{"Key": "dshwork-user", "Value": uid},
                 {"Key": "dshwork-bootcfg", "Value": fp}],
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
    assert info == WorkInfo(running=True, boot_fp="deadbeef",
                            image_id="ghcr.io/x/dsh:rc8",
                            host="172.29.181.220", state="Running")


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
    assert [a for a, _ in fake.calls if a not in
            ("DescribeContainerGroups", "DeleteContainerGroup")] == []


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
    await b.create("u_abc", boot="echo hi", env={}, boot_fp="fp1")
    p = fake.params_of("CreateContainerGroup")[0]
    assert p["AutoMatchImageCache"] == "true"
    assert p["Container.1.Image"] == "ghcr.io/x/dsh:rc8"
    assert p["RestartPolicy"] == "Never"


@pytest.mark.asyncio
async def test_create_stamps_user_and_boot_fingerprint_as_tags(eci):
    b, fake = eci
    await b.create("u_abc", boot="echo hi", env={}, boot_fp="fp1")
    p = fake.params_of("CreateContainerGroup")[0]
    tags = {p["Tag.1.Key"]: p["Tag.1.Value"], p["Tag.2.Key"]: p["Tag.2.Value"]}
    assert tags == {"dshwork-user": "u_abc", "dshwork-bootcfg": "fp1"}


@pytest.mark.asyncio
async def test_boot_script_and_env_reach_the_container(eci):
    b, fake = eci
    await b.create("u_abc", boot="exec dsh web", env={"A": "1", "B": "2"}, boot_fp="fp")
    p = fake.params_of("CreateContainerGroup")[0]
    assert [p["Container.1.Command.1"], p["Container.1.Arg.1"], p["Container.1.Arg.2"]] \
        == ["sh", "-c", "exec dsh web"]
    got = {p[f"Container.1.EnvironmentVar.{i}.Key"]: p[f"Container.1.EnvironmentVar.{i}.Value"]
           for i in (1, 2)}
    assert got == {"A": "1", "B": "2"}


@pytest.mark.asyncio
async def test_without_nas_no_volume_is_mounted(eci, caplog):
    """允许无 NAS 跑 (冒烟验证用), 但必须喊一声: 那种形态下工作台是一次性的。"""
    b, fake = eci
    with caplog.at_level("WARNING"):
        await b.create("u_abc", boot="x", env={}, boot_fp="fp")
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
    await b.create("u_abc", boot="x", env={}, boot_fp="fp")
    p = fake.params_of("CreateContainerGroup")[0]
    assert p["Volume.1.Type"] == "NFSVolume"
    assert p["Volume.1.NFSVolume.Server"] == "nas.example.com"
    assert p["Volume.1.NFSVolume.Path"] == "/dshwork"
    hexid = cname("u_abc")[len("dshwork-"):]
    mounts = {p[f"Container.1.VolumeMount.{i}.MountPath"]:
              p[f"Container.1.VolumeMount.{i}.SubPath"] for i in (1, 2)}
    assert mounts == {"/root": f"{hexid}/home", "/workspace": f"{hexid}/workspace"}


@pytest.mark.asyncio
async def test_two_users_never_share_a_subpath(eci, monkeypatch):
    b, fake = eci
    monkeypatch.setattr(config, "WORK_NAS_SERVER", "nas.example.com")
    await b.create("u_aaa", boot="x", env={}, boot_fp="fp")
    await b.create("u_bbb", boot="x", env={}, boot_fp="fp")
    subs = [p["Container.1.VolumeMount.1.SubPath"]
            for p in fake.params_of("CreateContainerGroup")]
    assert subs[0] != subs[1]


# --- 计量口径 ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_running_users_comes_from_tags_not_names(eci):
    b, fake = eci
    fake.groups = [_group(uid="u_aaa"), dict(_group(uid="u_bbb"), ContainerGroupId="eci-2")]
    assert sorted(await b.running_users()) == ["u_aaa", "u_bbb"]
    p = fake.params_of("DescribeContainerGroups")[0]
    assert p["Status"] == "Running"
    # 实测: 单独传 Limit 会被 ECI 要求 ContainerGroupId, 整个列举因此失败 ——
    # 而失败的列举意味着没人被计量、没人被回收。
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
    fake.total_count = 7           # 服务端说有 7 个, 却只给了 1 个且没有续页
    with caplog.at_level("ERROR"):
        await b.running_users()
    assert "1/7" in caplog.text


@pytest.mark.asyncio
async def test_eci_is_not_resumable():
    """workspace.py 靠这个区分"能 start"和"只能重建"。"""
    assert EciBackend.resumable is False
    assert workbackend.DockerBackend.resumable is True
