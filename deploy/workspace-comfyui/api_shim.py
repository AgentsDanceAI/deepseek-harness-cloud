"""让 ComfyUI **自带的官方 API 节点**跑我们的模型。

ComfyUI 内置了 30+ 个厂商的 API 节点 (Seedance、Kling、Veo、Luma…) 和配套官方
模板, 它们全部打同一个可配地址 (`--comfy-api-base`, 默认 https://api.comfy.org),
路径是厂商原生 API 加个 `/proxy/<厂商>` 前缀 —— comfy.org 那边就是个薄代理。

直接把 comfy-api-base 指向我们的网关不行, 有两处错配:

  鉴权  节点带的是 `auth_token_comfy_org` (用户在 comfy.org 的登录令牌),
        而我们的用户不会有。容器里有的是 DSH_CLOUD_TOKEN。
  形状  厂商原生报文 (Ark 的 tasks/id/content.video_url) 与我们网关的形状
        (task_id / data.status / data.url) 不是一回事。

所以在容器里起这个垫片, 让 comfy-api-base 指向它:

    官方节点 -> 127.0.0.1:8199/proxy/byteplus/... -> 加令牌+转译 -> 网关 /llm/v1

**网关一行都不用改** —— 它调的还是现有的 /videos/generations 与 /images/generations。

⚠️ 这套路径与报文是 comfy.org 与 ComfyUI 之间的**私有约定, 没有文档、版本间会变**。
升级 ComfyUI 后要跑一遍 verify.sh 的官方节点用例; 断掉的表现是「官方节点报错」
而不是我们的代码报错, 不看这里会绕很久。对齐版本: ComfyUI 0.34.1。
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GATEWAY = os.environ.get("DSH_CLOUD_VIDEO_BASE", "https://dshcloud.online/llm/v1").rstrip("/")
TOKEN = os.environ.get("DSH_CLOUD_TOKEN", "")
PORT = int(os.environ.get("DSH_CLOUD_SHIM_PORT", "8199"))
# 生成的图先落盘再以 URL 交出去 —— 官方节点按 data[0]["url"] 取图, 而我们的网关
# 返回的是 b64_json。
BLOBS = pathlib.Path(os.environ.get("DSH_CLOUD_SHIM_BLOBS", "/tmp/dsh-shim-blobs"))
# 与节点里那份同源: 网关前面有 Cloudflare, 按 UA 拦机器人, urllib 默认 UA 吃 403。
USER_AGENT = "DSHCloud-ComfyUI/1.0 (+https://dshcloud.online)"

_lock = threading.Lock()


def _gateway(method: str, path: str, payload: dict | None = None, timeout: float = 120.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{GATEWAY}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _text_of(content) -> str:
    """把 Ark 的 content 数组压成一句提示词。

    官方节点把提示词放在 [{"type":"text","text":...}] 里, 图生视频还会带
    image_url 项。我们的网关只收 prompt + image_url 两个字段。
    """
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return " ".join(p for p in parts if p).strip()


def _first_image_url(content) -> str:
    for item in content or []:
        if isinstance(item, dict) and item.get("type") == "image_url":
            return str((item.get("image_url") or {}).get("url") or "")
    return ""


# 我们的状态词 -> Ark 的状态词。节点按 succeeded/failed 判终态, 别的都当在跑。
_STATUS = {"SUCCESS": "succeeded", "FAIL": "failed", "PROCESSING": "running"}

# 官方节点发的是厂商公开名 (dreamina-seedance-2-5-260628), 而我们网关用的是
# doubao-seedance-2-5-260628 —— **同一个型号, 只差厂商前缀**。写死一张对照表迟早
# 漂 (两边都会加型号), 所以按「去掉厂商前缀后的尾巴」去匹配我们**在售**的清单。
_VENDOR_PREFIXES = ("dreamina-", "doubao-", "byteplus-", "ark-")
_offered_cache: dict | None = None


def _norm(name: str) -> str:
    n = (name or "").strip().lower()
    for prefix in _VENDOR_PREFIXES:
        if n.startswith(prefix):
            return n[len(prefix) :]
    return n


def _offered() -> dict:
    """在售清单 {归一化名: 真实 id}。取不到就返回空 —— 那时按原名透传, 让网关
    自己去拒, 报文里会写清楚不在售。"""
    global _offered_cache
    if _offered_cache is None:
        try:
            data = _gateway("GET", "/media/models", timeout=20.0)
            _offered_cache = {
                _norm(m["id"]): m["id"]
                for kind in ("video", "image")
                for m in (data.get(kind) or [])
                if m.get("id")
            }
            print(f"[shim] 在售型号 {len(_offered_cache)} 个: {sorted(_offered_cache.values())}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[shim] 取在售清单失败 ({type(exc).__name__}: {exc}), 型号按原名透传", flush=True)
            _offered_cache = {}
    return _offered_cache


def _map_model(name: str) -> str:
    table = _offered()
    if not table:
        return name
    return table.get(_norm(name), name)


class Handler(BaseHTTPRequestHandler):
    server_version = "DSHCloudShim/1.0"

    # ---- 基础 ----

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _fail(self, code: int, message: str) -> None:
        # 用 Ark 的错误形状回, 否则节点解不出来只会说「请求失败」。
        self._json(code, {"error": {"code": "ShimError", "message": message}})

    def _unsold(self, model: str, kind: str) -> None:
        """选了个没在售的型号 —— 说清楚现在能用哪些。

        官方节点的模型下拉是**写死在节点里的** (Seedance 2.5/2.0/Fast/Mini 都在),
        我们过滤不了它。选到没定价的型号时, 网关回 404, 而节点只会显示
        「请求失败」—— 用户无从知道该换成哪个。所以这里把在售清单摆出来。
        """
        table = _offered()
        avail = sorted(v for k, v in table.items()) if table else []
        tip = ("当前可用: " + "、".join(avail)) if avail else "当前没有可用型号"
        print(f"[shim] 型号 {model!r} 未在售; {tip}", flush=True)
        self._json(404, {"error": {
            "code": "ModelNotOffered",
            "message": f"「{model}」当前未开放。{tip}。（在节点的模型下拉里换一个）",
        }})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def log_message(self, fmt, *args):  # noqa: A003
        print(f"[shim] {fmt % args}", flush=True)

    # ---- 路由 ----

    # **按厂商显式路由**, 不按后缀。
    #
    # 曾经是 path.endswith("/images/generations") —— 而 ComfyUI 里另有三家的路径
    # 也是这个后缀 (/proxy/openai/…、/proxy/kling/…、/proxy/xai/…)。那样会把它们
    # 的请求误接过来, 用**它们的报文形状**打我们的网关, 产生看不懂的错误。
    # 官方节点一共用到 207 条代理路径、40 个厂商; 这里只接通了 byteplus 这一家。
    _WIRED = ("/proxy/byteplus/", "/proxy/byteplus-seedance2/")

    def _is_wired(self, path: str) -> bool:
        return path.startswith(self._WIRED)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if not self._is_wired(path):
            return self._not_wired(path)
        if path.endswith("/contents/generations/tasks"):
            return self._create_video()
        if path.endswith("/images/generations"):
            return self._create_image()
        return self._not_wired(path)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/blob/"):
            return self._blob(path.rsplit("/", 1)[-1])
        if not self._is_wired(path):
            return self._not_wired(path)
        if "/contents/generations/tasks/" in path:
            return self._video_status(path.rsplit("/", 1)[-1])
        return self._not_wired(path)

    def _not_wired(self, path: str) -> None:
        """这个官方节点还没接到我们的网关 —— 说清楚, 别让人对着「请求失败」猜。"""
        vendor = path.split("/")[2] if path.count("/") >= 2 else path
        print(f"[shim] 未接通的厂商 {vendor!r} (path={path})", flush=True)
        self._json(404, {"error": {
            "code": "VendorNotWired",
            "message": (
                f"「{vendor}」这类官方节点尚未接入本平台。"
                "目前只有 ByteDance / Seedance 系列的官方节点接通了；"
                "其余能力请用「DSH Cloud 生图 / 生视频」节点。"
            ),
        }})

    # ---- 视频 ----

    def _create_video(self):
        body = self._body()
        prompt = _text_of(body.get("content"))
        if not prompt:
            return self._fail(400, "content 里没有文本提示词")
        payload = {
            "model": _map_model(body.get("model") or ""),
            "prompt": prompt,
            # Seedance 2.x 把这两个放在顶层显式字段; 老模型没有, 让网关用默认值。
            "resolution": body.get("resolution") or "",
            "duration": body.get("duration") or 0,
        }
        payload = {k: v for k, v in payload.items() if v not in ("", 0)}
        image_url = _first_image_url(body.get("content"))
        if image_url:
            payload["image_url"] = image_url
        try:
            out = _gateway("POST", "/videos/generations", payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            print(f"[shim] 建视频任务失败 {exc.code}: {detail}", flush=True)
            if exc.code == 404:
                return self._unsold(payload["model"], "video")
            return self._send(exc.code, detail.encode(), "application/json")
        except Exception as exc:  # noqa: BLE001
            return self._fail(502, f"网关不可达: {type(exc).__name__}: {exc}")
        # 官方节点只认 {"id": ...}
        return self._json(200, {"id": out.get("id") or ""})

    def _video_status(self, job_id: str):
        try:
            out = _gateway("GET", f"/videos/result/{job_id}", timeout=60.0)
        except Exception as exc:  # noqa: BLE001
            # 查询失败不能报终态 —— 节点会当成任务失败, 而它可能马上就好。
            print(f"[shim] 查任务失败, 当作在跑: {type(exc).__name__}: {exc}", flush=True)
            return self._json(200, {"id": job_id, "model": "", "status": "running"})

        status = _STATUS.get(str(out.get("task_status") or "").upper(), "running")
        body: dict = {"id": job_id, "model": "", "status": status}
        if status == "succeeded":
            items = out.get("video_result") or []
            url = (items[0] or {}).get("url") if items else ""
            body["content"] = {"video_url": url or ""}
        elif status == "failed":
            body["error"] = {"code": "GenerationFailed", "message": str(out.get("error") or "生成失败")}
        return self._json(200, body)

    # ---- 图像 ----

    def _create_image(self):
        body = self._body()
        payload = {"model": _map_model(body.get("model") or ""), "prompt": body.get("prompt") or ""}
        for key in ("size", "seed", "watermark"):
            if body.get(key) is not None:
                payload[key] = body[key]
        if not payload["prompt"]:
            return self._fail(400, "缺少 prompt")
        try:
            out = _gateway("POST", "/images/generations", payload, timeout=300.0)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            print(f"[shim] 生图失败 {exc.code}: {detail}", flush=True)
            if exc.code == 404:
                return self._unsold(payload["model"], "image")
            return self._send(exc.code, detail.encode(), "application/json")
        except Exception as exc:  # noqa: BLE001
            return self._fail(502, f"网关不可达: {type(exc).__name__}: {exc}")

        # 官方节点按 data[0]["url"] 取图, 而网关给的是 b64_json —— 落盘再给个
        # 本地 URL。图只在本容器内被取一次, 用完不删也无所谓 (容器本身是临时的)。
        data = []
        for item in out.get("data") or []:
            if item.get("url"):
                data.append({"url": item["url"]})
                continue
            b64 = item.get("b64_json")
            if not b64:
                continue
            BLOBS.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}.png"
            (BLOBS / name).write_bytes(base64.b64decode(b64))
            data.append({"url": f"http://127.0.0.1:{PORT}/blob/{name}"})
        return self._json(200, {
            "model": payload["model"], "created": int(out.get("created") or 0),
            "data": data, "error": {},
        })

    def _blob(self, name: str):
        # 只认自己造的文件名, 不接受任何路径成分 —— 这个端口虽然只在回环上,
        # 但拼路径的习惯不能留。
        if "/" in name or ".." in name:
            return self._fail(400, "非法文件名")
        path = BLOBS / name
        if not path.is_file():
            return self._fail(404, "没有这个文件")
        self._send(200, path.read_bytes(), "image/png")


def main() -> None:
    print(f"[shim] 监听 127.0.0.1:{PORT} -> {GATEWAY}  token={'有' if TOKEN else '无'}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
