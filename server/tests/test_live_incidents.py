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


class ReportEndToEnd(unittest.TestCase):
    """真的走一遍 HTTP —— 登录、白名单、限流、落库。

    创始人要去实测了 (2026-09-10), 单元测试证明不了"字段名两边对得上"这类事。
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from app import config
        from app.main import app
        from tests._signup import signup

        # _enabled() 看的是 LIVE_GPU_URL, 测试环境默认空 —— 不打开的话所有请求
        # 都是 404 live_disabled。这里只借它开闸, 不会真去连上游: 这几条用例走的
        # 是 /report 和 /incidents, 它们只读写本地库。
        cls._real_url = config.LIVE_GPU_URL
        config.LIVE_GPU_URL = "http://gpu.test/live"
        cls.c = TestClient(app)
        signup(cls.c, "incident-probe@example.com")
        cls.room = config.LIVE_ROOM

    @classmethod
    def tearDownClass(cls):
        from app import config

        config.LIVE_GPU_URL = cls._real_url

    def _rows(self, kind):
        from app import db

        return db.query(
            "SELECT kind, side, secs, lag, detail FROM live_incidents "
            "WHERE room=? AND kind=? ORDER BY created DESC",
            (self.room, kind),
        )

    def test_观众报一条卡顿能落库_且字段对得上(self):
        r = self.c.post("/api/live/report", json={
            "kind": "waiting", "secs": 2.5, "lag": 11.8, "detail": "缓冲见底 2.5 秒",
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("ok"), r.text)
        rows = self._rows("waiting")
        self.assertTrue(rows, "上报成功却没落库")
        self.assertEqual(rows[0]["side"], "viewer")
        self.assertAlmostEqual(rows[0]["secs"], 2.5, places=3)
        self.assertAlmostEqual(rows[0]["lag"], 11.8, places=3)

    def test_同一种事件会被冷却挡住_卡住时是连着来的(self):
        first = self.c.post("/api/live/report", json={"kind": "stall", "secs": 6})
        again = self.c.post("/api/live/report", json={"kind": "stall", "secs": 6})
        self.assertTrue(first.json().get("ok"), first.text)
        self.assertFalse(again.json().get("ok"),
                         "同一种事件连着两条都收了 —— 卡住时会刷屏")
        self.assertEqual(len(self._rows("stall")), 1)

    def test_编造的种类不落库(self):
        r = self.c.post("/api/live/report", json={"kind": "whatever"})
        self.assertEqual(r.status_code, 200, "不该报错, 静静丢掉就行")
        self.assertFalse(r.json().get("ok"))
        self.assertEqual(self._rows("whatever"), [])

    def test_产出侧的种类不接受客户端声称(self):
        """否则谁都能往表里塞「产出掉了」, 这张表就不能用来定位问题。"""
        for kind in ("slow", "recovered"):
            r = self.c.post("/api/live/report", json={"kind": kind, "detail": "假的"})
            self.assertFalse(r.json().get("ok"), f"{kind} 被客户端写进去了")
            self.assertEqual(self._rows(kind), [])

    def test_离谱的数字要被丢掉_不能污染统计(self):
        self.c.post("/api/live/report", json={
            "kind": "fatal", "secs": -5, "lag": 10 ** 9, "detail": "x" * 500,
        })
        rows = self._rows("fatal")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["secs"], 0.0, "负数被收了")
        self.assertEqual(rows[0]["lag"], 0.0, "离谱的 lag 被收了")
        self.assertLessEqual(len(rows[0]["detail"]), 200, "detail 没截断")

    def test_读取口不回user_id(self):
        """要的是「卡了多少次」, 不是「谁卡了」。"""
        r = self.c.get("/api/live/incidents?hours=1")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertIn("tally", d)
        self.assertTrue(d["items"], "刚写进去的事件读不出来")
        self.assertNotIn("user_id", d["items"][0])

    def test_没登录不给写也不给读(self):
        from fastapi.testclient import TestClient

        from app.main import app

        anon = TestClient(app)
        self.assertEqual(anon.post("/api/live/report", json={"kind": "stall"}).status_code, 401)
        self.assertEqual(anon.get("/api/live/incidents").status_code, 401)

if __name__ == "__main__":
    unittest.main()
