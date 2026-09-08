"""调度偏好 (K8S_PREFERRED_NODES) 的守卫。

存在的理由: 节点成本不对等 —— 248 是公司白借的, 溢出节点是我们按量买的。k8s 默认打分
是 LeastAllocated (谁空谁得, 也就是摊开), 所以不加偏好的话新工作台会一半落到花钱那台
上, 哪怕白借的还很空。而这件事**不会有任何报错**, 只会体现在账单上。

同时钉住"必须是软偏好": 硬约束在首选节点装满时让 Pod 一直 Pending, 而 Pending 不抛
异常 —— 用户对着启动等待页转圈, 比多花点钱糟得多。
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-nodepref-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import config
from app.workbackend import K8sBackend


def test_no_preference_means_no_affinity(monkeypatch):
    """不配就什么都不加 —— 现有行为一个字节都不变。"""
    monkeypatch.setattr(config, "K8S_PREFERRED_NODES", "")
    assert K8sBackend._node_preference() == {}


def test_preference_is_soft_not_hard(monkeypatch):
    monkeypatch.setattr(config, "K8S_PREFERRED_NODES", "dsh-node-1")
    aff = K8sBackend._node_preference()["affinity"]["nodeAffinity"]
    assert "preferredDuringSchedulingIgnoredDuringExecution" in aff
    assert "requiredDuringSchedulingIgnoredDuringExecution" not in aff, (
        "硬约束在首选节点装满时会让 Pod 一直 Pending, 而 Pending 不抛异常"
    )
    rule = aff["preferredDuringSchedulingIgnoredDuringExecution"][0]
    assert rule["weight"] == 100
    expr = rule["preference"]["matchExpressions"][0]
    assert expr["key"] == "kubernetes.io/hostname" and expr["operator"] == "In"
    assert expr["values"] == ["dsh-node-1"]


def test_multiple_nodes_and_whitespace(monkeypatch):
    monkeypatch.setattr(config, "K8S_PREFERRED_NODES", " a , b ,, c ")
    vals = K8sBackend._node_preference()["affinity"]["nodeAffinity"][
        "preferredDuringSchedulingIgnoredDuringExecution"
    ][0]["preference"]["matchExpressions"][0]["values"]
    assert vals == ["a", "b", "c"]


def test_manifest_carries_the_preference(monkeypatch):
    """光有函数不算 —— 要真出现在发给 API 的清单里。"""
    monkeypatch.setattr(config, "K8S_PREFERRED_NODES", "dsh-node-1")
    b = K8sBackend()
    kw = dict(
        boot="echo hi",
        env={},
        boot_fp="fp",
        image="img",
        image_ref="",
        mem_mb=2048,
        cpus=1.0,
        sidecars=(),
        host_aliases=(),
        init_containers=(),
        seeds=(),
        run_as_user=None,
    )
    m = b._manifest("u1~pi", **kw)
    assert m["spec"]["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]

    monkeypatch.setattr(config, "K8S_PREFERRED_NODES", "")
    assert "affinity" not in b._manifest("u1~pi", **kw)["spec"]
