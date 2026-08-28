"""视频生成: 预扣、透传、幂等退款。

这条路和聊天不同 —— 钱在**作业提交成功那一刻**就扣掉, 因为上游一旦开跑我们
就撤不回来, 而用户完全可以提交完就关页面。所以这里钉死的是钱的三条路径:

  提交失败 -> 一分不扣        (参数被上游拒、余额不够)
  提交成功 -> 立刻扣          (不等生成完成)
  生成失败 -> 退且只退一次    (轮询是客户端驱动的, 会重复查同一个失败作业)
"""

import os
import tempfile
import time

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

    async def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        self._log.append(("POST", url))
        return self._post

    async def get(self, url, headers=None, timeout=None):
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


# --- 定价校准守护 --------------------------------------------------------------


def _media_config() -> dict:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "config" / "media_models.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_only_calibrated_media_models_carry_a_price():
    """定价表是按**某一个型号的网关牌价**算出来的, 不能顺手套给别的型号。

    AgentsDance 2026-08-27 的事故就是这一条: 按 Seedance 2.5 重定价后, 默认模型
    没跟着换, 用户按 2.5 的价拿 2.0 的成品, 7 天无人发现 (2.5 牌价 ¥70/M,
    2.0 是 ¥23/M —— 3 倍差)。我们卖 6 个视频模型, 共用一张表就是那件事的放大版。

    所以: 有价 <=> 标了 calibrated。要开新型号, 先拿它的牌价重算一遍再标。
    """
    for m in _media_config()["video"]:
        priced = any((m.get("credits_per_second") or {}).values())
        calibrated = bool(m.get("calibrated"))
        assert priced == calibrated, (
            f"{m['id']}: 定价({priced}) 与 calibrated({calibrated}) 不一致 —— 要么补校准, 要么把价格置 null"
        )
    # 图像同理。这条守卫原来只看视频, 而"拿甲型号的价卖乙型号"在图像上一样会犯 ——
    # qwen-image-3.0 的 2K 是 4 积分、-pro 的 2K 是 9, 套错就是按 pro 的价卖标准版。
    for m in _media_config()["image"]:
        priced = media._image_priced(m)
        calibrated = bool(m.get("calibrated"))
        assert priced == calibrated, (
            f"{m['id']}: 定价({priced}) 与 calibrated({calibrated}) 不一致 —— 要么补校准, 要么把价格置 null"
        )


def test_video_prices_match_agentsdance():
    """与 AgentsDance 的 _VIDEO_USD_PER_SEC 对齐 (老板 2026-08-27 要求「一致就好」)。

    那边是售价 USD/秒, 这边是积分/秒, $1 = 100 积分。任何一边单方面改价都会让
    同一个模型在两个产品里卖不同的钱 —— 这条用例让它当场红。
    """
    # AgentsDance backend/dataset/agent_entitlements.py::_VIDEO_USD_PER_SEC
    # 那张表是**按 Seedance 2.5 的网关牌价校准**的 (VIDEO_PRICING_CALIBRATED_FOR),
    # 所以只有 2.5 该逐档相等。别的型号牌价不同, 必须各自写明推导依据。
    agentsdance_usd = {"480p": 0.12, "720p": 0.26, "1080p": 0.60}
    cfg = _media_config()["video"]
    anchor = [m for m in cfg if m["id"] == "doubao-seedance-2-5-260628"]
    assert anchor, "校准锚点型号不在目录里"
    for res, usd in agentsdance_usd.items():
        assert anchor[0]["credits_per_second"][res] == round(usd * 100), (
            f"2.5 {res}: {anchor[0]['credits_per_second'][res]} != AgentsDance ${usd}/秒 x100"
        )
    # 其余校准过的型号: 价格可以不同, 但必须说得出出处 —— 否则就是拍脑袋,
    # 而拍脑袋的价格上次让我们一边多收 25 倍、一边亏一半。
    for m in cfg:
        if not m.get("calibrated") or m["id"] == "doubao-seedance-2-5-260628":
            continue
        assert m.get("_derivation"), f"{m['id']} 标了 calibrated 却没有 _derivation —— 价格从哪来的?"


