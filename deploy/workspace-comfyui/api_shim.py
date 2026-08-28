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

    官方节点 -> 127.0.0.1:8199/proxy/<厂商>/... -> 加令牌+转译 -> 网关 /llm/v1

**网关一行都不用改** —— 它调的还是现有的 /videos/generations 与 /images/generations。

已接通三家 (逐家核过, 为什么只有三家见 README 的对照表):

    byteplus / seedance   Ark 形状          文生视频、图生视频、生图
    openai                与网关同构        GPT Image 生图 (gpt-image-2 / 1.5)
    wan                   DashScope 形状    文生视频、图生视频、文生图/图生图

其余 37 家一律回 VendorNotWired —— **不是没写代码, 是网关不卖它们的型号,
或者节点下拉里写死的名字与我们在售的对不上**。

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


# 本地图任务表 (Wan 的图像走异步契约, 而网关的生图是同步的)。
_IMG_PREFIX = "shimimg-"
_IMG_TASKS: dict[str, dict] = {}

# DashScope 的 size 是 "1920*1080", 我们按短边定档。
_RES_BUCKETS = ((1000, "1080p"), (700, "720p"), (0, "480p"))


def _resolution_of(params: dict) -> str:
    """从 DashScope 的 parameters 里定出我们的分辨率档 (480p/720p/1080p)。

    节点有两种写法: 视频类给 resolution="1080P", 图生视频给 size="1920*1080"。
    """
    raw = str(params.get("resolution") or "").strip().lower()
    if raw in ("480p", "720p", "1080p"):
        return raw
    size = str(params.get("size") or "")
    nums = [int(n) for n in size.replace("x", "*").split("*") if n.strip().isdigit()]
    if len(nums) == 2:
        short = min(nums)
        for floor, bucket in _RES_BUCKETS:
            if short > floor:
                return bucket
    return ""


