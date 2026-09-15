#!/usr/bin/env python3
"""桌面端本地运行的那些镜像, 有没有 arm64 那一份。

为什么要有它: 线上节点全是 amd64, 所以镜像一直只建 amd64 —— 但桌面端把**同一个
镜像**跑在用户自己的机器上。Apple Silicon 上跑 amd64 要过 Rosetta, 实测 node 冷
启动慢 17 倍、sha256 慢 8.6 倍; Docker Desktop 会挂一个橙色 AMD64 角标写着
"may have poor performance, or fail, if run via emulation"。

这件事**不会报错**: 容器照样起、页面照样开, 只是全程慢一截。所以只能主动查。
发完版跑一遍:

    python3 server/scripts/image_arch_check.py            # 读线上目录
    DSH_IMAGES=a:1,b:2 python3 server/scripts/image_arch_check.py   # 指定几个

退出码: 0 = 每个都有 arm64; 1 = 有缺的 (会逐行列出来)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def arches(ref: str) -> list[str]:
    """问 registry 要这个 tag 下的架构清单。读不到就返回空。"""
    out = subprocess.run(
        ["docker", "manifest", "inspect", "-v", ref],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return []
    try:
        doc = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    items = doc if isinstance(doc, list) else [doc]
    found = []
    for item in items:
        plat = (item.get("Descriptor") or {}).get("platform") or {}
        arch = plat.get("architecture")
        # attestation manifest 的架构是 "unknown", 不算数。
        if arch and arch != "unknown":
            found.append(arch)
    return sorted(set(found))


def refs() -> list[tuple[str, str]]:
    override = os.environ.get("DSH_IMAGES")
    if override:
        return [(r.split(":")[0].rsplit("/", 1)[-1], r) for r in override.split(",") if r]
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from server.app import local_api  # noqa: PLC0415

    return [(p.id, p.image_ref or p.image) for p in local_api.available()]


def main() -> int:
    missing = []
    for pid, ref in refs():
        found = arches(ref)
        mark = "✓" if "arm64" in found else ("?" if not found else "✗")
        print(f"{mark} {pid:<14} {ref:<56} {','.join(found) or '读不到'}")
        if "arm64" not in found:
            missing.append((pid, ref))
    if missing:
        print(f"\n{len(missing)} 个镜像没有 arm64 —— 苹果芯片的机器上会走 Rosetta:")
        for pid, ref in missing:
            print(f"  {pid}: {ref}")
        return 1
    print("\n都有 arm64。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