def test_the_calibrated_model_is_the_one_we_actually_sell():
    """校准的型号必须真的在网关目录里。AgentsDance 那次事故的另一半就是
    「定价指向的型号」与「实际跑的型号」脱钩。"""
    cfg = _media_config()
    calibrated = [m["id"] for m in cfg["video"] if m.get("calibrated")]
    assert "doubao-seedance-2-5-260628" in calibrated, (
        "价目表是按 Seedance 2.5 的牌价算的 —— 换校准对象必须同步改这张表"
    )


# --- 服务端兜底 ---------------------------------------------------------------
# 作业的生命周期**不能只靠客户端轮询驱动**。浏览器一关、ComfyUI 一报错、网络一抖,
# 作业就永远停在 processing —— 而钱是提交时就扣掉的。
#
# 2026-08-27 实测事故: 两条 1080p 作业卡住, 各扣 600 积分。一条上游明明
# succeeded, 另一条上游 failed (内容审核), 13 小时无人退款 —— 只因为节点在轮询时
# 撞上一次 502 (我正在重新部署) 就放弃了。


def _reconcile(monkeypatch, upstream_payload, status=200):
    monkeypatch.setattr(media, "_client", lambda: _FakeClient(get=_Resp(status, upstream_payload)))
    import asyncio

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(media.reconcile_tick())


def test_abandoned_job_that_succeeded_upstream_gets_settled(monkeypatch):
    """客户端走了, 但视频其实出来了 —— 兜底循环要把它记上, 否则用户付了钱
    却在界面上永远看不到结果。"""
    _new_user("recon-ok@test.local")
    job_id = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_recon1"})).json()["id"]
    assert dict(db.query_one("SELECT * FROM video_jobs WHERE id=?", (job_id,)))["status"] == "processing"

    n = _reconcile(
        monkeypatch, {"code": "success", "data": {"status": "succeeded", "url": "https://cdn.example/ok.mp4"}}
    )
    assert n >= 1
    row = dict(db.query_one("SELECT * FROM video_jobs WHERE id=?", (job_id,)))
    assert row["status"] == "succeeded" and row["url"] == "https://cdn.example/ok.mp4"


def test_abandoned_job_that_failed_upstream_gets_refunded(monkeypatch):
    """内容审核拒了、客户端也走了 —— 没有兜底就是白扣钱。实测那次卡了 13 小时。"""
    user = _new_user("recon-fail@test.local")
    before = credits.balance(user["id"])
    job_id = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_recon2"})).json()["id"]
    assert credits.balance(user["id"]) == before - 50

    _reconcile(
        monkeypatch,
        {
            "code": "success",
            "data": {"status": "failed", "error": "output video may contain sensitive information"},
        },
    )
    row = dict(db.query_one("SELECT * FROM video_jobs WHERE id=?", (job_id,)))
    assert row["status"] == "failed"
    assert "sensitive" in row["error"]
    assert credits.balance(user["id"]) == before, "失败必须退全款"


def test_a_job_upstream_forgot_is_failed_and_refunded(monkeypatch):
    """上游偶尔把作业丢掉 —— 既不 succeeded 也不 failed, 就是不动。
    不设上限那笔钱永远悬着。"""
    user = _new_user("recon-lost@test.local")
    before = credits.balance(user["id"])
    job_id = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_lost"})).json()["id"]
    # 把创建时间推回到超过上限
    monkeypatch.setattr(config, "VIDEO_JOB_MAX_AGE_S", 1800)
    db.query("UPDATE video_jobs SET created = ? WHERE id = ?", (time.time() - 3600, job_id))

    _reconcile(monkeypatch, {"code": "success", "data": {"status": "processing"}})
    row = dict(db.query_one("SELECT * FROM video_jobs WHERE id=?", (job_id,)))
    assert row["status"] == "failed", "超时的作业必须落终态"
    assert credits.balance(user["id"]) == before, "超时也要退款"


