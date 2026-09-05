"""阿里云 STS 签名与会话策略。签名错了线上只会看到 SignatureDoesNotMatch, 所以对着
文档里的样例向量钉死 (ECS DescribeRegions 例: AccessKeyId=testid, secret=testsecret)。"""

import json

import httpx
import pytest

from app import alists

_DOC_PARAMS = {
    "Action": "DescribeRegions",
    "Version": "2014-05-26",
    "Format": "XML",
    "SignatureMethod": "HMAC-SHA1",
    "SignatureVersion": "1.0",
    "SignatureNonce": "3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf",
    "Timestamp": "2016-02-23T12:46:24Z",
    "AccessKeyId": "testid",
}


def test_string_to_sign_matches_the_documented_example():
    assert alists.string_to_sign(_DOC_PARAMS) == (
        "GET&%2F&AccessKeyId%3Dtestid%26Action%3DDescribeRegions%26Format%3DXML"
        "%26SignatureMethod%3DHMAC-SHA1%26SignatureNonce%3D3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf"
        "%26SignatureVersion%3D1.0%26Timestamp%3D2016-02-23T12%253A46%253A24Z%26Version%3D2014-05-26"
    )


def test_signature_matches_the_documented_example():
    assert alists.sign_rpc(_DOC_PARAMS, "testsecret") == "OLeaidS1JvxuMvnyHOwuJ+uX5qY="


def test_percent_encode_follows_aliyun_rules():
    assert alists.percent_encode("a b*c~d") == "a%20b%2Ac~d"
    assert alists.percent_encode("2016-02-23T12:46:24Z") == "2016-02-23T12%3A46%3A24Z"


def test_pod_policy_is_confined_to_the_users_subtree():
    p = alists.pod_policy("dshcloud-work", "dshwork", "u1c2f5")
    objects, listing = p["Statement"]
    assert objects["Resource"] == [
        "acs:oss:*:*:dshcloud-work/dshwork/u1c2f5",
        "acs:oss:*:*:dshcloud-work/dshwork/u1c2f5/*",
    ]
    assert "oss:PutObject" in objects["Action"] and "oss:DeleteObject" in objects["Action"]
    # 列桶只许带自己的前缀 —— 否则能枚举别人的目录名
    assert listing["Resource"] == ["acs:oss:*:*:dshcloud-work"]
    assert listing["Condition"]["StringLike"]["oss:Prefix"] == ["dshwork/u1c2f5", "dshwork/u1c2f5/*"]
    assert not any("*:*:dshcloud-work/*" in r for s in p["Statement"] for r in s["Resource"]), (
        "不能放开整个桶"
    )


@pytest.mark.asyncio
async def test_assume_role_sends_signed_request_and_parses_credentials():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        q = dict(request.url.params)
        seen["q"] = q
        # 服务端按同样算法复算签名 —— 对不上就是我们编码错了
        sig = q.pop("Signature")
        assert alists.sign_rpc(q, "SECRET") == sig
        return httpx.Response(
            200,
            json={
                "Credentials": {
                    "AccessKeyId": "STS.abc",
                    "AccessKeySecret": "xyz",
                    "SecurityToken": "tok",
                    "Expiration": "2026-09-05T09:00:00Z",
                }
            },
        )

    out = await alists.assume_role(
        access_key_id="AKID",
        access_key_secret="SECRET",
        role_arn="acs:ram::123:role/dshwork-pod",
        session="dshwork-u1",
        policy=alists.pod_policy("b", "p", "u1"),
        duration_s=3600,
        transport=httpx.MockTransport(handler),
    )
    assert out["access_key_id"] == "STS.abc" and out["security_token"] == "tok"
    assert out["expires"] == 1788598800.0  # 2026-09-05T09:00:00Z
    q = seen["q"]
    assert q["Action"] == "AssumeRole" and q["RoleArn"] == "acs:ram::123:role/dshwork-pod"
    assert json.loads(q["Policy"])["Statement"][0]["Resource"][0] == "acs:oss:*:*:b/p/u1"
    assert seen["url"].startswith("https://sts.aliyuncs.com/")


@pytest.mark.asyncio
async def test_assume_role_errors_carry_the_aliyun_code():
    def handler(request):
        return httpx.Response(404, json={"Code": "EntityNotExist.Role", "Message": "The role not exists"})

    with pytest.raises(alists.StsError) as e:
        await alists.assume_role(
            access_key_id="a",
            access_key_secret="b",
            role_arn="acs:ram::1:role/x",
            session="s",
            transport=httpx.MockTransport(handler),
        )
    assert "EntityNotExist.Role" in str(e.value)
