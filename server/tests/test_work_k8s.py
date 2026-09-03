"""k8s 后端与按产品分派。

与 ECI 那份同一姿态: 盯的是**错了也不会报错**的地方 —— 撞名当成功会让用户连到旧
的那台; 用户键写进 label 会被 API 拒掉 (那是 400, 会报); 但写进错的注解键就只是
running_users 永远为空, 计量与回收静默失效。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app import config, products, workbackend  # noqa: E402
from app.workbackend import (  # noqa: E402
    K8S_ANN_BOOTCFG,
    K8S_ANN_USER,
    K8S_LABEL,
    K8sBackend,
    RoutedBackend,
    WorkInfo,
    cname,
    pod_name,
)


class _Resp:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeK8s:
    """一个只认识 pods 资源的假 API server。"""

    def __init__(self):
        self.pods: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.quota_full = False
        # DELETE 之后名字还占着几次 GET (模拟宽限期), 0 = 立刻消失
        self.linger_gets = 0
        self._lingering: dict[str, int] = {}

    async def call(self, method, path, *, json_body=None, params=None):
        self.calls.append((method, path, json_body, params))
        if "/secrets" in path:
            return self._secrets(method, path, json_body)
        name = path.rsplit("/", 1)[-1] if path.count("/pods/") else None
        if method == "GET" and name:
            if name in self._lingering:
                self._lingering[name] -= 1
                pod = dict(self.pods[name])
                pod["metadata"] = {**pod["metadata"], "deletionTimestamp": "2026-09-03T00:00:00Z"}
                if self._lingering[name] <= 0:
                    del self._lingering[name]
                    del self.pods[name]
                return _Resp(200, pod)
            return _Resp(200, self.pods[name]) if name in self.pods else _Resp(404, {"reason": "NotFound"})
        if method == "GET":
            sel = (params or {}).get("labelSelector", "")
            items = [p for p in self.pods.values() if sel == "" or self._matches(p, sel)]
            return _Resp(200, {"items": items, "metadata": {}})
        if method == "POST":
            n = json_body["metadata"]["name"]
            if self.quota_full:
                return _Resp(403, {"message": 'pods "x" is forbidden: exceeded quota: dsh-workspaces'})
            if n in self.pods:
                return _Resp(409, {"reason": "AlreadyExists"})
            self.pods[n] = {
                **json_body,
                "status": {"phase": "Pending"},
            }
            return _Resp(201, self.pods[n])
        if method == "DELETE" and name:
            if name not in self.pods:
                return _Resp(404, {"reason": "NotFound"})
            if self.linger_gets:
                self._lingering[name] = self.linger_gets
            else:
                del self.pods[name]
            return _Resp(200, {})
        raise AssertionError(f"unexpected {method} {path}")

    secrets: dict[str, dict] = {}
    secret_status = None  # 覆盖它让 secrets 接口固定回某个状态码

    def _secrets(self, method, path, body):
        if self.secret_status is not None:
            return _Resp(self.secret_status, {"message": "nope"})
        if method == "POST":
            n = body["metadata"]["name"]
            if n in self.secrets:
                return _Resp(409, {"reason": "AlreadyExists"})
            self.secrets[n] = body
            return _Resp(201, body)
        if method == "PUT":
            self.secrets[path.rsplit("/", 1)[-1]] = body
            return _Resp(200, body)
        raise AssertionError(f"unexpected {method} {path}")

    def secret_writes(self):
        return [(m, p) for m, p, _, _ in self.calls if "/secrets" in p]

    @staticmethod
    def _matches(pod, sel):
        k, _, v = sel.partition("=")
        return (pod["metadata"].get("labels") or {}).get(k) == v

    def add(
        self,
        uid,
        *,
        phase="Running",
        ip="10.42.0.7",
        fp="deadbeef",
        image="ghcr.io/x/pi:1",
        waiting=None,
        terminating=False,
    ):
        meta = {
            "name": pod_name(uid),
            "labels": {K8S_LABEL: "workspace"},
            "annotations": {K8S_ANN_USER: uid, K8S_ANN_BOOTCFG: fp},
        }
        if terminating:
            meta["deletionTimestamp"] = "2026-09-03T00:00:00Z"
        status = {"phase": phase, "podIP": ip}
        if waiting:
            status["containerStatuses"] = [{"name": "app", "state": {"waiting": waiting}}]
        self.pods[pod_name(uid)] = {
            "metadata": meta,
            "spec": {"containers": [{"image": image}]},
            "status": status,
        }

    def created(self):
        return [b for m, p, b, _ in self.calls if m == "POST" and "/pods" in p]

    def deleted(self):
        return [p.rsplit("/", 1)[-1] for m, p, _, _ in self.calls if m == "DELETE"]


@pytest.fixture()
def k8s(monkeypatch):
    monkeypatch.setattr(config, "K8S_API_URL", "https://<TUNNEL_NODE_IP>:6443")
    monkeypatch.setattr(config, "K8S_TOKEN", "tok")
    monkeypatch.setattr(config, "K8S_TOKEN_FILE", "")
    monkeypatch.setattr(config, "K8S_NAMESPACE", "dsh")
    monkeypatch.setattr(config, "K8S_DATA_PVC", "dshwork-data")
    monkeypatch.setattr(config, "K8S_IMAGE_PULL_SECRET", "ghcr")
    monkeypatch.setattr(config, "WORK_REGISTRY_SERVER", "")
    monkeypatch.setattr(config, "WORK_REGISTRY_USERNAME", "")
    monkeypatch.setattr(config, "WORK_REGISTRY_PASSWORD", "")
    monkeypatch.setattr(config, "WORK_CPUS", 0.5)
    monkeypatch.setattr(config, "WORK_MEM_LIMIT_MB", 1024)
    b = K8sBackend()
    fake = FakeK8s()
    fake.secrets = {}
    monkeypatch.setattr(b, "_api", fake.call)
    return b, fake


@pytest.fixture()
def registry(monkeypatch):
    monkeypatch.setattr(config, "WORK_REGISTRY_SERVER", "ghcr.io")
    monkeypatch.setattr(config, "WORK_REGISTRY_USERNAME", "bot")
    monkeypatch.setattr(config, "WORK_REGISTRY_PASSWORD", "p4ss")


async def _create(b, uid="u_abc~pi", **kw):
    args = dict(
        boot="echo hi",
        env={"A": "1"},
        boot_fp="fp1",
        image="pi:1",
        image_ref="ghcr.io/x/pi:1",
        mem_mb=2048,
        cpus=2.0,
    )
    args.update(kw)
    await b.create(uid, **args)


# --- 名字与身份 ---------------------------------------------------------------


def test_pod_name_is_dns_safe_lowercase_of_cname():
    assert pod_name("u_AbC~pi") == "dshwork-uabcpi"
    assert pod_name("u_AbC~pi") == cname("u_AbC~pi").lower()


@pytest.mark.asyncio
async def test_identity_goes_into_annotations_not_labels(k8s):
    """工作台键里有 "~", label 值装不下 —— 必须在注解里, 且 label 只是个固定的选择器。"""
    b, fake = k8s
    await _create(b, "u_abc~pi")
    (body,) = fake.created()
    assert body["metadata"]["annotations"] == {K8S_ANN_USER: "u_abc~pi", K8S_ANN_BOOTCFG: "fp1"}
    assert body["metadata"]["labels"] == {K8S_LABEL: "workspace"}
    assert body["metadata"]["namespace"] == "dsh"


# --- create: 规格 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_script_env_and_image_reach_the_app_container(k8s):
    b, fake = k8s
    await _create(b, env={"DSH_GATEWAY_KEY": "t", "N": 3})
    app = fake.created()[0]["spec"]["containers"][0]
    assert app["image"] == "ghcr.io/x/pi:1"
    assert app["command"] == ["sh", "-c", "echo hi"]
    assert app["workingDir"] == "/workspace"
    # k8s 的 env value 必须是字符串; 传个 int 进去 API 会 400
    assert {"name": "N", "value": "3"} in app["env"]
    assert {"name": "DSH_GATEWAY_KEY", "value": "t"} in app["env"]


@pytest.mark.asyncio
async def test_resources_are_pod_level_with_quarter_cpu_reservation(k8s):
    """组给总量 (与 ECI 容器组同义); CPU 只按上限 1/4 预留, 内存足额预留。"""
    b, fake = k8s
    await _create(b, cpus=2.0, mem_mb=2048)
    res = fake.created()[0]["spec"]["resources"]
    assert res["limits"] == {"cpu": "2000m", "memory": "2048Mi"}
    assert res["requests"] == {"cpu": "500m", "memory": "2048Mi"}
    # 容器自己不带资源 —— 否则 Pod 级的总量对它不生效
    assert "resources" not in fake.created()[0]["spec"]["containers"][0]


@pytest.mark.asyncio
async def test_zero_spec_falls_back_to_the_global_defaults(k8s):
    b, fake = k8s
    await _create(b, cpus=0.0, mem_mb=0)
    res = fake.created()[0]["spec"]["resources"]
    assert res["limits"] == {"cpu": "500m", "memory": "1024Mi"}
    assert res["requests"]["cpu"] == "125m"


@pytest.mark.asyncio
async def test_home_and_workspace_live_on_the_pvc_under_per_user_subpaths(k8s):
    b, fake = k8s
    await _create(b, "u_abc~pi")
    body = fake.created()[0]
    assert {"name": "dshwork-data", "persistentVolumeClaim": {"claimName": "dshwork-data"}} in body["spec"][
        "volumes"
    ]
    mounts = body["spec"]["containers"][0]["volumeMounts"]
    assert {"name": "dshwork-data", "mountPath": "/root", "subPath": "uabcpi/home"} in mounts
    assert {"name": "dshwork-data", "mountPath": "/workspace", "subPath": "uabcpi/workspace"} in mounts


@pytest.mark.asyncio
async def test_two_users_never_share_a_subpath(k8s):
    b, fake = k8s
    await _create(b, "u_one~pi")
    await _create(b, "u_two~pi")
    subs = [m["subPath"] for body in fake.created() for m in body["spec"]["containers"][0]["volumeMounts"]]
    assert len(subs) == len(set(subs)) == 4


@pytest.mark.asyncio
async def test_without_a_pvc_the_workspace_is_ephemeral_and_says_so_once(k8s, monkeypatch, caplog):
    b, fake = k8s
    monkeypatch.setattr(config, "K8S_DATA_PVC", "")
    with caplog.at_level("WARNING"):
        await _create(b, "u_a~pi")
        await _create(b, "u_b~pi")
    vols = fake.created()[0]["spec"]["volumes"]
    assert {"name": "dshwork-data", "emptyDir": {}} in vols
    assert sum("一次性" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_private_images_are_pulled_with_the_configured_secret(k8s, monkeypatch):
    b, fake = k8s
    await _create(b)
    assert fake.created()[0]["spec"]["imagePullSecrets"] == [{"name": "ghcr"}]
    monkeypatch.setattr(config, "K8S_IMAGE_PULL_SECRET", "")
    await _create(b, "u_other~pi")
    assert "imagePullSecrets" not in fake.created()[1]["spec"]


@pytest.mark.asyncio
async def test_pull_secret_is_written_from_the_registry_credential_once(k8s, registry):
    """凭据与 ECI 同源, dhc-server 自己写进命名空间; 每个进程只写一次。"""
    b, fake = k8s
    await _create(b, "u_a~pi")
    await _create(b, "u_b~pi")
    assert fake.secret_writes() == [("POST", "/api/v1/namespaces/dsh/secrets")]
    sec = fake.secrets["ghcr"]
    assert sec["type"] == "kubernetes.io/dockerconfigjson"
    import base64

    cfg = json.loads(base64.b64decode(sec["data"][".dockerconfigjson"]))
    assert cfg["auths"]["ghcr.io"]["username"] == "bot"
    assert cfg["auths"]["ghcr.io"]["password"] == "p4ss"
    assert base64.b64decode(cfg["auths"]["ghcr.io"]["auth"]) == b"bot:p4ss"
    # Secret 先于 Pod 写 —— 顺序反了第一台 Pod 会拉不到镜像
    order = [m + " " + p.rsplit("/", 1)[-1] for m, p, _, _ in fake.calls if m == "POST"]
    assert order[:2] == ["POST secrets", "POST pods"]


@pytest.mark.asyncio
async def test_an_existing_pull_secret_is_replaced_not_left_stale(k8s, registry):
    b, fake = k8s
    fake.secrets["ghcr"] = {"old": True}
    await _create(b)
    assert fake.secret_writes() == [
        ("POST", "/api/v1/namespaces/dsh/secrets"),
        ("PUT", "/api/v1/namespaces/dsh/secrets/ghcr"),
    ]
    assert "old" not in fake.secrets["ghcr"]


@pytest.mark.asyncio
async def test_without_a_registry_credential_nothing_is_written(k8s):
    b, fake = k8s
    await _create(b)
    assert fake.secret_writes() == []
    assert fake.created()[0]["spec"]["imagePullSecrets"] == [{"name": "ghcr"}]  # 假定别人建好了


@pytest.mark.asyncio
async def test_a_failed_secret_write_does_not_block_the_pod(k8s, registry, caplog):
    b, fake = k8s
    fake.secret_status = 500
    with caplog.at_level("WARNING"):
        await _create(b)
    assert len(fake.created()) == 1
    assert any("拉取凭据" in r.message and "失败" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_the_agent_gets_no_cluster_credentials(k8s):
    """工作台里跑的是用户的智能体: 不给它 ServiceAccount token, 也不注入服务变量。"""
    b, fake = k8s
    await _create(b)
    spec = fake.created()[0]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["securityContext"] == {"seccompProfile": {"type": "RuntimeDefault"}}


@pytest.mark.asyncio
async def test_single_container_products_keep_restart_never(k8s):
    b, fake = k8s
    await _create(b)
    assert fake.created()[0]["spec"]["restartPolicy"] == "Never"
    assert fake.created()[0]["spec"]["terminationGracePeriodSeconds"] == 5


@pytest.mark.asyncio
async def test_run_as_user_lands_on_the_app_container(k8s):
    b, fake = k8s
    await _create(b, run_as_user=0)
    assert fake.created()[0]["spec"]["containers"][0]["securityContext"] == {"runAsUser": 0}
    await _create(b, "u_x~pi", run_as_user=None)
    assert "securityContext" not in fake.created()[1]["spec"]["containers"][0]


@pytest.mark.asyncio
async def test_stack_product_becomes_one_pod_with_sidecar_containers(k8s):
    b, fake = k8s
    sc = (
        products.Sidecar(
            name="postgres",
            image_ref="postgres:15",
            env=(("POSTGRES_PASSWORD", "s"),),
            mounts=(("pg", "/var/lib/postgresql/data"),),
            run_as_user=0,
        ),
        products.Sidecar(
            name="web", image_ref="x/web:1", args=("--port", "3000"), seeds=(("nginx", "/etc/nginx"),)
        ),
    )
    await _create(b, "u_abc~dify", sidecars=sc, host_aliases=("api", "web"))
    spec = fake.created()[0]["spec"]
    assert spec["restartPolicy"] == "Always"
    names = [c["name"] for c in spec["containers"]]
    assert names == ["app", "postgres", "web"]
    pg = spec["containers"][1]
    assert pg["env"] == [{"name": "POSTGRES_PASSWORD", "value": "s"}]
    assert pg["securityContext"] == {"runAsUser": 0}
    assert pg["volumeMounts"] == [
        {"name": "dshwork-data", "mountPath": "/var/lib/postgresql/data", "subPath": "uabcdify/pg"}
    ]
    web = spec["containers"][2]
    assert web["args"] == ["--port", "3000"]
    assert "command" not in web  # args 不顶 entrypoint
    assert web["volumeMounts"] == [{"name": "dshwork-seed", "mountPath": "/etc/nginx", "subPath": "nginx"}]
    assert spec["hostAliases"] == [{"ip": "127.0.0.1", "hostnames": ["api", "web"]}]
    # 有人要种子卷, 就得有那个 emptyDir
    assert {"name": "dshwork-seed", "emptyDir": {}} in spec["volumes"]


@pytest.mark.asyncio
async def test_host_aliases_with_underscores_are_dropped_not_fatal(k8s, caplog):
    """k8s 的 hostAliases 只收 RFC 1123 名; 带下划线的整个 Pod 会被 422 拒掉 (Dify 2026-09-03)。"""
    b, fake = k8s
    with caplog.at_level("WARNING"):
        await _create(b, "u_abc~dify", host_aliases=("api", "api_websocket", "db_postgres", "web"))
    assert fake.created()[0]["spec"]["hostAliases"] == [{"ip": "127.0.0.1", "hostnames": ["api", "web"]}]
    assert any("api_websocket" in r.message and "db_postgres" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_all_underscore_aliases_means_no_host_aliases_block(k8s):
    b, fake = k8s
    await _create(b, host_aliases=("db_postgres",))
    assert "hostAliases" not in fake.created()[0]["spec"]


def test_every_product_manifest_carries_only_valid_hostnames(monkeypatch):
    """每个产品的栈定义都过一遍: 别名要么合法要么被过滤, 不许有漏网的进 manifest。"""
    monkeypatch.setattr(config, "K8S_DATA_PVC", "dshwork-data")
    b = K8sBackend()
    for prod in products.registry().values():
        m = b._manifest(
            f"u_x~{prod.id}",
            boot="true",
            env={},
            boot_fp="fp",
            image=prod.image or "img",
            image_ref=prod.image_ref,
            mem_mb=prod.mem_mb,
            cpus=prod.cpus,
            sidecars=prod.sidecars,
            host_aliases=prod.host_aliases,
            init_containers=prod.init_containers,
            seeds=prod.seeds,
            run_as_user=prod.run_as_user,
        )
        for ha in m["spec"].get("hostAliases", []):
            for h in ha["hostnames"]:
                assert workbackend._RFC1123.fullmatch(h), (prod.id, h)
        # 容器名也是 DNS label
        for c in m["spec"]["containers"] + m["spec"].get("initContainers", []):
            assert workbackend._RFC1123.fullmatch(c["name"]), (prod.id, c["name"])


@pytest.mark.asyncio
async def test_init_container_seeds_a_shared_volume_for_the_whole_pod(k8s):
    b, fake = k8s
    ic = (
        products.InitContainer(
            name="seed", image_ref="x/assets:1", cmd=("cp", "-r", "/a/.", "/seed/"), mounts=(("cfg", "/out"),)
        ),
    )
    await _create(
        b, "u_abc~coze", init_containers=ic, seeds=(("", "/seed"), ("sql", "/docker-entrypoint-initdb.d"))
    )
    spec = fake.created()[0]["spec"]
    (init,) = spec["initContainers"]
    assert init["command"] == ["cp", "-r", "/a/.", "/seed/"]
    assert init["volumeMounts"] == [
        {"name": "dshwork-seed", "mountPath": "/seed"},
        {"name": "dshwork-data", "mountPath": "/out", "subPath": "uabccoze/cfg"},
    ]
    app_mounts = spec["containers"][0]["volumeMounts"]
    assert {"name": "dshwork-seed", "mountPath": "/seed"} in app_mounts
    assert {
        "name": "dshwork-seed",
        "mountPath": "/docker-entrypoint-initdb.d",
        "subPath": "sql",
    } in app_mounts
    assert {"name": "dshwork-seed", "emptyDir": {}} in spec["volumes"]


@pytest.mark.asyncio
async def test_no_seed_volume_when_nobody_asks_for_one(k8s):
    b, fake = k8s
    await _create(b)
    assert [v["name"] for v in fake.created()[0]["spec"]["volumes"]] == ["dshwork-data"]


# --- create: 撞名与配额 -------------------------------------------------------


@pytest.mark.asyncio
async def test_name_clash_waits_for_the_old_pod_to_vanish_then_creates(k8s, monkeypatch):
    """撞名不是成功 —— 当成功的话用户会连到正在死的那台。"""
    b, fake = k8s
    monkeypatch.setattr(workbackend.asyncio, "sleep", _nosleep)
    fake.add("u_abc~pi")
    fake.linger_gets = 3
    await b.destroy("u_abc~pi")
    await _create(b, "u_abc~pi")
    assert len(fake.created()) == 2  # 第一次 409, 等它消失后再建
    assert pod_name("u_abc~pi") in fake.pods


@pytest.mark.asyncio
async def test_quota_exhausted_is_reported_as_capacity_not_as_an_error(k8s):
    b, fake = k8s
    fake.quota_full = True
    with pytest.raises(RuntimeError, match="^capacity$"):
        await _create(b)


@pytest.mark.asyncio
async def test_other_api_failures_raise_with_the_status(k8s, monkeypatch):
    b, fake = k8s

    async def boom(method, path, **kw):
        return _Resp(500, {"message": "etcd down"})

    monkeypatch.setattr(b, "_api", boom)
    with pytest.raises(workbackend.K8sError, match="500"):
        await _create(b)


async def _nosleep(_):
    return None


# --- inspect ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_pod_reports_its_ip_as_the_host(k8s):
    b, fake = k8s
    fake.add("u_abc~pi", phase="Running", ip="10.42.0.9", fp="fp1", image="ghcr.io/x/pi:1")
    info = await b.inspect("u_abc~pi")
    assert info == WorkInfo(
        running=True, boot_fp="fp1", image_id="ghcr.io/x/pi:1", host="10.42.0.9", state="Running"
    )


@pytest.mark.asyncio
async def test_pending_pod_exists_but_is_not_running(k8s):
    b, fake = k8s
    fake.add("u_abc~pi", phase="Pending", ip="")
    info = await b.inspect("u_abc~pi")
    assert info is not None and not info.running and info.state == "Pending"
    assert fake.deleted() == []


@pytest.mark.asyncio
async def test_terminal_pod_is_cleaned_up_and_reported_gone(k8s):
    for phase in ("Failed", "Succeeded", "Unknown"):
        b, fake = k8s
        fake.add("u_abc~pi", phase=phase)
        assert await b.inspect("u_abc~pi") is None
        assert fake.deleted() == [pod_name("u_abc~pi")]
        fake.calls.clear()


@pytest.mark.asyncio
async def test_terminating_pod_is_reported_as_present_but_not_running(k8s):
    """正在删的 Pod 名字还占着 —— 报 None 会让上层立刻重建然后 409。"""
    b, fake = k8s
    fake.add("u_abc~pi", phase="Running", terminating=True)
    info = await b.inspect("u_abc~pi")
    assert info is not None and not info.running and info.state == "Terminating" and info.host == ""
    assert fake.deleted() == []


@pytest.mark.asyncio
async def test_absent_pod_is_none(k8s):
    b, _ = k8s
    assert await b.inspect("u_nobody~pi") is None


@pytest.mark.asyncio
async def test_a_stuck_image_pull_is_reported_once(k8s, caplog):
    b, fake = k8s
    fake.add(
        "u_abc~pi", phase="Pending", waiting={"reason": "ImagePullBackOff", "message": "pull access denied"}
    )
    with caplog.at_level("ERROR"):
        await b.inspect("u_abc~pi")
        await b.inspect("u_abc~pi")
    errs = [r for r in caplog.records if "ImagePullBackOff" in r.message]
    assert len(errs) == 1 and "pull access denied" in errs[0].message


@pytest.mark.asyncio
async def test_a_healthy_pending_says_nothing(k8s, caplog):
    b, fake = k8s
    fake.add("u_abc~pi", phase="Pending", waiting={"reason": "ContainerCreating"})
    with caplog.at_level("ERROR"):
        await b.inspect("u_abc~pi")
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


# --- release / destroy / running_users ---------------------------------------


@pytest.mark.asyncio
async def test_release_deletes_because_a_pod_cannot_be_stopped(k8s):
    b, fake = k8s
    fake.add("u_abc~pi")
    await b.release("u_abc~pi")
    assert fake.deleted() == [pod_name("u_abc~pi")]
    assert not b.resumable


@pytest.mark.asyncio
async def test_destroying_something_absent_is_not_an_error(k8s):
    b, fake = k8s
    await b.destroy("u_nobody~pi")
    assert fake.deleted() == [pod_name("u_nobody~pi")]


@pytest.mark.asyncio
async def test_running_users_come_from_annotations_of_running_pods_only(k8s):
    b, fake = k8s
    fake.add("u_run~pi", phase="Running")
    fake.add("u_pend~pi", phase="Pending")
    fake.add("u_dying~pi", phase="Running", terminating=True)
    fake.add("u_dead~pi", phase="Failed")
    # 别人的 Pod (没有我们的 label) 不算
    fake.pods["someone-else"] = {
        "metadata": {"name": "someone-else", "annotations": {K8S_ANN_USER: "u_x~pi"}},
        "status": {"phase": "Running"},
    }
    assert await b.running_users() == ["u_run~pi"]
    (_, _, _, params) = [c for c in fake.calls if c[0] == "GET"][0]
    assert params == {"labelSelector": "dshwork=workspace"}


@pytest.mark.asyncio
async def test_running_users_follows_the_continue_token(k8s, monkeypatch):
    b, fake = k8s
    pages = [
        _Resp(200, {"items": [fake_pod("u_a~pi")], "metadata": {"continue": "tok"}}),
        _Resp(200, {"items": [fake_pod("u_b~pi")], "metadata": {}}),
    ]
    seen = []

    async def call(method, path, *, json_body=None, params=None):
        seen.append(params)
        return pages.pop(0)

    monkeypatch.setattr(b, "_api", call)
    assert await b.running_users() == ["u_a~pi", "u_b~pi"]
    assert seen[1]["continue"] == "tok"


def fake_pod(uid):
    return {
        "metadata": {"name": pod_name(uid), "annotations": {K8S_ANN_USER: uid}},
        "status": {"phase": "Running"},
    }


@pytest.mark.asyncio
async def test_image_identity_is_the_reference_itself(k8s):
    b, _ = k8s
    assert await b.current_image_id("ghcr.io/x/pi:1") == "ghcr.io/x/pi:1"


def test_token_can_come_from_a_file(monkeypatch, tmp_path):
    f = tmp_path / "token"
    f.write_text("  secret-token\n")
    monkeypatch.setattr(config, "K8S_TOKEN_FILE", str(f))
    monkeypatch.setattr(config, "K8S_TOKEN", "")
    assert K8sBackend()._token() == "secret-token"


# --- OSS 同步: 正本在 OSS, 本地盘只是工作盘 ------------------------------------


@pytest.fixture()
def oss(monkeypatch):
    monkeypatch.setattr(config, "K8S_SYNC_OSS_BUCKET", "dshcloud-work")
    monkeypatch.setattr(config, "K8S_SYNC_OSS_PREFIX", "dshwork")
    monkeypatch.setattr(config, "K8S_SYNC_OSS_ENDPOINT", "oss-ap-southeast-1-internal.aliyuncs.com")
    monkeypatch.setattr(config, "K8S_SYNC_OSS_ACCESS_KEY_ID", "AKID")
    monkeypatch.setattr(config, "K8S_SYNC_OSS_ACCESS_KEY_SECRET", "SK")
    monkeypatch.setattr(config, "K8S_SYNC_SECRET", "dshwork-oss")
    monkeypatch.setattr(config, "K8S_SYNC_IMAGE", "rclone/rclone:1.75")
    monkeypatch.setattr(config, "K8S_SYNC_INTERVAL_S", 300)
    monkeypatch.setattr(config, "K8S_SYNC_GRACE_S", 180)


@pytest.mark.asyncio
async def test_without_oss_config_no_sync_containers_and_short_grace(k8s, monkeypatch):
    monkeypatch.setattr(config, "K8S_SYNC_OSS_BUCKET", "")
    b, fake = k8s
    await _create(b)
    spec = fake.created()[0]["spec"]
    assert "initContainers" not in spec
    assert spec["terminationGracePeriodSeconds"] == 5
    assert fake.secret_writes() == []


@pytest.mark.asyncio
async def test_sync_adds_restore_then_syncer_before_product_inits(k8s, oss):
    """恢复容器排在所有初始化容器之前 (产品的初始化容器可能往用户目录写东西),
    同步器紧随其后 (原生 sidecar), 免得先推一份空的上去。"""
    b, fake = k8s
    ic = (products.InitContainer(name="seed", image_ref="x/assets:1", cmd=("cp", "-r", "/a/.", "/seed/")),)
    await _create(b, "u_abc~coze", init_containers=ic, seeds=(("", "/seed"),))
    spec = fake.created()[0]["spec"]
    names = [c["name"] for c in spec["initContainers"]]
    assert names == ["dsh-restore", "dsh-syncer", "seed"]
    restore, syncer = spec["initContainers"][:2]
    assert "restartPolicy" not in restore
    assert syncer["restartPolicy"] == "Always", "同步器必须是原生 sidecar: 应用退出之后才收 TERM"
    assert spec["terminationGracePeriodSeconds"] == 180, "全量推送要时间, 5 秒会被杀在半路"


@pytest.mark.asyncio
async def test_sync_containers_mount_the_users_whole_tree_and_only_they_get_the_key(k8s, oss):
    b, fake = k8s
    await _create(b, "u_abc~pi")
    spec = fake.created()[0]["spec"]
    for c in spec["initContainers"]:
        assert c["image"] == "rclone/rclone:1.75"
        assert c["volumeMounts"] == [{"name": "dshwork-data", "mountPath": "/data", "subPath": "uabcpi"}]
        assert c["envFrom"] == [{"secretRef": {"name": "dshwork-oss"}}]
        assert {"name": "DSH_HEXID", "value": "uabcpi"} in c["env"]
    # 用户的智能体跑在 app 容器里 —— 它拿不到 OSS 密钥, 否则能读别人的目录
    app = spec["containers"][0]
    assert "envFrom" not in app
    assert not any(e["name"].startswith("RCLONE") for e in app["env"])


@pytest.mark.asyncio
async def test_sync_secret_holds_rclone_config_and_is_written_once(k8s, oss):
    import base64

    b, fake = k8s
    await _create(b, "u_a~pi")
    await _create(b, "u_b~pi")
    writes = [p for _, p in fake.secret_writes()]
    assert writes == ["/api/v1/namespaces/dsh/secrets"], "每个进程只写一次"
    sec = fake.secrets["dshwork-oss"]
    kv = {k: base64.b64decode(v).decode() for k, v in sec["data"].items()}
    assert kv["RCLONE_CONFIG_OSS_TYPE"] == "s3" and kv["RCLONE_CONFIG_OSS_PROVIDER"] == "Alibaba"
    assert kv["RCLONE_CONFIG_OSS_ENDPOINT"] == "oss-ap-southeast-1-internal.aliyuncs.com"
    assert (
        kv["RCLONE_CONFIG_OSS_ACCESS_KEY_ID"] == "AKID" and kv["RCLONE_CONFIG_OSS_SECRET_ACCESS_KEY"] == "SK"
    )
    assert kv["DSH_SYNC_REMOTE"] == "oss:dshcloud-work/dshwork"
    # 只限桶内的密钥查不了桶, 不跳过 rclone 会去建桶然后 409, 一个文件都传不上去
    assert kv["RCLONE_CONFIG_OSS_NO_CHECK_BUCKET"] == "true"
    # 密钥不进 Pod 清单
    assert "SK" not in json.dumps(fake.created()[0])


@pytest.mark.asyncio
async def test_sync_scripts_guard_against_pushing_a_half_restore(k8s, oss):
    """拉失败不落标记, 同步器见不到标记就不推 —— 半份数据推上去会把正本弄坏。"""
    b, fake = k8s
    await _create(b)
    restore, syncer = fake.created()[0]["spec"]["initContainers"][:2]
    r = restore["command"][2]
    s_ = syncer["command"][2]
    assert "rm -f /data/.dsh-restored" in r and ": > /data/.dsh-restored" in r
    assert r.index("rclone sync") < r.index(": > /data/.dsh-restored"), "标记要在拉成功之后"
    assert "local is authoritative" in r, "OSS 上没有这个用户时本地就是正本, 也要落标记"
    assert "[ -e /data/.dsh-restored ] || continue" in s_
    assert "trap final TERM" in s_ and 'rclone sync /data "$R"' in s_, "TERM 时全量推"
    for flags in (
        "--metadata",
        "--links",
        "--s3-directory-markers",
        "--create-empty-src-dirs",
        "--exclude '/.dsh-*'",
    ):
        assert flags in r and flags in s_, flags


# --- 按产品分派 ---------------------------------------------------------------


class _Stub(workbackend.Backend):
    resumable = True

    def __init__(self, tag, users=()):
        self.tag = tag
        self.users = list(users)
        self.seen = []

    async def inspect(self, user_id):
        self.seen.append(("inspect", user_id))
        return None

    async def current_image_id(self, image):
        return f"{self.tag}:{image}"

    async def create(self, user_id, **kw):
        self.seen.append(("create", user_id))

    async def start(self, user_id):
        self.seen.append(("start", user_id))

    async def release(self, user_id):
        self.seen.append(("release", user_id))

    async def destroy(self, user_id):
        self.seen.append(("destroy", user_id))

    async def running_users(self):
        return list(self.users)

    def capacity_reason(self):
        return f"cap:{self.tag}"


@pytest.mark.asyncio
async def test_routed_backend_sends_each_product_to_its_own_backend():
    eci, k8s_ = _Stub("eci"), _Stub("k8s")
    r = RoutedBackend(eci, {"pi": k8s_})
    await r.create("u_a~pi", boot="", env={}, boot_fp="", image="")
    await r.inspect("u_a~pi")
    await r.start("u_a~comfyui")  # 默认产品/没派出去的产品走默认后端
    assert k8s_.seen == [("create", "u_a~pi"), ("inspect", "u_a~pi")]
    assert eci.seen == [("start", "u_a~comfyui")]


@pytest.mark.asyncio
async def test_routed_reclaim_reaches_every_backend():
    """把产品从 ECI 切到 k8s 的那一刻, ECI 上还有它的实例在跑: 回收器数得到它
    (running_users 是并集), 但只删 k8s 那边就等于删了个不存在的 Pod —— ECI 那台
    按秒计费到天荒地老。所以 release/destroy 问遍所有后端, 该产品的那个排最前。"""
    eci, k8s_ = _Stub("eci"), _Stub("k8s")
    r = RoutedBackend(eci, {"pi": k8s_, "openmanus": k8s_})
    await r.release("u_a~pi")
    await r.destroy("u_b")
    assert k8s_.seen == [("release", "u_a~pi"), ("destroy", "u_b")]
    assert eci.seen == [("release", "u_a~pi"), ("destroy", "u_b")]


@pytest.mark.asyncio
async def test_routed_running_users_is_the_union_metered_once_each():
    eci, k8s_ = _Stub("eci", ["u_a", "u_b~comfyui"]), _Stub("k8s", ["u_c~pi", "u_a"])
    r = RoutedBackend(eci, {"pi": k8s_, "openmanus": k8s_})
    assert await r.running_users() == ["u_a", "u_b~comfyui", "u_c~pi"]


@pytest.mark.asyncio
async def test_routed_image_identity_is_asked_of_the_products_own_backend():
    """问错后端 = 永远陈旧 = 每次访问都重建。"""
    eci, k8s_ = _Stub("eci"), _Stub("k8s")
    r = RoutedBackend(eci, {"pi": k8s_})
    assert await r.for_product("pi").current_image_id("img") == "k8s:img"
    assert await r.for_product("comfyui").current_image_id("img") == "eci:img"
    assert eci.for_product("pi") is eci  # 单后端: 自己就是答案


def test_boot_hint_is_honest_per_backend():
    """启动等待页那句话: k8s 节点上镜像在本地, 别再说 20–40 秒吓人。"""
    assert K8sBackend.boot_hint == "5–15 秒"
    assert workbackend.EciBackend.boot_hint == "20–40 秒"
    r = RoutedBackend(_Stub("eci"), {"pi": K8sBackend()})
    assert r.boot_hint == _Stub.boot_hint == "5–20 秒"  # 无键的问题按默认后端答


def test_routed_keyless_questions_go_to_the_default():
    eci, k8s_ = _Stub("eci"), _Stub("k8s")
    k8s_.resumable = False
    r = RoutedBackend(eci, {"pi": k8s_})
    assert r.capacity_reason() == "cap:eci"
    assert r.resumable is True


def test_make_backend_parses_the_product_map(monkeypatch):
    monkeypatch.setattr(config, "WORK_BACKEND", "eci")
    monkeypatch.setattr(config, "WORK_BACKEND_PRODUCTS", "pi=k8s, openmanus = K8S ,comfyui=eci")
    b = workbackend.make_backend()
    assert isinstance(b, RoutedBackend)
    assert isinstance(b.default, workbackend.EciBackend)
    assert isinstance(b.by_product["pi"], K8sBackend)
    assert b.by_product["openmanus"] is b.by_product["pi"]  # 同名后端只建一份
    assert b.by_product["comfyui"] is b.default


def test_make_backend_without_a_map_is_the_plain_backend(monkeypatch):
    monkeypatch.setattr(config, "WORK_BACKEND", "k8s")
    monkeypatch.setattr(config, "WORK_BACKEND_PRODUCTS", "")
    assert isinstance(workbackend.make_backend(), K8sBackend)


def test_make_backend_rejects_a_malformed_map(monkeypatch):
    monkeypatch.setattr(config, "WORK_BACKEND", "eci")
    monkeypatch.setattr(config, "WORK_BACKEND_PRODUCTS", "pi:k8s")
    with pytest.raises(ValueError, match="格式"):
        workbackend.make_backend()
    monkeypatch.setattr(config, "WORK_BACKEND_PRODUCTS", "pi=nomad")
    with pytest.raises(ValueError, match="未知"):
        workbackend.make_backend()


@pytest.mark.asyncio
async def test_a_broken_backend_does_not_stop_the_others_from_being_metered(caplog):
    """k8s 节点掉线时 ECI 上的实例还在烧钱 —— 那边的计量与回收必须照常。"""

    class _Broken(_Stub):
        async def running_users(self):
            raise RuntimeError("PermissionError: ca.crt")

    eci, k8s_ = _Stub("eci", ["u_a", "u_b~comfyui"]), _Broken("k8s")
    r = RoutedBackend(eci, {"pi": k8s_})
    with caplog.at_level("ERROR"):
        assert await r.running_users() == ["u_a", "u_b~comfyui"]
    assert any("_Broken.running_users 失败" in rec.message for rec in caplog.records)