def test_upstream_hiccup_during_reconcile_leaves_the_job_alone(monkeypatch):
    """上游抖一下不能把作业判死 —— 那会白退钱, 而视频可能马上就好。"""
    user = _new_user("recon-hiccup@test.local")
    job_id = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_hiccup"})).json()["id"]
    after_submit = credits.balance(user["id"])

    class _Boom(_FakeClient):
        async def get(self, url, headers=None):
            raise __import__("httpx").ConnectError("upstream down")

    monkeypatch.setattr(media, "_client", lambda: _Boom())
    import asyncio

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(media.reconcile_tick())
    row = dict(db.query_one("SELECT * FROM video_jobs WHERE id=?", (job_id,)))
    assert row["status"] == "processing"
    assert credits.balance(user["id"]) == after_submit


def test_reconcile_does_not_double_refund(monkeypatch):
    """兜底循环每分钟跑一次, 同一个失败作业会被看到很多次。"""
    user = _new_user("recon-once@test.local")
    before = credits.balance(user["id"])
    _submit(monkeypatch, _Resp(200, {"task_id": "vtask_once"}))
    failed = {"code": "success", "data": {"status": "failed", "error": "boom"}}
    for _ in range(5):
        _reconcile(monkeypatch, failed)
    assert credits.balance(user["id"]) == before, "退款必须幂等"


# --- 多上游 (千面 / 百炼) -------------------------------------------------------


def test_bailian_without_credentials_is_unavailable(monkeypatch):
    """没配专属域名就不可用 —— **绝不回落到公共 dashscope 域名**。

    公共域名一样能通、结果也一样, 但预付套餐不抵扣、走按量计费, 且没有任何报错
    提示 (AgentsDance 2026-08-12 踩过)。所以宁可不可用, 也不偷偷去打公共域名。
    """
    monkeypatch.setattr(config, "BAILIAN_NATIVE_BASE", "")
    monkeypatch.setattr(config, "BAILIAN_API_KEY", "k")
    assert media.provider_available(media.BAILIAN) is False
    monkeypatch.setattr(config, "BAILIAN_NATIVE_BASE", "https://x.example/api/v1")
    monkeypatch.setattr(config, "BAILIAN_API_KEY", "")
    assert media.provider_available(media.BAILIAN) is False
    # 两个都配齐才可用
    monkeypatch.setattr(config, "BAILIAN_API_KEY", "k")
    assert media.provider_available(media.BAILIAN) is True
    # 千面不受这个影响
    assert media.provider_available(media.QIANMIAN) is True


def test_unavailable_provider_models_are_not_offered(monkeypatch):
    """凭据没配的上游, 型号不该出现在下拉里 —— 露出来只会点了报错。"""
    monkeypatch.setattr(config, "BAILIAN_NATIVE_BASE", "")
    monkeypatch.setattr(
        media,
        "_prices_cache",
        {
            "q": {"id": "q", "name": "Q", "provider": "qianmian", "credits_per_second": {"480p": 10}},
            "b": {"id": "b", "name": "B", "provider": "bailian", "credits_per_second": {"480p": 10}},
        },
    )
    monkeypatch.setattr(media, "_image_cache", {})
    assert [m["id"] for m in media.offered()["video"]] == ["q"]


def test_job_remembers_which_upstream_placed_it(monkeypatch):
    """作业要记住是哪个上游下的单 —— 轮询时才知道问谁。

    不能靠 model 反查配置: 型号一旦从 media_models.json 里删掉, 还在跑的作业就
    永远收不了尾, 而钱已经扣了。
    """
    _new_user("prov@test.local")
    job_id = _submit(monkeypatch, _Resp(200, {"task_id": "vtask_prov"})).json()["id"]
    row = dict(db.query_one("SELECT * FROM video_jobs WHERE id = ?", (job_id,)))
    assert row["provider"] == "qianmian"


