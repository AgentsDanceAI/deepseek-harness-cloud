"""垫片自检: 用 **Ark 的报文形状** 打它, 确认转译与型号映射都对。

刻意不走 ComfyUI 的节点: 节点的内部结构版本间会变, 而垫片对外的契约
(comfy.org 那套 /proxy/... 路径与报文) 才是要钉住的东西。
"""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8199"


def call(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf-8", "replace")


def main() -> int:
    # 官方节点发的是厂商公开名 (dreamina-), 垫片要映射到我们在售的 (doubao-)
    code, out = call(
        "POST",
        "/proxy/byteplus/api/v3/contents/generations/tasks",
        {
            "model": "dreamina-seedance-2-5-260628",
            "content": [{"type": "text", "text": "一只猫在雪地里奔跑"}],
            "resolution": "480p",
            "duration": 5,
        },
    )
    if code != 200 or not isinstance(out, dict) or not out.get("id"):
        print(f"  ✗ 建任务: {code} {out}")
        return 1
    task_id = out["id"]
    print(f"  ✓ 建任务 -> Ark 形状的 id={task_id}")

    code, out = call("GET", f"/proxy/byteplus-seedance2/api/v3/contents/generations/tasks/{task_id}")
    if code != 200 or not isinstance(out, dict):
        print(f"  ✗ 查任务: {code} {out}")
        return 1
    if out.get("status") not in ("queued", "running", "succeeded", "failed"):
        print(f"  ✗ 状态词不是 Ark 的那套: {out}")
        return 1
    print(f"  ✓ 查任务 -> status={out['status']}")

    # 生图: 网关给 b64, 垫片必须落盘再交出一个 url —— 官方节点按 data[0]["url"] 取图
    code, out = call(
        "POST",
        "/proxy/byteplus/api/v3/images/generations",
        {"model": "gpt-image-2", "prompt": "一只柴犬", "size": "1024x1024"},
    )
    if code != 200 or not isinstance(out, dict):
        print(f"  ✗ 生图: {code} {out}")
        return 1
    data = out.get("data") or []
    if not data or not data[0].get("url"):
        print(f"  ✗ 生图没给出 url (官方节点按 data[0][\"url\"] 取图): {out}")
        return 1
    print(f"  ✓ 生图 -> {data[0]['url']}")

    # blob 是二进制, 不能用上面那个 (它 json.loads)
    try:
        with urllib.request.urlopen(data[0]["url"], timeout=30) as r:
            blob = r.read()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 取不回那张图: {type(exc).__name__}: {exc}")
        return 1
    if not blob.startswith(b"\x89PNG"):
        print(f"  ✗ 取回来的不是 PNG: {blob[:12]!r}")
        return 1
    print(f"  ✓ 图能取回 ({len(blob)} 字节, PNG)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
