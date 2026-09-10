"""直播出问题时要留下记录 —— 在这之前完全没有。

2026-09-10 创始人问"每一次卡顿你能捕捉到并记录下来吗", 答案是不能:
· 观众卡了几次没人知道 —— 播放器的卡死检测每秒一拍, 冻住 6 秒就自己跳一下, 但从不上报。
· 产出什么时候掉到实时以下也没人知道 —— 上游那个 `starved` 计数在产能不足时**恒为 0**,
  因为它只在我们主动踩刹车导致队列空时才加, 而产能不足时数字人自己就是瓶颈, 一刻不闲。
于是每次报障都只能现场架探针去量, 回头什么都查不到。

这一组测试盯住两件事: 产出速率的判定别误报/漏报, 上报口别被拿来乱写。
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-incident-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

import unittest  # noqa: E402

from app import live  # noqa: E402


class RateSampler(unittest.TestCase):
    """`_sample_rate` 只在**状态转换**时记一条, 且判定要经得起锯齿。"""

    def setUp(self):
        live._RATE["pts"] = []
        live._RATE["slow"] = False
        self.recorded: list[tuple] = []
        self._real = live._record
        live._record = lambda kind, side, **kw: self.recorded.append((kind, side, kw))

    def tearDown(self):
        live._record = self._real

    def feed(self, pairs):
        """按 (相对秒, 边缘视频秒数) 喂采样。"""
        import time as _t

        base = _t.time()
        for dt, edge in pairs:
            live._RATE["pts"].append((base + dt, float(edge)))
        # 最后一笔走真实入口, 让判定跑起来
        last_t, last_edge = pairs[-1]
        live._RATE["pts"].pop()
        real_time = live.time.time
        live.time.time = lambda: base + last_t  # type: ignore[assignment]
        try:
            live._sample_rate({"live": True, "edge": last_edge})
        finally:
            live.time.time = real_time  # type: ignore[assignment]

    def test_稳态一倍速不报(self):
        self.feed([(0, 0.0), (90, 90.0)])
        self.assertEqual(self.recorded, [], "产出正好实时却报了 slow")

    def test_掉到实时以下要报一条(self):
        self.feed([(0, 0.0), (90, 71.0)])  # 0.79x
        self.assertEqual(len(self.recorded), 1, f"没记下掉速: {self.recorded}")
        kind, side, kw = self.recorded[0]
        self.assertEqual((kind, side), ("slow", "server"))
        self.assertIn("0.78", kw["detail"] + " " + kw["detail"])

    def test_一直慢也只报一条_不是每次采样都写库(self):
        self.feed([(0, 0.0), (90, 71.0)])
        self.feed([(0, 0.0), (100, 79.0)])
        self.feed([(0, 0.0), (120, 95.0)])
        self.assertEqual(len(self.recorded), 1,
                         f"慢的期间反复写库, 表会被噪声填满: {self.recorded}")

    def test_恢复了要报_而且要有迟滞(self):
        self.feed([(0, 0.0), (90, 71.0)])          # slow
        self.feed([(0, 0.0), (90, 87.0)])          # 0.967x —— 在 0.95~0.99 之间
        self.assertEqual(len(self.recorded), 1,
                         "刚过 0.95 就报恢复 —— 会在边界反复报, 迟滞没起作用")
        self.feed([(0, 0.0), (90, 90.0)])          # 1.0x
        self.assertEqual(len(self.recorded), 2)
        self.assertEqual(self.recorded[1][0], "recovered")

    def test_窗太短不判定__锯齿会把稳态读成掉速(self):
        """节流让产出成锯齿(句内出片, 句间空 5~8 秒)。

        30 秒的窗如果正好停在空档里, 稳态 1.000x 会被读成 0.7x —— 2026-09-10 我
        真的被这个骗过一次, 拿短窗得出过相反的结论。
        """
        self.feed([(0, 0.0), (30, 21.0)])          # 跨度不够 _RATE_MIN_SPAN
        self.assertEqual(self.recorded, [], "窗不够长就下了结论")

    def test_换场要把采样清掉__否则边缘倒退会被当成掉速(self):
        live._RATE["pts"] = []
        import time as _t

        base = _t.time()
        live._RATE["pts"].append((base, 500.0))
        real_time = live.time.time
        live.time.time = lambda: base + 5  # type: ignore[assignment]
        try:
            live._sample_rate({"live": True, "edge": 1.0})   # 换场: 时间轴回到 0
        finally:
            live.time.time = real_time  # type: ignore[assignment]
        self.assertEqual(self.recorded, [], "换场被当成掉速报了一条")
        self.assertEqual(len(live._RATE["pts"]), 1, "换场后没把旧采样清掉")

    def test_上游给不出edge时不判定(self):
        live._sample_rate({"live": True, "edge": 0})
        live._sample_rate({"live": True})
        self.assertEqual(self.recorded, [])
        self.assertEqual(live._RATE["pts"], [], "没有 edge 却往窗里塞了采样")

    def test_停播清空采样(self):
        live._RATE["pts"] = [(1.0, 10.0)]
        live._sample_rate({"live": False})
        self.assertEqual(live._RATE["pts"], [])


class ReportGuard(unittest.TestCase):
    """上报口是个写库的入口, 别让它变成随便写的洞。"""

    def test_产出侧的两种不接受客户端声称(self):
        """slow/recovered 是这一层自己算出来的。

        客户端能声称的话, 谁都能往表里塞"产出掉了", 这张表就不能用来定位问题了。
        """
        for kind in ("slow", "recovered"):
            self.assertIn(kind, live._INCIDENT_KINDS)

    def test_观众侧的种类是白名单(self):
        for kind in ("stall", "waiting", "fatal", "rebuild", "autoplay", "remuted"):
            self.assertIn(kind, live._INCIDENT_KINDS)
        self.assertNotIn("whatever", live._INCIDENT_KINDS)


if __name__ == "__main__":
    unittest.main()
