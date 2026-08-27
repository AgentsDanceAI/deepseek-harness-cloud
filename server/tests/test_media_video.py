"""视频生成: 预扣、透传、幂等退款。

这条路和聊天不同 —— 钱在**作业提交成功那一刻**就扣掉, 因为上游一旦开跑我们
就撤不回来, 而用户完全可以提交完就关页面。所以这里钉死的是钱的三条路径:

  提交失败 -> 一分不扣        (参数被上游拒、余额不够)
  提交成功 -> 立刻扣          (不等生成完成)
  生成失败 -> 退且只退一次    (轮询是客户端驱动的, 会重复查同一个失败作业)
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-media-")
os.environ.update(
    {
        "DHC_DEV": "1",
        "AUTH_SECRET": "test-secret",
        "DHC_DATA_DIR": _TMP,
        "DB_PATH": os.path.join(_TMP, "test.db"),
        "UPSTREAM_API_KEY": "sk-upstream-test",
        "FREE_SIGNUP_CREDITS": "500",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, credits, db, media  # noqa: E402
from app.main import app  # noqa: E402

from ._signup import signup  # noqa: E402

client = TestClient(app)

MODEL = "doubao-seedance-2-0-mini-260615"
PRICED = {
    MODEL: {
        "id": MODEL,
        "name": "Seedance 2.0 Mini",
        "credits_per_second": {"480p": 10, "720p": 20},
    }
}


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeClient:
    """顶替 media._client()。记录调用次数, 好断言"没扣费的路径也没打上游"。"""

    def __init__(self, post=None, get=None, log=None):
        self._post, self._get, self._log = post, get, log if log is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, headers=None, json=None):  # noqa: A002
        self._log.append(("POST", url))
        return self._post

    async def get(self, url, headers=None):
        self._log.append(("GET", url))
        return self._get


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "sk-upstream-test")
    monkeypatch.setattr(config, "UPSTREAM_BASE_URL", "https://api.qianmian.ai/v1")
    monkeypatch.setattr(media, "_prices_cache", PRICED)


def _new_user(email):
    signup(client, email)
    return client.get("/api/auth/me").json()["user"]


def _submit(monkeypatch, response, log=None):
    monkeypatch.setattr(media, "_client", lambda: _FakeClient(post=response, log=log))
    return client.post(
        "/llm/v1/videos/generations",
        json={"model": MODEL, "prompt": "一只猫", "duration": 5, "resolution": "480p"},
    )


def test_unpriced_model_is_not_offered(monkeypatch):
    """默认状态: video_models.json 里价格全是 null, 于是一个都不卖。"""
    monkeypatch.setattr(media, "_prices_cache", {MODEL: {"credits_per_second": {"480p": None}}})
    _new_user("vid-unpriced@test.local")
    log = []
    r = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_x"}), log)
    assert r.status_code == 404, r.text
    assert log == [], "未定价就不该打上游"


def test_submit_charges_immediately_and_records_the_job(monkeypatch):
    user = _new_user("vid-ok@test.local")
    before = credits.balance(user["id"])
    r = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_abc"}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_status"] == "PROCESSING"
    assert body["credits"] == 50  # 10 积分/秒 × 5 秒

    # 钱现在就没了 —— 不等生成完成。
    assert credits.balance(user["id"]) == before - 50

    row = db.query_one("SELECT * FROM video_jobs WHERE id = ?", (body["id"],))
    assert row["upstream_task_id"] == "vtask_abc"
    assert row["status"] == "processing"
    assert row["credits"] == 50


def test_upstream_rejection_costs_nothing(monkeypatch):
    """duration 档位非法这类错误由上游判定, 原样转达, 且不扣费。"""
    user = _new_user("vid-reject@test.local")
    before = credits.balance(user["id"])
    detail = {"error": {"code": "InvalidParameter", "message": "duration not valid"}}
    r = _submit(monkeypatch, _Resp(400, detail))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "InvalidParameter"
    assert credits.balance(user["id"]) == before
    assert db.query("SELECT * FROM video_jobs WHERE user_id = ?", (user["id"],)) == []


def test_insufficient_balance_never_reaches_upstream(monkeypatch):
    user = _new_user("vid-broke@test.local")
    db.query("DELETE FROM credit_grants WHERE user_id = ?", (user["id"],))
    log = []
    r = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_never"}), log)
    assert r.status_code == 402, r.text
    assert log == [], "余额不足就不该让上游先跑起来"


def test_poll_processing_then_success(monkeypatch):
    _new_user("vid-poll@test.local")
    job_id = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_poll"})).json()["id"]

    monkeypatch.setattr(
        media,
        "_client",
        lambda: _FakeClient(get=_Resp(200, {"code": "success", "data": {"status": "processing"}})),
    )
    r = client.get(f"/llm/v1/videos/result/{job_id}")
    assert r.json()["task_status"] == "PROCESSING"

    monkeypatch.setattr(
        media,
        "_client",
        lambda: _FakeClient(
            get=_Resp(
                200,
                {
                    "code": "success",
                    "data": {"status": "succeeded", "url": "https://cdn.example/v.mp4", "format": "mp4"},
                },
            )
        ),
    )
    r = client.get(f"/llm/v1/videos/result/{job_id}")
    assert r.json()["task_status"] == "SUCCESS"
    assert r.json()["video_result"][0]["url"] == "https://cdn.example/v.mp4"

    # 终态后不再打上游 —— 落库了就该从库里读。
    log = []
    monkeypatch.setattr(media, "_client", lambda: _FakeClient(log=log))
    assert client.get(f"/llm/v1/videos/result/{job_id}").json()["task_status"] == "SUCCESS"
    assert log == []


def test_failure_refunds_exactly_once(monkeypatch):
    """轮询由客户端驱动, 同一个失败作业会被查很多次 —— 只能退一次钱。"""
    user = _new_user("vid-fail@test.local")
    before = credits.balance(user["id"])
    job_id = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_fail"})).json()["id"]
    assert credits.balance(user["id"]) == before - 50

    failed = _Resp(200, {"code": "success", "data": {"status": "failed", "error": "boom"}})
    monkeypatch.setattr(media, "_client", lambda: _FakeClient(get=failed))
    r = client.get(f"/llm/v1/videos/result/{job_id}")
    assert r.json()["task_status"] == "FAIL"
    assert credits.balance(user["id"]) == before, "失败该退回全额"

    # 再查五次, 余额不能再涨。
    for _ in range(5):
        client.get(f"/llm/v1/videos/result/{job_id}")
    assert credits.balance(user["id"]) == before, "退款必须幂等"


def test_another_users_job_is_invisible(monkeypatch):
    _new_user("vid-owner@test.local")
    job_id = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_priv"})).json()["id"]
    _new_user("vid-stranger@test.local")
    assert client.get(f"/llm/v1/videos/result/{job_id}").status_code == 404


def test_upstream_hiccup_keeps_the_job_alive(monkeypatch):
    """轮询时上游抖一下, 不该把作业打成终态 (那会连带触发退款)。"""
    user = _new_user("vid-hiccup@test.local")
    job_id = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_hiccup"})).json()["id"]
    after_submit = credits.balance(user["id"])

    class _Boom(_FakeClient):
        async def get(self, url, headers=None):
            raise __import__("httpx").ConnectError("upstream down")

    monkeypatch.setattr(media, "_client", lambda: _Boom())
    r = client.get(f"/llm/v1/videos/result/{job_id}")
    assert r.json()["task_status"] == "PROCESSING"
    assert credits.balance(user["id"]) == after_submit, "抖动不该触发退款"
    row = db.query_one("SELECT * FROM video_jobs WHERE id = ?", (job_id,))
    assert row["status"] == "processing"


def test_concurrent_pollers_refund_only_once(monkeypatch):
    """两个轮询同时把同一个作业判成失败, 只能退一次钱。

    上面那个顺序用例抓不到这个: 第一次轮询把行标成 failed 之后, 后续请求走的是
    "终态直接读库"的提前返回, 压根不会再走退款。真正的风险是两个请求**都**在
    行还是 processing 时读到它、又都拿到上游的 failed —— 于是各自捧着一份
    refunded=0 的旧快照去退款。这里直接用同一份旧快照调两次来复现。
    """
    user = _new_user("vid-race@test.local")
    before = credits.balance(user["id"])
    job_id = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_race"})).json()["id"]
    stale = dict(db.query_one("SELECT * FROM video_jobs WHERE id = ?", (job_id,)))
    assert stale["refunded"] == 0

    media._refund_once(stale)
    media._refund_once(stale)  # 同一份旧快照, 模拟第二个并发请求

    assert credits.balance(user["id"]) == before, "并发退款必须只生效一次"
