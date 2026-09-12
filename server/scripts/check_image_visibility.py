"""目录里每一格引用的镜像, 陌生人拉不拉得动。

    python server/scripts/check_image_visibility.py        # 退出码 0/1

不是洁癖: 「把工作台跑在自己机器上」的第一步就是 `docker pull`。镜像忘了设公开的
表现是用户等几分钟拿到一行 denied —— 而我们这边一切正常, 没有任何信号。README 曾
为此维护过一张手写的"哪些还私有"清单, 清单会烂 (2026-09-10 实测: 上面写公开的有
一个其实是私有的)。

**要在生产机上跑**: 有五格的镜像引用来自 `.env` (WORK_IMAGE_REF、COMFY_IMAGE_REF
这些), 仓库里没有 —— CI 只看得见代码里写死的那几个。同一个探针在 CI 里由
`tests/test_image_visibility.py` 用代码里那部分跑。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ACCEPT = ",".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def _switchable_refs() -> set[str]:
    """开关**另一侧**的镜像 —— registry() 只反映当前这一侧。

    claude-code / codex 两格的外壳由 USE_CLI_WORKSPACE 选 (agentui 或 pi-web-ui
    那份 CLI 外壳)。没翻开关时另一侧的镜像在 registry() 里根本不出现, 于是"包忘了
    设公开"这件事要等到翻开关当天、集群拉不动才暴露 —— 而翻开关只是改一行 env,
    改的人不会想到顺手查一遍可见性。两侧一起钉住。
    """
    from app import config

    # OpenManus 那格的 pi-web-ui 版也在开关另一侧 (同一个开关管三格)。
    return {config.CLI_WORKSPACE_IMAGE_REF, config.AGENTUI_IMAGE_REF, config.OPENMANUS_CLI_IMAGE_REF}


def ghcr_refs() -> list[str]:
    """当前配置下, 目录引用到的**我们自己的** ghcr 镜像。

    sidecar 里那些上游镜像 (postgres、redis…) 在 docker.io 上, 有匿名拉取限流,
    查它们只会带来抖动而不是信息。
    """
    from app import products

    off = products.disabled_ids()
    out = set()
    for p in products.registry().values():
        if p.id in off:
            continue
        for ref in (p.image_ref or p.image, *(sc.image_ref for sc in p.sidecars)):
            if ref and ref.startswith("ghcr.io/"):
                out.add(ref)
    out.update(r for r in _switchable_refs() if r and r.startswith("ghcr.io/"))
    return sorted(out)


def anon_status(ref: str) -> int | str:
    """匿名拉这个 tag 的 manifest 会得到什么。

    返回状态码; **网络不通返回字符串** —— 把"连不上"和"被拒绝"分开, 否则一次
    网络抖动会被读成"镜像变私有了"。
    """
    name, _, tag = ref[len("ghcr.io/") :].rpartition(":")
    try:
        with urllib.request.urlopen(
            f"https://ghcr.io/token?scope=repository:{name}:pull&service=ghcr.io", timeout=8
        ) as r:
            token = json.load(r).get("token", "")
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{name}/manifests/{tag}",
            headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # noqa: BLE001
        return f"unreachable: {type(e).__name__}"


def probe(refs: list[str]) -> dict[str, int | str]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(refs, pool.map(anon_status, refs), strict=True))


def main() -> int:
    refs = ghcr_refs()
    if not refs:
        print("!! 一个 ghcr 镜像都没扫到 —— 先看 registry() 是不是空的", file=sys.stderr)
        return 1
    results = probe(refs)
    bad = 0
    for ref, st in results.items():
        mark = {200: "公开"}.get(st, str(st))
        if st in (401, 403):
            mark, bad = "**私有 — 陌生人拉不动**", bad + 1
        elif st == 404:
            mark, bad = "**tag 不存在**", bad + 1
        print(f"  {ref:<52} {mark}")
    if bad:
        print(
            f"\n{bad} 个拉不动。去 github.com/orgs/agentsdancepro/packages 把包的可见性改成 Public。",
            file=sys.stderr,
        )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
