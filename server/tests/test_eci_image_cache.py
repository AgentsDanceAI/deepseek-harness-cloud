"""ECI 镜像缓存的镜像集合。

这里盯的是**只会变慢、不会报错**的一类错: 集合里漏一个镜像, 缓存照样报"命中",
冷启动却要额外去 registry 拉一次 —— 用户看到的是转圈, 日志里什么都没有。
2026-08-28 就这么让人「根本启动不了」过一次 (那次漏的是伴随容器)。
"""

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))
sys.path.insert(0, os.path.join(os.path.dirname(_here), "scripts"))

import eci_image_cache  # noqa: E402

from app import config  # noqa: E402


def test_ref_set_covers_init_containers_too(monkeypatch):
    """初始化容器的镜像必须进集合。

    它虽小, 但**每个常规容器都要等它跑完**才起 —— 漏了它, 缓存看着是整组命中,
    冷启动却仍要先拉一次, 而这段等待加在所有容器前面。
    """
    monkeypatch.setattr(config, "COZE_DOMAIN", "coze.test.local")
    monkeypatch.setattr(config, "COZE_ASSETS_IMAGE_REF", "ghcr.io/x/coze-assets:t")
    sets = dict(eci_image_cache._ref_sets())
    assert "coze" in sets, "启用了却没进集合 —— 这个产品的缓存永远建不出来"
    refs = sets["coze"]
    assert "ghcr.io/x/coze-assets:t" in refs
    # 主容器和伴随容器一个都不能少 (整组装不下 = 等于没缓存, 见 _covered)
    assert "cozedev/coze-studio-web:0.5.1" in refs
    assert "cozedev/coze-studio-server:0.5.1" in refs
    assert "milvusdb/milvus:v2.5.10" in refs
    # 同一个镜像用在两个容器上 (nsqlookupd/nsqd) 只该出现一次
    assert refs.count("nsqio/nsq:v1.2.1") == 1


def test_partial_cache_is_not_covered():
    """部分命中不算命中。

    缺谁谁就要全量拉, 而栈产品最大的往往正是伴随容器 (向量库/搜索引擎) ——
    把部分命中当命中, 冷启动就从二十几秒变成几分钟。
    """
    refs = ("a:1", "b:2", "c:3")
    assert eci_image_cache._covered(refs, [{"Status": "Ready", "Images": ["a:1", "b:2"]}]) is None
    full = {"Status": "Ready", "Images": ["a:1", "b:2", "c:3", "d:4"]}
    assert eci_image_cache._covered(refs, [full]) is full
    # 没 Ready 的缓存不能算数 —— 还在建的缓存对冷启动没有任何帮助
    assert eci_image_cache._covered(refs, [{"Status": "Creating", "Images": list(refs)}]) is None


def test_an_interrupted_build_leaves_no_ghost(monkeypatch):
    """建缓存被打断时, 半成品必须一起删掉。

    EIP 是在 finally 里无条件释放的 (漏一个会一直计费, 那句是对的)。但只释放不
    清理会留下**永远 Creating 的幽灵**: 缓存还在建、出网能力已被抽走, 阿里云侧
    不报错也不结束。后果不只是多一条垃圾记录 ——
      · check 从此把这个产品报成"没有整组 Ready 的缓存";
      · **连重试都会被挡住**: prepare 看见 Creating 就去等它, 于是每次重试都在等
        一个永远不会好的东西。
    2026-09-01 实测: prepare 被超时打断一次, 0.7.5 的缓存卡了二十分钟, 没有任何
    一行日志说出了什么事。
    """
    dropped = []
    calls = []

    def _fake_call(product, version, action, params):
        calls.append(action)
        if action == "AllocateEipAddress":
            return {"AllocationId": "eip-x", "EipAddress": "1.2.3.4"}
        if action == "CreateImageCache":
            return {"ImageCacheId": "imc-ghost"}
        return {}

    # 状态永远停在 Creating -> 模拟"EIP 被抽走后再也好不了"
    monkeypatch.setattr(eci_image_cache, "_call", _fake_call)
    monkeypatch.setattr(
        eci_image_cache, "_caches", lambda: [{"ImageCacheId": "imc-ghost", "Status": "Creating"}]
    )
    monkeypatch.setattr(eci_image_cache, "_drop", lambda cid: dropped.append(cid))
    # 让等待循环立刻走完, 别真等半小时
    monkeypatch.setattr(eci_image_cache.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(eci_image_cache.time, "time", _fake_clock(start=0.0, step=1000.0))

    try:
        eci_image_cache._build_one("ghcr.io/x/y:1", [])
    except RuntimeError:
        pass  # 超时抛错是对的, 这里验的是它有没有把半成品收走

    assert dropped == ["imc-ghost"], (
        f"半成品缓存没被删掉 (dropped={dropped}) —— 会留下永远 Creating 的幽灵, "
        "而且之后每次 prepare 都会去等它"
    )
    assert "ReleaseEipAddress" in calls, "EIP 还是要释放的 (漏了会一直计费)"


def _fake_clock(start: float, step: float):
    """每次调用往前跳一大步 —— 让 30 分钟的等待循环一两轮就走完。"""
    state = {"t": start}

    def _now():
        state["t"] += step
        return state["t"]

    return _now