def test_bailian_resolution_and_status_words_are_translated():
    """两家的写法不一样, 直接透传必错。

    分辨率  千面 "480p"        百炼 "832*480" (宽*高)
    状态词  千面 SUCCESS/FAIL  百炼 SUCCEEDED/FAILED/CANCELED (大写)
    """
    assert media._BAILIAN_SIZE["480p"] == "832*480"
    assert media._BAILIAN_SIZE["1080p"] == "1920*1080"
    assert media._BAILIAN_TERMINAL["SUCCEEDED"] == "succeeded"
    assert media._BAILIAN_TERMINAL["FAILED"] == "failed"
    assert media._BAILIAN_TERMINAL["CANCELED"] == "failed", "取消也是没交付, 要退款"
    assert "RUNNING" not in media._BAILIAN_TERMINAL, "RUNNING 不是终态, 落进去会被当失败退款"


def test_bailian_images_price_per_item_not_per_token(monkeypatch):
    """百炼的同步生图**不返回 token 用量**, 只能按张。千面有 usage, 按 token 更准。"""
    per_item = {"id": "b", "provider": "bailian", "credits_per_image": 7}
    by_token = {"id": "q", "provider": "qianmian", "usd_per_1m_image_tokens": 30.0, "fallback_credits": 25}
    monkeypatch.setattr(config, "MODEL_PRICE_MARKUP", 1.2)
    assert media.image_credits(per_item, None) == 7
    assert media.image_credits(per_item, {"output_tokens": 9999}) == 7, "按张就不看 token"
    assert media.image_credits(by_token, {"output_tokens": 196}) == 1


def test_per_item_image_price_follows_the_size_tier(monkeypatch):
    """qwen-image-3.0-pro 的 1K 与 2K 差整整一倍 (¥0.25 / ¥0.5)。

    一口价必然坑一头: 按 2K 收则 1K 的人被多收一倍, 按 1K 收则 2K 单单亏本。
    分档判的是**面积**不是边长 —— 按边长会把 2560x800 这种宽幅错判成 2K。
    """
    entry = {"credits_per_image": {"1k": 4, "2k": 8}}
    assert media.image_credits(entry, None, "1328*1328") == 4      # 1.76M <= 2.25M
    assert media.image_credits(entry, None, "1024x1024") == 4      # x 与 * 都要认
    assert media.image_credits(entry, None, "2048*2048") == 8      # 4.19M
    assert media.image_credits(entry, None, "2560*800") == 4, "宽幅面积只有 2.05M, 是 1K 档"
    assert media.image_credits(entry, None, "") == 8, "认不出尺寸时按贵的算, 不能亏本"


def test_all_null_tiers_count_as_unpriced(monkeypatch):
    """{"1k": null, "2k": null} 是"还没定价", 不是"已定价"。

    直接判 dict 真假会把它当成在售 -> 露在下拉里 -> 点了按 1 积分卖出去。
    """
    assert not media._image_priced({"credits_per_image": {"1k": None, "2k": None}})
    assert media._image_priced({"credits_per_image": {"1k": None, "2k": 8}})
    assert not media._image_priced({"credits_per_image": None})
    assert media._image_priced({"usd_per_1m_image_tokens": 30.0})


def test_bailian_image_model_actually_reaches_the_bailian_adapter(monkeypatch):
    """按张计价的模型必须真的能出图。

    gen_image 里的百炼分支写完后**没人调用** —— create_image 那条路写死打千面,
    而且只认 usd_per_1m_image_tokens, 于是百炼那半边图像模型 (qwen-image-3.0
    这些) 全部 404, 在售清单里却列着。这条测试钉住整条路: 路由 -> provider ->
    百炼原生端点 -> 按张扣费。
    """
    monkeypatch.setattr(config, "BAILIAN_NATIVE_BASE", "https://ws-x.example.com/api/v1")
    monkeypatch.setattr(config, "BAILIAN_API_KEY", "sk-fake")
    monkeypatch.setattr(media, "_image_cache", {
        "qwen-image-3.0": {"id": "qwen-image-3.0", "provider": "bailian", "credits_per_image": 3},
    })
    user = _new_user("img-bailian@test.local")
    before = credits.balance(user["id"])
    log = []
    # 百炼的同步生图形状: output.choices[].message.content[].image, **没有 usage**
    monkeypatch.setattr(media, "_client", lambda: _FakeClient(post=_Resp(200, {
        "output": {"choices": [{"message": {"content": [{"image": "https://oss/x.png"}]}}]},
    }), log=log))
    r = client.post("/llm/v1/images/generations",
                    json={"model": "qwen-image-3.0", "prompt": "一只柴犬"})
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["url"] == "https://oss/x.png"
    assert r.json()["credits"] == 3, "百炼没有 token 用量, 只能按张"
    assert credits.balance(user["id"]) == before - 3
    assert log and "ws-x.example.com" in log[0][1], f"没打到百炼专属域名: {log}"
    assert "multimodal-generation" in log[0][1], f"没走百炼的原生生图端点: {log}"


