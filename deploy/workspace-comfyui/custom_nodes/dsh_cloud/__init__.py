"""DSH Cloud 的视频编排节点。

设计要点: 这个节点**不认识任何一家视频厂商**。它只对着 DSH Cloud 自己的网关
说话 (与 /llm/v1 聊天通道同构), 由网关去适配智谱 / Kling / Runway / 自建 GPU。
理由是计费 —— 节点直连厂商就等于把差价让出去, 且换供应商要重发镜像。

契约 (异步作业, 三家主流厂商都是这个形状):
    POST {base}/videos/generations  -> {"id": ..., "task_status": "PROCESSING"}
    GET  {base}/videos/result/{id}  -> {"task_status": "SUCCESS",
                                        "video_result": [{"url": ...}]}
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("DSH_CLOUD_VIDEO_BASE", "https://dshcloud.online/llm/v1").rstrip("/")
TOKEN = os.environ.get("DSH_CLOUD_TOKEN", "")
POLL_TIMEOUT_S = float(os.environ.get("DSH_CLOUD_VIDEO_TIMEOUT_S", "600"))
POLL_INTERVAL_S = float(os.environ.get("DSH_CLOUD_VIDEO_POLL_S", "3"))


# 网关前面有 Cloudflare, 它按 **User-Agent** 拦机器人 —— urllib 的默认
# "Python-urllib/3.x" 会直接吃 403 `error code: 1010`, 报文里不提 UA, 只说
# Forbidden。2026-08-27 实测: 默认 UA 403, 换成下面这个就正常走到网关。
# 这条在桩网关上永远测不出来 (桩前面没有 CDN), 所以桩改成校验 UA 存在。
_USER_AGENT = "DSHCloud-ComfyUI/1.0 (+https://dshcloud.online)"


def _request(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", _USER_AGENT)
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


# --- 在售模型清单 --------------------------------------------------------------
# 把"模型"做成下拉而不是自由文本框 —— 没人该去背
# doubao-seedance-2-0-mini-260615 这种 id。
#
# INPUT_TYPES() 在 ComfyUI **启动时**被调用, 所以这里必须短超时 + 缓存: 网关慢
# 一下就把整个工作台的启动拖住, 而用户看到的只是"一直起不来"。取不到就回落成
# 文本框, 节点照常可用。
_MODELS_CACHE: dict | None = None
_MODELS_FAILED_AT: float = 0.0
_RETRY_AFTER_S = 30.0


def _offered() -> dict:
    """在售清单。**失败不入缓存** —— 只缓存成功的结果。

    ComfyUI 启动时 ECI 实例的 EIP 未必已经绑好, 此刻取不到是正常的。把失败也
    缓存起来就等于一次抖动永久锁死: 之后按多少次「刷新节点」都还是文本框。
    现在失败后 30 秒可重试, 用户按一下刷新就好了。
    """
    global _MODELS_CACHE, _MODELS_FAILED_AT
    if _MODELS_CACHE is None:
        if time.time() - _MODELS_FAILED_AT < _RETRY_AFTER_S:
            return {}
        url = f"{BASE}/media/models"
        try:
            _MODELS_CACHE = _request("GET", url)
            counts = {k: len(v) for k, v in (_MODELS_CACHE or {}).items()}
            print(f"[dsh_cloud] 模型清单已加载 {counts} <- {url}", flush=True)
        except Exception as exc:                                      # noqa: BLE001
            # 一定要喊出来。静默回落的表现只是"下拉变成了文本框", 没有任何线索
            # 指向真实原因 (令牌无效? 网关不可达? 灰度闸挡了?) —— 2026-08-27
            # 我就是在这里瞎猜了很久。
            detail = ""
            body = getattr(exc, "read", None)
            if callable(body):
                try:
                    detail = f" 报文={body()[:200]!r}"
                except Exception:                                     # noqa: BLE001
                    detail = ""
            print(
                f"[dsh_cloud] !! 取模型清单失败, 模型输入回落成文本框: "
                f"{type(exc).__name__}: {exc}{detail}  url={url} "
                f"token={'有' if TOKEN else '无'}",
                flush=True,
            )
            _MODELS_FAILED_AT = time.time()
            return {}
    return _MODELS_CACHE


def _choices(kind: str) -> list:
    """下拉选项。空列表会让 ComfyUI 报错, 所以取不到时返回 None 让调用方回落。"""
    items = (_offered() or {}).get(kind) or []
    return [m["id"] for m in items if m.get("id")] or None


def _model_input(kind: str, fallback: str):
    choices = _choices(kind)
    return (choices,) if choices else ("STRING", {"default": fallback})


def _resolution_input():
    for m in (_offered() or {}).get("video") or []:
        if m.get("resolutions"):
            return (m["resolutions"],)
    return ("STRING", {"default": "480p"})


class DSHCloudVideo:
    """文/图生视频 —— 算力在远端, 本容器只做编排。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "一只柴犬在雪地里奔跑，慢镜头，电影感"}),
                "model": _model_input("video", "doubao-seedance-2-0-mini-260615"),
                "resolution": _resolution_input(),
                "duration": ("INT", {"default": 5, "min": 1, "max": 30}),
            },
            "optional": {
                "image_url": ("STRING", {"default": ""}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("video_url", "local_path")
    FUNCTION = "generate"
    CATEGORY = "DSH Cloud"
    # 必须: ComfyUI 只执行通向输出节点的分支, 不标这个的话整张图会被当成
    # 死枝跳过 —— 提交返回 200、/history 里空空如也, 极难排查。
    OUTPUT_NODE = True

    def generate(self, prompt, model, resolution, duration, image_url=""):
        payload = {"model": model, "prompt": prompt,
                   "resolution": resolution, "duration": duration}
        if image_url:
            payload["image_url"] = image_url

        job = _request("POST", f"{BASE}/videos/generations", payload)
        job_id = job.get("id")
        if not job_id:
            raise RuntimeError(f"网关没有返回作业 id: {job}")

        deadline = time.time() + POLL_TIMEOUT_S
        while True:
            # 轮询期间的**瞬时错误不能让作业作废** —— 钱在提交那一刻就扣掉了。
            # 2026-08-27 实测: 服务端重新部署的十几秒里节点收到一个 502, 直接
            # 放弃了一条已付费的 1080p 作业 (600 积分), 而上游其实已经出片。
            # 5xx 与网络错误一律重试到超时为止; 4xx 是真错 (作业不存在/无权),
            # 重试没有意义, 照旧抛出。
            try:
                result = _request("GET", f"{BASE}/videos/result/{job_id}")
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or time.time() > deadline:
                    raise
                print(f"[dsh_cloud] 轮询遇到 {exc.code}, {POLL_INTERVAL_S}s 后重试", flush=True)
                time.sleep(POLL_INTERVAL_S)
                continue
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if time.time() > deadline:
                    raise
                print(f"[dsh_cloud] 轮询网络错误 {exc}, 重试", flush=True)
                time.sleep(POLL_INTERVAL_S)
                continue
            status = (result.get("task_status") or "").upper()
            if status == "SUCCESS":
                break
            if status in ("FAIL", "FAILED", "ERROR"):
                raise RuntimeError(f"生成失败: {result}")
            if time.time() > deadline:
                raise RuntimeError(f"等待超时 ({POLL_TIMEOUT_S}s), 末次状态 {status}")
            time.sleep(POLL_INTERVAL_S)

        items = result.get("video_result") or []
        if not items or not items[0].get("url"):
            raise RuntimeError(f"成功了但没有 url: {result}")
        url = items[0]["url"]

        # 落进 ComfyUI 的 output 目录 —— 用户的 NAS 卷挂在这儿, 回收后还在。
        out_dir = os.environ.get("COMFY_OUTPUT_DIR", "/opt/ComfyUI/output")
        os.makedirs(out_dir, exist_ok=True)
        local_path = os.path.join(out_dir, f"dshcloud-{job_id}.mp4")
        dl = urllib.request.Request(url)
        dl.add_header("User-Agent", _USER_AGENT)  # 产物 URL 也可能在 CDN 后面
        with urllib.request.urlopen(dl, timeout=300) as src, open(local_path, "wb") as dst:
            dst.write(src.read())

        return {"ui": {"text": [local_path]}, "result": (url, local_path)}


NODE_CLASS_MAPPINGS = {"DSHCloudVideo": DSHCloudVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"DSHCloudVideo": "DSH Cloud 生视频"}


class DSHCloudImage:
    """文生图 —— 同步出图, 返回真正的 IMAGE 张量。

    返回张量而不是文件路径, 是为了让它成为 ComfyUI 里的一等节点: 能直接接
    SaveImage / PreviewImage / 各种后处理。返回路径的话它就是个死胡同, 用户
    只能自己去翻文件。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "一只柴犬在雪地里奔跑，电影感，柔和光线"}),
                "model": _model_input("image", "gpt-image-2"),
                "size": (["1024x1024", "1536x1024", "1024x1536"],),
                "n": ("INT", {"default": 1, "min": 1, "max": 4}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "generate"
    CATEGORY = "DSH Cloud"

    def generate(self, prompt, model, size, n):
        import base64
        import io

        import numpy as np
        import torch
        from PIL import Image

        result = _request(
            "POST", f"{BASE}/images/generations",
            {"model": model, "prompt": prompt, "size": size, "n": n},
        )
        items = result.get("data") or []
        if not items:
            raise RuntimeError(f"网关没有返回图片: {result}")

        frames = []
        for item in items:
            if item.get("b64_json"):
                raw = base64.b64decode(item["b64_json"])
            elif item.get("url"):
                dl = urllib.request.Request(item["url"])
                dl.add_header("User-Agent", _USER_AGENT)
                with urllib.request.urlopen(dl, timeout=120) as src:
                    raw = src.read()
            else:
                continue
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            frames.append(np.array(img, dtype=np.float32) / 255.0)

        if not frames:
            raise RuntimeError("返回里既没有 b64_json 也没有 url")
        # ComfyUI 的 IMAGE 是 [B,H,W,C] 的 float32, 取值 0..1。尺寸不一致时只取
        # 第一张 —— 拼一个参差不齐的批次会在下游炸得莫名其妙。
        if len({f.shape for f in frames}) > 1:
            frames = frames[:1]
        return (torch.from_numpy(np.stack(frames)),)


NODE_CLASS_MAPPINGS["DSHCloudImage"] = DSHCloudImage
NODE_DISPLAY_NAME_MAPPINGS["DSHCloudImage"] = "DSH Cloud 生图"
