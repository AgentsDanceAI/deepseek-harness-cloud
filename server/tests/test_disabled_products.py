"""整格下架 (WORK_DISABLED_PRODUCTS) 的守卫。

下架和加锁是两件事, 混了就出静默错误:
  加锁 (WORK_LOCKED_PRODUCTS)  卡还在, 买了通行证就能开
  下架 (WORK_DISABLED_PRODUCTS) 卡都不出, 谁都开不了

这里钉三条, 每条都是"漏了不会报错、只是行为不对":
  · enabled() 要把下架的排除掉 —— 漏了它照样能被路由到, 用户点进去是转圈
  · 目录要**不出卡**, 而不是出一张灰的 —— 灰卡照样占位置, 那不叫下架
  · 下架不改 registry() —— 已经跑着的工作台还要能被 inspect/release, 定义得留着
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-disabled-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import apps_catalog, config, products


def test_disabled_ids_parses_env(monkeypatch):
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", " coze , dify ,, ")
    assert products.disabled_ids() == {"coze", "dify"}
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "")
    assert products.disabled_ids() == set()


def test_enabled_excludes_disabled(monkeypatch):
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "")
    before = {p.id for p in products.enabled()}
    victim = next(iter(before), None)
    if victim is None:                      # 这个环境一个产品都没配, 没什么可测的
        return
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", victim)
    after = {p.id for p in products.enabled()}
    assert victim not in after
    assert after == before - {victim}       # 只少这一个, 别的一个都没连带掉


def test_disabled_still_in_registry(monkeypatch):
    """下架只影响"能不能新开", 不能把定义抽走 —— 已经跑着的那台还要被回收和计量。"""
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "coze")
    assert "coze" in products.registry()


def test_catalog_drops_the_card_entirely(monkeypatch):
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "")
    all_ids = {e["id"] for e in apps_catalog.entries_with_status(set())}
    assert "coze" in all_ids, "目录里本来该有 coze, 不然这条测试什么也没验到"

    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "coze")
    entries = apps_catalog.entries_with_status(set())
    ids = {e["id"] for e in entries}
    # 不是"live=False 的灰卡", 是根本没有这一项
    assert "coze" not in ids
    assert ids == all_ids - {"coze"}


def test_catalog_sorting_survives_a_disabled_card(monkeypatch):
    """排序用的 order 表也要跟着去掉那一项, 否则按时长排序时 KeyError。"""
    monkeypatch.setattr(config, "WORK_DISABLED_PRODUCTS", "coze")
    entries = apps_catalog.entries_with_status(set(), minutes={"dify": 10})
    assert "coze" not in {e["id"] for e in entries}
