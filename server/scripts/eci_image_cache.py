"""ECI 镜像缓存: 查漂移 / 重建。

    python3 -m scripts.eci_image_cache check      # 缓存与 WORK_IMAGE_REF 对得上吗
    python3 -m scripts.eci_image_cache rebuild    # 按当前 WORK_IMAGE_REF 重建

为什么需要这个: 冷启动 25s 全靠命中镜像缓存, 未命中会退回 50s。而缓存是**按
镜像引用**建的 —— WORK_IMAGE_REF 一升到 rc9, 缓存还指着 rc8, 于是每个用户每次
都多等半分钟。这件事**不报错、不告警**, 只是慢, 所以没人会发现。

构建缓存的那个任务不共享业务实例的 EIP, 自己没有公网出口, 从公网仓库拉不到镜像
(实测: 一直 Preparing / 进度 0 / "start to pull images")。所以要临时申请一个 EIP
传给 CreateImageCache, 用完释放。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys
import time
import uuid
from urllib.parse import quote

import httpx

sys.path.insert(0, "/srv/dhc")
from app import config  # noqa: E402


def _pe(s) -> str:
    return quote(str(s), safe="~-._").replace("+", "%20").replace("*", "%2A")


def _call(product: str, version: str, action: str, params: dict | None = None) -> dict:
    """阿里云 RPC 调用。签名与 workbackend._sign 同一套 (已对着真实端点验过)。"""
    p = {
        "Action": action, "Version": version, "Format": "JSON",
        "AccessKeyId": config.ECI_ACCESS_KEY_ID,
        "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "RegionId": config.ECI_REGION_ID,
    }
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    canon = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in sorted(p.items()))
    sts = f"POST&{_pe('/')}&{_pe(canon)}"
    p["Signature"] = base64.b64encode(
        hmac.new((config.ECI_ACCESS_KEY_SECRET + "&").encode(), sts.encode(),
                 hashlib.sha1).digest()).decode()
    r = httpx.post(f"https://{product}.{config.ECI_REGION_ID}.aliyuncs.com/",
                   data=p, timeout=60.0)
    body = r.json()
    # 阿里云把错误也放在非 200 之外的地方, Code 字段才是权威的
    if r.status_code != 200 or body.get("Code"):
        raise RuntimeError(f"{action}: {body.get('Code') or r.status_code} "
                           f"{body.get('Message', r.text[:200])}")
    return body


def _caches() -> list[dict]:
    return _call("eci", "2018-08-08", "DescribeImageCaches").get("ImageCaches", [])


def _ref() -> str:
    ref = (config.WORK_IMAGE_REF or config.WORK_IMAGE).strip()
    if not ref:
        raise SystemExit("WORK_IMAGE_REF 为空 —— 没有可对照的镜像引用")
    return ref


def check() -> int:
    ref = _ref()
    ready = [c for c in _caches()
             if c.get("Status") == "Ready" and ref in (c.get("Images") or [])]
    if ready:
        print(f"✓ 镜像缓存与 WORK_IMAGE_REF 一致: {ref}")
        print(f"  {ready[0].get('ImageCacheId')}  ({ready[0].get('ImageCacheName')})")
        return 0
    print(f"✗ 没有对应 {ref} 的 Ready 镜像缓存。", file=sys.stderr)
    print("  后果不是报错, 是每个用户每次冷启动从 ~25s 退回 ~50s —— 只会慢, 不会响。",
          file=sys.stderr)
    for c in _caches():
        print(f"  现有: {c.get('ImageCacheId')} {c.get('Status')} {c.get('Images')}",
              file=sys.stderr)
    print("  修复: python3 -m scripts.eci_image_cache rebuild", file=sys.stderr)
    return 1


def rebuild() -> int:
    ref = _ref()
    stale = [c["ImageCacheId"] for c in _caches()
             if ref not in (c.get("Images") or [])
             or c.get("Status") not in ("Ready", "Creating", "Preparing")]
    print(f"==> 目标镜像: {ref}")

    print("==> 申请临时 EIP (构建任务不共享业务实例的 EIP, 自己没有出网能力)")
    eip = _call("vpc", "2016-04-28", "AllocateEipAddress",
                {"Bandwidth": 100, "InternetChargeType": "PayByTraffic",
                 "Name": "dsh-imagecache-build"})
    alloc, addr = eip["AllocationId"], eip["EipAddress"]
    print(f"    {alloc}  {addr}")

    try:
        name = "dsh-" + hashlib.sha256(ref.encode()).hexdigest()[:10]
        print(f"==> 建缓存 {name}")
        made = _call("eci", "2018-08-08", "CreateImageCache", {
            "ImageCacheName": name, "Image.1": ref,
            "VSwitchId": config.ECI_VSWITCH_ID,
            "SecurityGroupId": config.ECI_SECURITY_GROUP_ID,
            "ZoneId": config.ECI_ZONE_ID or None,
            "EipInstanceId": alloc,
            "ImageCacheSize": 20, "RetentionDays": 90,
        })
        cid = made["ImageCacheId"]
        t0 = time.time()
        while time.time() - t0 < 1800:
            cur = [c for c in _caches() if c.get("ImageCacheId") == cid]
            st = cur[0].get("Status") if cur else "?"
            if st == "Ready":
                print(f"    Ready @ {int(time.time()-t0)}s")
                break
            if st == "Failed":
                raise RuntimeError(f"缓存构建失败 ({cid}) —— 多半是构建任务没有出网"
                                   f"能力, 检查 EIP 是否真的绑上了")
            time.sleep(10)
        else:
            raise RuntimeError("等了 30 分钟仍未 Ready")
    finally:
        # 无论成败都释放 —— 一个忘了释放的 EIP 会一直计费, 而它没有任何提示
        print("==> 释放临时 EIP")
        try:
            _call("vpc", "2016-04-28", "ReleaseEipAddress", {"AllocationId": alloc})
        except Exception as e:  # noqa: BLE001
            print(f"!! EIP 释放失败, 请手动删除 {alloc}: {e}", file=sys.stderr)

    for old in stale:
        print(f"==> 删旧缓存 {old}")
        try:
            _call("eci", "2018-08-08", "DeleteImageCache", {"ImageCacheId": old})
        except Exception as e:  # noqa: BLE001
            print(f"   (跳过: {e})", file=sys.stderr)

    return check()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        raise SystemExit(check())
    if cmd == "rebuild":
        raise SystemExit(rebuild())
    raise SystemExit(f"用法: {sys.argv[0]} [check|rebuild]")
