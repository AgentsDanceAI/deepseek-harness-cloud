"""ECI 镜像缓存: 查漂移 / 重建。

    python3 -m scripts.eci_image_cache check            # 每个已启用产品的镜像都有缓存吗
    python3 -m scripts.eci_image_cache prepare <ref>...  # 给指定镜像建缓存, **什么都不删**
    python3 -m scripts.eci_image_cache rebuild          # 给缺的那些建, 顺带清掉谁都不认的

**发布顺序很重要**: rebuild 只认当前 .env 里的引用, 所以"改 .env -> 部署 ->
rebuild"这个顺序会留下一段**线上 tag 没有缓存**的窗口 (缓存要建十来分钟)。那段
时间里每个冷启动都是完整拉 2.5GB 镜像 —— 从二十几秒变成几分钟, 而且不报错。
2026-08-28 就是这么让人卡住的。正确顺序是先 prepare 新 tag, 再切 .env:

    prepare <新 ref>   # 旧镜像仍在服务, 用户无感
    改 .env / 部署
    rebuild            # 这时才清掉旧的

镜像缓存按镜像引用匹配。WORK_IMAGE_REF 更新后必须同步重建缓存，否则新实例会
静默回退到完整镜像拉取并增加冷启动时间。

缓存构建任务不共享业务实例的网络出口，因此脚本临时申请 EIP 并传给
CreateImageCache，完成后释放。
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
    """阿里云 RPC 调用；签名算法与 workbackend._sign 保持一致。"""
    p = {
        "Action": action,
        "Version": version,
        "Format": "JSON",
        "AccessKeyId": config.ECI_ACCESS_KEY_ID,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "RegionId": config.ECI_REGION_ID,
    }
    p.update({k: v for k, v in (params or {}).items() if v is not None})
    canon = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in sorted(p.items()))
    sts = f"POST&{_pe('/')}&{_pe(canon)}"
    p["Signature"] = base64.b64encode(
        hmac.new((config.ECI_ACCESS_KEY_SECRET + "&").encode(), sts.encode(), hashlib.sha1).digest()
    ).decode()
    # quotas 是全局服务, 没有分地域的域名; 其余产品带地域。
    host = "quotas.aliyuncs.com" if product == "quotas" else f"{product}.{config.ECI_REGION_ID}.aliyuncs.com"
    r = httpx.post(f"https://{host}/", data=p, timeout=60.0)
    body = r.json()
    # 阿里云把错误也放在非 200 之外的地方, Code 字段才是权威的
    if r.status_code != 200 or body.get("Code"):
        raise RuntimeError(
            f"{action}: {body.get('Code') or r.status_code} {body.get('Message', r.text[:200])}"
        )
    return body


def _caches() -> list[dict]:
    return _call("eci", "2018-08-08", "DescribeImageCaches").get("ImageCaches", [])


def _ref_sets() -> list[tuple[str, tuple[str, ...]]]:
    """每个**已启用产品**的镜像集合 (主容器 + 伴随容器)。

    栈产品 (Penpot/Coze/Dify) 一次要拉一组镜像 —— 缓存必须整组装下, 只缓存主
    容器等于没缓存 (冷启动照样全量拉伴随容器)。单容器产品的集合就是一个元素,
    行为与从前一致。

    另一条旧教训继续有效: rebuild() 只删"哪个产品都不认"的缓存 —— 只按单个
    产品判会顺手删掉别的产品的缓存, 让冷启动退回全量拉取, 不报错只是变慢。
    """
    from app import products

    sets = []
    for product in products.enabled():
        refs = [(product.image_ref or product.image).strip()]
        refs += [sc.image_ref.strip() for sc in product.sidecars]
        deduped = tuple(dict.fromkeys(r for r in refs if r))
        if deduped:
            sets.append((product.id, deduped))
    if not sets:
        raise SystemExit("没有已启用的工作台产品 —— 没有可对照的镜像引用")
    return sets


def eip_headroom() -> int | None:
    """并发上限与 EIP 配额的余量; 读不到返回 **None**。

    不要用 -1 表示“未知”：余量本身可能为负，复用同一个哨兵值会漏掉超额配置。

    每个工作台自动创建一个 EIP, 所以**并发的真正天花板是 EIP 配额**, 不是
    WORK_MAX_CONCURRENT。把上限配得比配额高不会有任何提示: 前 N 个用户一切
    正常, 第 N+1 个看到"启动失败", 而错误里不会提配额。
    这条配额是**账号级**的 (给它传 regionId 会直接 QUOTA.DIMENSION.UNSUPPORT),
    所以同账号下别的产品线用掉的 EIP 也算在里面。
    """
    try:
        body = _call("quotas", "2020-05-10", "ListProductQuotas", {"ProductCode": "eip", "MaxResults": 50})
    except Exception as e:  # noqa: BLE001
        print(f"  (读不到 EIP 配额, 跳过并发检查: {e})", file=sys.stderr)
        return None
    for q in body.get("Quotas", []):
        if q.get("QuotaActionCode") == "q_6arozx":
            return int(q.get("TotalQuota", 0)) - int(config.WORK_MAX_CONCURRENT)
    return None


def _covered(refs: tuple[str, ...], caches: list[dict]) -> dict | None:
    """这组镜像有没有被某个 Ready 缓存**整组**装下。部分命中不算 —— 缺谁谁就
    要全量拉, 而栈产品最大的往往正是伴随容器 (数据库/向量库)。"""
    want = set(refs)
    for c in caches:
        if c.get("Status") == "Ready" and want <= set(c.get("Images") or []):
            return c
    return None


def check() -> int:
    sets = _ref_sets()
    caches = _caches()
    missing = [(pid, refs) for pid, refs in sets if _covered(refs, caches) is None]
    rc = 0
    # 至少留几个 EIP 给"重建镜像缓存"和人工排查 —— 前者恰好在升级镜像版本时跑,
    # 是最不该因为抢不到 EIP 而失败的时刻。
    head = eip_headroom()
    if head is not None and head < 5:
        if head < 0:
            print(
                f"✗ WORK_MAX_CONCURRENT={config.WORK_MAX_CONCURRENT} **超过** EIP 配额 "
                f"{config.WORK_MAX_CONCURRENT + head} 个 —— 超出的那部分用户会看到"
                f"“启动失败”, 而错误里不会提配额。",
                file=sys.stderr,
            )
        else:
            print(
                f"✗ WORK_MAX_CONCURRENT={config.WORK_MAX_CONCURRENT} 距 EIP 配额只剩 {head} 个。",
                file=sys.stderr,
            )
        print(
            "  每个工作台占一个 EIP, 配额是**账号级**的 (别的产品线也算在里面)。"
            "留不足 5 个时, 重建镜像缓存会抢不到 EIP 而失败。",
            file=sys.stderr,
        )
        rc = 1
    if not missing:
        print(f"✓ {len(sets)} 个产品的镜像组都有 Ready 缓存:")
        for pid, refs in sets:
            hit = _covered(refs, caches)
            print(f"  {pid} ({len(refs)} 镜像)  ->  {hit.get('ImageCacheId')} ({hit.get('ImageCacheName')})")
        if head is not None and head >= 5:
            print(f"✓ EIP 余量 {head} 个 (配额 - WORK_MAX_CONCURRENT)")
        return rc
    for pid, refs in missing:
        print(f"✗ 产品 {pid} 的镜像组 ({len(refs)} 个) 没有整组 Ready 的缓存。", file=sys.stderr)
    print("  缓存未命中会回退到完整镜像拉取并增加冷启动时间。", file=sys.stderr)
    for c in caches:
        print(f"  现有: {c.get('ImageCacheId')} {c.get('Status')} {c.get('Images')}", file=sys.stderr)
    print("  修复: python3 -m scripts.eci_image_cache rebuild", file=sys.stderr)
    return 1


def _drop(cache_id: str) -> None:
    try:
        _call("eci", "2018-08-08", "DeleteImageCache", {"ImageCacheId": cache_id})
    except Exception as e:  # noqa: BLE001
        print(f"   (删 {cache_id} 失败, 跳过: {e})", file=sys.stderr)


def _await_ready(cache_id: str, ref: str, timeout: float = 900.0) -> bool:
    """等一个在建的缓存到 Ready。返回它是否成功。

    **为什么要等而不是直接认它**: ECI 在实例缓存未命中时会**自己建**一个缓存,
    而自动建的那个没有临时 EIP、没有出网能力 —— 它会卡在 Preparing 0% 然后转
    Failed。把 Creating/Preparing 一律当成「已经有了」而跳过, 就等于把这个必然
    失败的缓存认下来: 之后没人再管它, 冷启动一直慢, 而且不报错。
    2026-08-27 实测踩到, 只能人工删掉重建。
    """
    print(f"==> {ref} 已有在建的缓存 {cache_id}, 等它到 Ready (最多 {int(timeout)}s)")
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        cur = [c for c in _caches() if c.get("ImageCacheId") == cache_id]
        if not cur:
            print("    它消失了 —— 当作失败处理")
            return False
        st = cur[0].get("Status")
        if st != last:
            print(f"    {st} ({cur[0].get('Progress')}) @ {int(time.time() - t0)}s")
            last = st
        if st == "Ready":
            return True
        if st == "Failed":
            print("    构建失败 —— 多半是 ECI 自动建的那个 (没有出网能力), 删掉自己建")
            _drop(cache_id)
            return False
        time.sleep(10)
    print("    等超时 —— 删掉自己建")
    _drop(cache_id)
    return False


def rebuild() -> int:
    sets = _ref_sets()
    caches = _caches()
    all_refs = {r for _, refs in sets for r in refs}
    # 陈旧 = **哪个产品的镜像都不沾**。只按单个产品判会顺手删掉别的产品的缓存
    # (见 _ref_sets 的说明)。
    stale = [
        c["ImageCacheId"]
        for c in caches
        if not (set(c.get("Images") or []) & all_refs)
        or c.get("Status") not in ("Ready", "Creating", "Preparing")
    ]

    todo = []
    for pid, refs in sets:
        if _covered(refs, caches):
            continue
        # 在建的要**等出结果**, 不能当成已经有了 —— 见 _await_ready 的说明。
        building = [
            c for c in caches
            if c.get("Status") in ("Creating", "Preparing") and set(refs) <= set(c.get("Images") or [])
        ]
        if building and _await_ready(building[0]["ImageCacheId"], refs[0]):
            continue
        todo.append((pid, refs))

    if not todo:
        print(f"==> {len(sets)} 个产品的镜像组都已有 Ready 缓存")
        for old in stale:
            print(f"==> 删旧缓存 {old}")
            _drop(old)
        return check()
    rc = 0
    for i, (pid, refs) in enumerate(todo):
        rc |= _build_one(refs, stale if i == len(todo) - 1 else [])
    return rc or check()


def _build_one(refs, stale: list[str]) -> int:
    if isinstance(refs, str):
        refs = (refs,)   # prepare 的单镜像路径继续可用
    print(f"==> 目标镜像组 ({len(refs)} 个):")
    for r in refs:
        print(f"      {r}")

    print("==> 申请临时 EIP (构建任务不共享业务实例的 EIP, 自己没有出网能力)")
    eip = _call(
        "vpc",
        "2016-04-28",
        "AllocateEipAddress",
        {"Bandwidth": 100, "InternetChargeType": "PayByTraffic", "Name": "dsh-imagecache-build"},
    )
    alloc, addr = eip["AllocationId"], eip["EipAddress"]
    print(f"    {alloc}  {addr}")

    try:
        name = "dsh-" + hashlib.sha256("|".join(refs).encode()).hexdigest()[:10]
        print(f"==> 建缓存 {name}")
        made = _call(
            "eci",
            "2018-08-08",
            "CreateImageCache",
            {
                "ImageCacheName": name,
                **{f"Image.{i}": r for i, r in enumerate(refs, start=1)},
                "VSwitchId": config.ECI_VSWITCH_ID,
                "SecurityGroupId": config.ECI_SECURITY_GROUP_ID,
                "ZoneId": config.ECI_ZONE_ID or None,
                "EipInstanceId": alloc,
                "ImageCacheSize": 20,
                "RetentionDays": 90,
            },
        )
        cid = made["ImageCacheId"]
        t0 = time.time()
        while time.time() - t0 < 1800:
            cur = [c for c in _caches() if c.get("ImageCacheId") == cid]
            st = cur[0].get("Status") if cur else "?"
            if st == "Ready":
                print(f"    Ready @ {int(time.time() - t0)}s")
                break
            if st == "?":
                pass
            if st == "Failed":
                raise RuntimeError(
                    f"缓存构建失败 ({cid}) —— 多半是构建任务没有出网能力, 检查 EIP 是否真的绑上了"
                )
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
        _drop(old)

    return 0


def prepare(refs: list[str]) -> int:
    """给指定镜像备好缓存, **一个都不删**。

    切 tag 之前用: 那时 .env 还指着旧镜像, 旧缓存必须留着继续服务。rebuild 会
    删掉"当前 .env 认不出的"缓存, 拿它来做这件事等于把还在用的那个删了。
    """
    caches = _caches()
    rc = 0
    for ref in refs:
        mine = [c for c in caches if ref in (c.get("Images") or [])]
        if any(c.get("Status") == "Ready" for c in mine):
            print(f"==> {ref} 已有 Ready 缓存, 跳过")
            continue
        building = [c for c in mine if c.get("Status") in ("Creating", "Preparing")]
        if building and _await_ready(building[0]["ImageCacheId"], ref):
            continue
        rc |= _build_one(ref, [])   # stale=[] —— 这一步什么都不删
    return rc


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        raise SystemExit(check())
    if cmd == "rebuild":
        raise SystemExit(rebuild())
    if cmd == "prepare":
        wanted = [a for a in sys.argv[2:] if a.strip()]
        if not wanted:
            raise SystemExit("prepare 需要至少一个镜像引用")
        raise SystemExit(prepare(wanted))
    raise SystemExit(f"用法: {sys.argv[0]} [check|rebuild|prepare <ref>...]")