def _run_image_task(task_id: str, payload: dict) -> None:
    """后台跑一次同步生图, 把结果落进本地任务表。"""
    status, urls, message = "FAILED", [], ""
    try:
        out = _gateway("POST", "/images/generations", payload, timeout=300.0)
        BLOBS.mkdir(parents=True, exist_ok=True)
        for item in out.get("data") or []:
            if item.get("url"):
                urls.append(item["url"])
                continue
            b64 = item.get("b64_json")
            if not b64:
                continue
            name = f"{uuid.uuid4().hex}.png"
            (BLOBS / name).write_bytes(base64.b64decode(b64))
            urls.append(f"http://127.0.0.1:{PORT}/blob/{name}")
        status = "SUCCEEDED" if urls else "FAILED"
        if not urls:
            message = "网关没有返回图像"
    except urllib.error.HTTPError as exc:
        message = exc.read()[:400].decode("utf-8", "replace")
        print(f"[shim] 后台生图失败 {exc.code}: {message}", flush=True)
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        print(f"[shim] 后台生图异常: {message}", flush=True)
    with _lock:
        _IMG_TASKS[task_id] = {"task_status": status, "urls": urls, "message": message}


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
    #
    # 官方节点一共用到 207 条代理路径、40 个厂商。**接一家的前提是网关真的卖那家
    # 的型号, 而且节点下拉里的名字对得上** —— 对不上就只能是「未在售」, 接了也白接。
    # 逐家核过后能接的就下面这三家 (见 README 的对照表)。
    _WIRED = (
        "/proxy/byteplus/",
        "/proxy/byteplus-seedance2/",
        "/proxy/openai/",
        "/proxy/wan/",
    )

    def _is_wired(self, path: str) -> bool:
        return path.startswith(self._WIRED)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if not self._is_wired(path):
            return self._not_wired(path)
        # --- ByteDance / Seedance (Ark 形状) ---
        if path.startswith(("/proxy/byteplus/", "/proxy/byteplus-seedance2/")):
            if path.endswith("/contents/generations/tasks"):
                return self._create_video()
            if path.endswith("/images/generations"):
                return self._create_image()
        # --- OpenAI (报文与网关同源, 近乎直通) ---
        elif path.startswith("/proxy/openai/"):
            if path.endswith("/images/generations"):
                return self._openai_image()
            if path.endswith("/images/edits"):
                return self._fail(400, "本平台的图像通道只做「文生图」，不支持 OpenAI 的图像编辑端点。")
        # --- Wan (DashScope 原生形状) ---
        elif path.startswith("/proxy/wan/"):
            if path.endswith("/video-generation/video-synthesis"):
                return self._wan_video()
            if path.endswith("/image-synthesis"):
                return self._wan_image()
        return self._not_wired(path)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/blob/"):
            return self._blob(path.rsplit("/", 1)[-1])
        if not self._is_wired(path):
            return self._not_wired(path)
        if path.startswith(("/proxy/byteplus/", "/proxy/byteplus-seedance2/")):
            if "/contents/generations/tasks/" in path:
                return self._video_status(path.rsplit("/", 1)[-1])
        elif path.startswith("/proxy/wan/") and "/api/v1/tasks/" in path:
            return self._wan_task(path.rsplit("/", 1)[-1])
        return self._not_wired(path)

    def _not_wired(self, path: str) -> None:
        """这个官方节点还没接到我们的网关 —— 说清楚, 别让人对着「请求失败」猜。"""
        vendor = path.split("/")[2] if path.count("/") >= 2 else path
        print(f"[shim] 未接通的厂商 {vendor!r} (path={path})", flush=True)
        self._json(404, {"error": {
            "code": "VendorNotWired",
            "message": (
                f"「{vendor}」这类官方节点尚未接入本平台。"
                "已接通: ByteDance/Seedance 视频、OpenAI GPT Image、Wan 视频；"
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

    # ---- OpenAI 官方图像节点 ----

    def _openai_image(self):
        """OpenAI 的 /images/generations 与我们网关**同一套报文** (网关本身就是
        OpenAI 兼容的), 所以这里只做三件事: 映射型号、转达错误、把 usage 带回去。

        图也不用落盘 —— 节点的 validate_and_cast_response 优先读 b64_json,
        网关给的正好就是 b64_json。
        """
        body = self._body()
        model = _map_model(body.get("model") or "")
        prompt = body.get("prompt") or ""
        if not prompt:
            return self._fail(400, "缺少 prompt")
        payload = {"model": model, "prompt": prompt}
        for key in ("n", "size", "quality", "background", "output_format"):
            if body.get(key) is not None:
                payload[key] = body[key]
        try:
            out = _gateway("POST", "/images/generations", payload, timeout=300.0)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            print(f"[shim] OpenAI 生图失败 {exc.code}: {detail}", flush=True)
            if exc.code == 404:
                return self._unsold(model, "image")
            return self._send(exc.code, detail.encode(), "application/json")
        except Exception as exc:  # noqa: BLE001
            return self._fail(502, f"网关不可达: {type(exc).__name__}: {exc}")
        data = [
            {k: v for k, v in item.items() if k in ("b64_json", "url", "revised_prompt")}
            for item in (out.get("data") or [])
        ]
        return self._json(200, {"data": data, "usage": out.get("usage") or {}})

    # ---- Wan 官方节点 (DashScope 原生形状) ----

    def _dashscope_error(self, code: str, message: str):
        """DashScope 的失败是 **HTTP 200 + 顶层 code/message**, output 缺席。

        节点拿不到 output 时抛的正是 f"{code} - {message}" —— 走这条能让用户看见
        我们写的中文原话; 回 404 只会变成一句「请求失败」。
        """
        print(f"[shim] {code}: {message}", flush=True)
        return self._json(200, {"request_id": uuid.uuid4().hex, "code": code, "message": message})

    def _unsold_dashscope(self, model: str):
        table = _offered()
        avail = sorted(table.values()) if table else []
        tip = ("当前可用: " + "、".join(avail)) if avail else "当前没有可用型号"
        return self._dashscope_error(
            "ModelNotOffered", f"「{model}」当前未开放。{tip}。（在节点的模型下拉里换一个）"
        )

    def _wan_video(self):
        """Wan 的文生视频/图生视频。

        Wan 系是百炼通道的原生型号 —— 节点下拉里的 wan2.7-t2v / wan2.7-i2v
        跟我们在售的 id **一字不差**, 所以型号不用改名, 只是形状要拆。
        """
        body = self._body()
        inp = body.get("input") or {}
        params = body.get("parameters") or {}
        prompt = str(inp.get("prompt") or "")
        if not prompt:
            return self._dashscope_error("InvalidParameter", "input.prompt 不能为空")
        model = _map_model(body.get("model") or "")
        payload = {"model": model, "prompt": prompt}
        resolution = _resolution_of(params)
        if resolution:
            payload["resolution"] = resolution
        try:
            duration = int(params.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration:
            payload["duration"] = duration
        # 节点把首帧塞成 data:image/png;base64,... 的 img_url, 网关收的是 image_url。
        img = str(inp.get("img_url") or "")
        if img:
            payload["image_url"] = img
        try:
            out = _gateway("POST", "/videos/generations", payload, timeout=180.0)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            print(f"[shim] Wan 建视频失败 {exc.code}: {detail}", flush=True)
            if exc.code == 404:
                return self._unsold_dashscope(model)
            return self._dashscope_error("UpstreamError", detail)
        except Exception as exc:  # noqa: BLE001
            return self._dashscope_error("GatewayUnreachable", f"{type(exc).__name__}: {exc}")
        return self._json(200, {
            "request_id": uuid.uuid4().hex,
            "output": {"task_id": out.get("id") or "", "task_status": "PENDING"},
        })

    def _wan_image(self):
        """Wan 的文生图/图生图。

        DashScope 这两条是**异步**的 (建任务 -> 轮询), 而我们网关的生图是同步的
        (一个请求 ~15 秒出图)。所以这里造一个任务号, 把同步调用甩进后台线程,
        让节点照常轮询 —— 不这么做就得让 POST 阻塞几十秒, 节点那边的超时行为
        没有文档, 不赌。
        """
        body = self._body()
        inp = body.get("input") or {}
        prompt = str(inp.get("prompt") or "")
        if not prompt:
            return self._dashscope_error("InvalidParameter", "input.prompt 不能为空")
        model = _map_model(body.get("model") or "")
        params = body.get("parameters") or {}
        payload = {"model": model, "prompt": prompt}
        if params.get("size"):
            payload["size"] = str(params["size"]).replace("*", "x")
        images = inp.get("images") or []
        if images:
            payload["image_url"] = images[0]
        task_id = _IMG_PREFIX + uuid.uuid4().hex
        with _lock:
            _IMG_TASKS[task_id] = {"task_status": "RUNNING", "urls": [], "message": ""}
        threading.Thread(target=_run_image_task, args=(task_id, payload), daemon=True).start()
        return self._json(200, {
            "request_id": uuid.uuid4().hex,
            "output": {"task_id": task_id, "task_status": "PENDING"},
        })

    def _wan_task(self, task_id: str):
        """轮询。视频任务号是网关发的 (vjob_...), 图任务号是本地造的 (shimimg-...)。

        返回体同时带上 video_url 与 results —— 视频节点读前者, 图像节点读后者,
        而这条路径两边共用; 多带一个键对 pydantic 无害, 少带一个就取不到结果。
        """
        if task_id.startswith(_IMG_PREFIX):
            with _lock:
                task = dict(_IMG_TASKS.get(task_id) or {})
            if not task:
                return self._dashscope_error("TaskNotFound", f"没有这个任务: {task_id}")
            return self._json(200, {"request_id": uuid.uuid4().hex, "output": {
                "task_id": task_id,
                "task_status": task["task_status"],
                "results": [{"url": u} for u in task["urls"]],
                "message": task["message"],
            }})

        try:
            out = _gateway("GET", f"/videos/result/{task_id}", timeout=60.0)
        except Exception as exc:  # noqa: BLE001
            # 查询失败不能报终态 —— 节点会当成任务失败, 而它可能马上就好。
            print(f"[shim] 查 Wan 任务失败, 当作在跑: {type(exc).__name__}: {exc}", flush=True)
            return self._json(200, {"request_id": uuid.uuid4().hex, "output": {
                "task_id": task_id, "task_status": "RUNNING"}})
        raw = str(out.get("task_status") or "").upper()
        status = {"SUCCESS": "SUCCEEDED", "FAIL": "FAILED"}.get(raw, "RUNNING")
        items = out.get("video_result") or []
        url = (items[0] or {}).get("url") if items else ""
        return self._json(200, {"request_id": uuid.uuid4().hex, "output": {
            "task_id": task_id,
            "task_status": status,
            "video_url": url or "",
            "results": [{"url": url}] if url else [],
            "message": str(out.get("error") or ""),
        }})

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
