"""阿里云 STS AssumeRole —— 给每个工作台 Pod 铸一把**只能碰它自己那个目录**的临时 OSS 凭据。

为什么: 用户数据正本在 OSS 桶 dshcloud-work 里, 每个 Pod 的同步伴随容器都要一把钥匙。
原来是同一把桶级长期密钥塞进所有 Pod —— 谁从自己的工作台逃到伴随容器, 拿到的就是
全部用户的数据。换成 STS: 长期密钥只留在 dhc-server 上, Pod 里的是一小时一换、
policy 收窄到 `<prefix>/<hexid>/` 的临时凭据。

为什么不用 SDK: 只用一个接口, RPC 风格签名 30 行, 少一棵依赖树。签名算法对着
阿里云文档的样例向量测过 (见 tests/test_alists.py)。
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import quote

import httpx

STS_VERSION = "2015-04-01"
#: 对象级操作全给 (在自己的目录里); rclone 要 Head/Get/Put/Delete/Copy, 大文件要分片。
_OBJECT_ACTIONS = (
    "oss:GetObject",
    "oss:GetObjectMeta",
    "oss:HeadObject",
    "oss:PutObject",
    "oss:CopyObject",
    "oss:DeleteObject",
    "oss:AbortMultipartUpload",
    "oss:ListParts",
    "oss:ListMultipartUploads",
)


class StsError(RuntimeError):
    pass


def percent_encode(s: str) -> str:
    """阿里云 RPC 签名的编码: RFC 3986, 保留 -_.~; 空格是 %20 不是 +; * 是 %2A。"""
    return quote(str(s), safe="-_.~")


def string_to_sign(params: dict[str, str], method: str = "GET") -> str:
    canon = "&".join(f"{percent_encode(k)}={percent_encode(v)}" for k, v in sorted(params.items()))
    return f"{method}&{percent_encode('/')}&{percent_encode(canon)}"


def sign_rpc(params: dict[str, str], access_key_secret: str, method: str = "GET") -> str:
    digest = hmac.new(
        (access_key_secret + "&").encode(), string_to_sign(params, method).encode(), hashlib.sha1
    )
    return base64.b64encode(digest.digest()).decode()


def pod_policy(bucket: str, prefix: str, hexid: str) -> dict:
    """会话策略: 只许碰 `<bucket>/<prefix>/<hexid>` 这棵子树。生效权限 = 角色权限 ∩ 这个。"""
    root = f"acs:oss:*:*:{bucket}/{prefix}/{hexid}"
    return {
        "Version": "1",
        "Statement": [
            {"Effect": "Allow", "Action": list(_OBJECT_ACTIONS), "Resource": [root, root + "/*"]},
            {
                "Effect": "Allow",
                "Action": ["oss:ListObjects", "oss:ListObjectsV2"],
                "Resource": [f"acs:oss:*:*:{bucket}"],
                "Condition": {"StringLike": {"oss:Prefix": [f"{prefix}/{hexid}", f"{prefix}/{hexid}/*"]}},
            },
        ],
    }


def session_name(hexid: str) -> str:
    """RoleSessionName: 2-64 个 [A-Za-z0-9.@_-]。"""
    return f"dshwork-{hexid}"[:64]


async def assume_role(
    *,
    access_key_id: str,
    access_key_secret: str,
    role_arn: str,
    session: str,
    policy: dict | None = None,
    duration_s: int = 3600,
    endpoint: str = "sts.aliyuncs.com",
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """返回 {access_key_id, access_key_secret, security_token, expires(epoch)}; 失败抛 StsError。"""
    params: dict[str, str] = {
        "Action": "AssumeRole",
        "Version": STS_VERSION,
        "Format": "JSON",
        "AccessKeyId": access_key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "RoleArn": role_arn,
        "RoleSessionName": session,
        "DurationSeconds": str(int(duration_s)),
    }
    if policy:
        params["Policy"] = json.dumps(policy, separators=(",", ":"))
    params["Signature"] = sign_rpc(params, access_key_secret)
    try:
        async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
            r = await client.get(f"https://{endpoint}/", params=params)
    except httpx.HTTPError as e:
        raise StsError(f"sts unreachable: {e}") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    creds = body.get("Credentials") if isinstance(body, dict) else None
    if r.status_code != 200 or not creds:
        raise StsError(f"{r.status_code} {body.get('Code', '')}: {body.get('Message', r.text[:200])}")
    exp = str(creds.get("Expiration", ""))
    try:
        expires = float(calendar.timegm(time.strptime(exp, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        expires = time.time() + duration_s
    return {
        "access_key_id": creds["AccessKeyId"],
        "access_key_secret": creds["AccessKeySecret"],
        "security_token": creds["SecurityToken"],
        "expires": expires,
    }
