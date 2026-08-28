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
IMAGE_MODEL = "gpt-image-2"
# 官方公开价: 图像输出 $30/百万 token, 文本输入 $5/百万。
PRICED_IMAGES = {
    IMAGE_MODEL: {
        "id": IMAGE_MODEL,
        "name": "GPT Image 2",
        "usd_per_1m_image_tokens": 30.0,
        "usd_per_1m_text_input_tokens": 5.0,
        "fallback_credits": 25,
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
    monkeypatch.setattr(media, "_image_cache", PRICED_IMAGES)
    # 灰度闸单独由 test_gate_* 覆盖; 其余用例测的是计费逻辑, 先放行。
    monkeypatch.setattr(config, "MEDIA_ADMIN_ONLY", False)


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


# --- 灰度闸 -------------------------------------------------------------------
# 价格是手填且未经账单核对的。开闸前若让全量用户可用, 等于按错误的价格真金白银
# 地卖, 那种错只能靠对账收场 —— 所以默认关着这件事本身需要被钉死。


def test_gate_blocks_non_admin_by_default(monkeypatch):
    monkeypatch.setattr(config, "MEDIA_ADMIN_ONLY", True)
    _new_user("vid-plebe@test.local")
    log = []
    r = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_gate"}), log)
    assert r.status_code == 403, r.text
    assert log == [], "被闸挡住就不该打上游"


def test_gate_lets_admin_through(monkeypatch):
    monkeypatch.setattr(config, "MEDIA_ADMIN_ONLY", True)
    email = "vid-boss@test.local"
    monkeypatch.setattr(config, "ADMIN_EMAILS", [email])
    _new_user(email)
    r = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_admin"}))
    assert r.status_code == 200, r.text


# --- 图像 ---------------------------------------------------------------------
# 同步出图, 不进 video_jobs。计费口径与视频一致 (按件), 因为图像模型不在
# models.json 目录里, charge_credits 会按"最贵条目"兜底 —— 不漏计费但价格离谱。


def _gen_image(monkeypatch, response, log=None):
    monkeypatch.setattr(media, "_client", lambda: _FakeClient(post=response, log=log))
    return client.post(
        "/llm/v1/images/generations",
        json={"model": IMAGE_MODEL, "prompt": "一个红色的圆", "n": 1},
    )


def test_image_charges_by_token_not_per_item(monkeypatch):
    """按张收会要么坑用户要么亏钱: 同一模型低画质与高画质相差 35 倍
    (gpt-image-2 一张 1024²: $0.006 vs $0.211)。token 数如实反映画质与尺寸。"""
    monkeypatch.setattr(config, "MODEL_PRICE_MARKUP", 1.2)
    user = _new_user("img-ok@test.local")
    before = credits.balance(user["id"])
    # 生产实测的那一次: 低画质 1024², output_tokens=196
    ok = _Resp(
        200,
        {
            "created": 1,
            "data": [{"b64_json": "AAAA"}],
            "usage": {"output_tokens": 196, "input_tokens_details": {"text_tokens": 12}},
        },
    )
    r = _gen_image(monkeypatch, ok)
    assert r.status_code == 200, r.text
    # 196 x $30/1M + 12 x $5/1M = $0.00594 -> x100 积分 x1.2 倍率 = 0.71 -> 进位 1
    assert r.json()["credits"] == 1, r.json()
    assert credits.balance(user["id"]) == before - 1


def test_high_quality_image_costs_much_more(monkeypatch):
    """同一个模型, 高画质 token 数是低画质的几十倍 —— 按张收就永远收错。"""
    monkeypatch.setattr(config, "MODEL_PRICE_MARKUP", 1.2)
    entry = PRICED_IMAGES[IMAGE_MODEL]
    low = media.image_credits(entry, {"output_tokens": 196})
    high = media.image_credits(entry, {"output_tokens": 7000})
    assert low == 1 and high == 26, (low, high)
    assert high > low * 20, "高低画质的价差必须传导到用户账单上"


def test_image_without_usage_falls_back_but_never_free(monkeypatch):
    """上游没给 usage 时宁可贵也不能免费 —— 免费的那条路会被人发现并刷爆。"""
    assert media.image_credits(PRICED_IMAGES[IMAGE_MODEL], None) == 25
    assert media.image_credits(PRICED_IMAGES[IMAGE_MODEL], {}) == 25
    assert media.image_credits({}, None) >= 1, "连 fallback 都没配也不能免费"


def test_image_upstream_error_costs_nothing(monkeypatch):
    user = _new_user("img-fail@test.local")
    before = credits.balance(user["id"])
    r = _gen_image(monkeypatch, _Resp(400, {"error": {"message": "bad prompt"}}))
    assert r.status_code == 400
    assert credits.balance(user["id"]) == before, "上游报错的路径上一分不收"


def test_image_unpriced_model_is_not_offered(monkeypatch):
    monkeypatch.setattr(media, "_image_cache", {IMAGE_MODEL: {"usd_per_1m_image_tokens": None}})
    _new_user("img-unpriced@test.local")
    log = []
    r = _gen_image(monkeypatch, _Resp(200, {"data": []}), log)
    assert r.status_code == 404
    assert log == []


def test_resolutions_are_ordered_cheapest_first(monkeypatch):
    """下拉默认选中第一项。字典序会把 1080p 排到 480p 前面 —— 那意味着谁第一次
    点运行都是最贵的那档, 而他根本没做这个选择。"""
    monkeypatch.setattr(
        media,
        "_prices_cache",
        {
            "m": {"id": "m", "name": "M", "credits_per_second": {"720p": 20, "1080p": 40, "480p": 10}},
        },
    )
    monkeypatch.setattr(media, "_image_cache", {})
    assert media.offered()["video"][0]["resolutions"] == ["480p", "720p", "1080p"]
