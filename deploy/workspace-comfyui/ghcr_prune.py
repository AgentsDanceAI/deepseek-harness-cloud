#!/usr/bin/env python3
"""清理 ghcr 上 comfy-local 的旧版本。**默认只演练, 加 --apply 才真删。**

    python3 ghcr_prune.py                 # 演练: 打印会删什么
    python3 ghcr_prune.py --apply         # 真删
    KEEP=4 python3 ghcr_prune.py --apply  # 多留几个回滚位 (默认 2)

为什么不能照着"无 tag 就删"来做
--------------------------------
`docker build` 推上去的是一个 **OCI 索引**, 它引用两个子清单: 真正的
linux/amd64 镜像, 和一个 attestation (platform 显示 unknown/unknown)。
这两个子清单在 GitHub 的 API 里各占一个"版本", 而且**没有 tag**。

所以"删掉所有无 tag 版本"这种常见写法, 会把 r15 索引指向的那两块删掉 ——
tag 还在, 拉下来却是坏的。表现是 ECI 冷启动失败, 而 GitHub 那边一切正常。
2026-08-28 写这个脚本时先 `docker manifest inspect` 了一次才发现。

于是保护集合 = {要保留的 tag} ∪ {这些 tag 索引所引用的全部子清单摘要}。

安全条件 (与 build.sh 的本机删旧一致)
-------------------------------------
  1. 生产 .env 引用的 tag 永不删
  2. 读不出生产 tag 就**一个都不删** (不知道线上用哪个时不动手)
  3. 其余按创建时间倒序留 KEEP 个
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

OWNER = os.environ.get("GHCR_OWNER", "AgentsDancePro")
PKG = os.environ.get("GHCR_PACKAGE", "comfy-local")
IMAGE = f"ghcr.io/{OWNER.lower()}/{PKG}"
KEEP = int(os.environ.get("KEEP", "2"))
ENVFILE = os.environ.get("ENVFILE", "/data/workspace/deepseek-harness-cloud/deploy/prod/.env")
DOCKER_CFG = os.environ.get("DOCKER_CONFIG_JSON", "/root/.docker/config.json")


def token() -> str:
    """从 docker 的登录凭据里取 ghcr token。绝不打印它。"""
    with open(DOCKER_CFG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    auth = ((cfg.get("auths") or {}).get("ghcr.io") or {}).get("auth")
    if not auth:
        sys.exit(f"{DOCKER_CFG} 里没有 ghcr.io 的凭据 —— 先 docker login ghcr.io")
    return base64.b64decode(auth).decode().partition(":")[2]


def api(path: str, method: str = "GET", tok: str = ""):
    req = urllib.request.Request("https://api.github.com" + path, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status == 204 or not (body := resp.read()):
            return None
        return json.loads(body)


def prod_tag() -> str:
    """生产 .env 里 COMFY_IMAGE_REF 的 tag。取最后一个冒号之后 —— 仓库地址
    本身可能带端口号, 按冒号切字段会取错 (build.sh 里踩过)。"""
    try:
        with open(ENVFILE, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("COMFY_IMAGE_REF="):
                    return line.strip().partition("=")[2].rpartition(":")[2]
    except OSError:
        pass
    return ""


def referenced_digests(tag: str) -> set[str]:
    """一个 tag 的索引引用了哪些子清单摘要。取不到就返回 None 表示"问不出来"。"""
    try:
        out = subprocess.run(
            ["docker", "manifest", "inspect", f"{IMAGE}:{tag}"],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    try:
        doc = json.loads(out)
    except ValueError:
        return None
    return {m["digest"] for m in doc.get("manifests", []) if m.get("digest")}


def main() -> int:
    apply = "--apply" in sys.argv
    tok = token()

    versions, page = [], 1
    while True:
        batch = api(f"/users/{OWNER}/packages/container/{PKG}/versions?per_page=100&page={page}", tok=tok)
        if not batch:
            break
        versions += batch
        page += 1
    versions.sort(key=lambda v: v["created_at"], reverse=True)

    def tags_of(v) -> list[str]:
        return list(((v.get("metadata") or {}).get("container") or {}).get("tags") or [])

    prod = prod_tag()
    if not prod:
        print(f"!! 读不出生产 tag ({ENVFILE}) —— 一个都不删", file=sys.stderr)
        return 2
    print(f"生产在用: {prod} (永不删)")

    # 要保留的 tag: 生产那个 + 最近 KEEP 个
    keep_tags = {prod}
    for v in versions:
        if len(keep_tags) >= KEEP + 1:
            break
        for t in tags_of(v):
            if re.search(r"r\d+$", t):
                keep_tags.add(t)
                break
    print(f"保留 tag: {', '.join(sorted(keep_tags))}")

    # 保护集合还要加上这些 tag 的索引所引用的**子清单** —— 它们在 API 里是
    # 无 tag 版本, 删了会让 tag 还在但拉不动。
    protected: set[str] = set()
    for t in sorted(keep_tags):
        kids = referenced_digests(t)
        if kids is None:
            print(f"!! 问不出 {t} 引用了哪些子清单 —— 无法判断哪些无 tag 版本是安全的, 停手",
                  file=sys.stderr)
            return 2
        protected |= kids
        print(f"  {t} 引用 {len(kids)} 个子清单")

    doomed = []
    for v in versions:
        ts = tags_of(v)
        if any(t in keep_tags for t in ts):
            continue
        if v["name"] in protected:      # name 就是 sha256:… 摘要
            continue
        doomed.append(v)

    kept = len(versions) - len(doomed)
    print(f"\n共 {len(versions)} 个版本: 保留 {kept}, {'删除' if apply else '将删除'} {len(doomed)}")
    for v in doomed[:6]:
        label = ",".join(tags_of(v)) or "(untagged)"
        print(f"  {'删' if apply else '会删'} id={v['id']:<12} {label:<16} {v['created_at'][:16]}")
    if len(doomed) > 6:
        print(f"  … 另有 {len(doomed) - 6} 个")

    if not apply:
        print("\n(演练, 什么都没删。加 --apply 才真删)")
        return 0

    failed = 0
    for v in doomed:
        try:
            api(f"/users/{OWNER}/packages/container/{PKG}/versions/{v['id']}", "DELETE", tok)
        except urllib.error.HTTPError as exc:
            print(f"  !! id={v['id']} 删除失败 HTTP {exc.code}", file=sys.stderr)
            failed += 1
    print(f"\n已删 {len(doomed) - failed} 个, 失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