def test_bailian_image_request_carries_the_size(monkeypatch):
    """尺寸不转达上去 = 按 2K 收钱、按默认尺寸出图, 两边都不报错。"""
    monkeypatch.setattr(config, "BAILIAN_NATIVE_BASE", "https://ws-x.example.com/api/v1")
    monkeypatch.setattr(config, "BAILIAN_API_KEY", "sk-fake")
    sent = {}

    class _Spy(_FakeClient):
        async def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
            sent.update(json or {})
            return await super().post(url, headers=headers, json=json, timeout=timeout)

    monkeypatch.setattr(media, "_client", lambda: _Spy(post=_Resp(200, {
        "output": {"choices": [{"message": {"content": [{"image": "https://oss/x.png"}]}}]}})))
    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        media.gen_image("bailian", "qwen-image-3.0-pro", "p", 1, {"size": "2048x2048"}))
    # 百炼写 "*", OpenAI 那套写 "x" —— 转达时要统一
    assert sent.get("parameters", {}).get("size") == "2048*2048", sent


def test_bailian_image_without_credentials_is_not_offered(monkeypatch):
    """凭据没配就不该在售 —— 露出来只会让人选了报 502。

    尤其不能回落到公共 dashscope 域名: 一样能通、结果一样, 但预付套餐不抵扣。
    """
    monkeypatch.setattr(config, "BAILIAN_NATIVE_BASE", "")
    monkeypatch.setattr(config, "BAILIAN_API_KEY", "")
    monkeypatch.setattr(media, "_image_cache", {
        "qwen-image-3.0": {"id": "qwen-image-3.0", "provider": "bailian", "credits_per_image": 3},
    })
    _new_user("img-bailian-nocred@test.local")
    log = []
    monkeypatch.setattr(media, "_client", lambda: _FakeClient(post=_Resp(200, {}), log=log))
    r = client.post("/llm/v1/images/generations",
                    json={"model": "qwen-image-3.0", "prompt": "x"})
    assert r.status_code == 404, r.text
    assert log == [], "凭据没配却还是打了上游"
    assert "qwen-image-3.0" not in [m["id"] for m in media.offered()["image"]]


def test_postgres_migrations_actually_run(monkeypatch):
    """迁移在 postgres 上必须真的执行 —— 2026-08-28 之前它一条都没跑成过。

    连接池配的是 row_factory=dict_row (行是字典), 而 pg_columns 写的是 r[0],
    KeyError 被 _apply_migrations 的 except 吞掉 —— **每条迁移都被静默跳过**。
    一直没被发现, 是因为迁移涉及的列同时也写在 CREATE TABLE 里: 新库建表时就带上
    了, 迁移路径从没真正跑成。加 video_jobs.provider 时才撞上 —— 那是第一个真
    需要迁移到既有表的列。

    这里用一个假的 dict_row 游标复现那次失败: 取列名必须按键名, 不能按下标。
    """
    import app.db as dbmod

    class _DictCursor:
        def fetchall(self):
            return [{"column_name": "id"}, {"column_name": "user_id"}]

    applied = []

    def columns_of(table):
        # 与生产同款: dict_row。按 r[0] 取会 KeyError。
        return {r["column_name"] for r in _DictCursor().fetchall()}

    monkeypatch.setattr(dbmod, "MIGRATIONS", [("some_table", "new_col", "TEXT")])
    dbmod._apply_migrations(lambda sql: applied.append(sql), columns_of)
    assert applied and "ADD COLUMN new_col TEXT" in applied[0], (
        "迁移没有执行 —— 列查询若按下标取字典就会静默跳过所有迁移"
    )
