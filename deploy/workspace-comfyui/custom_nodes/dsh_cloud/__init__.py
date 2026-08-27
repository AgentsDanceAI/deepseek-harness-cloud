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


def _request(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


class DSHCloudVideo:
    """文/图生视频 —— 算力在远端, 本容器只做编排。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "一只猫在雪地里奔跑"}),
                "model": ("STRING", {"default": "cogvideox-3"}),
                "size": ("STRING", {"default": "1920x1080"}),
                "duration": ("INT", {"default": 5, "min": 1, "max": 30}),
                "fps": ("INT", {"default": 30, "min": 1, "max": 60}),
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

    def generate(self, prompt, model, size, duration, fps, image_url=""):
        payload = {"model": model, "prompt": prompt, "size": size,
                   "duration": duration, "fps": fps}
        if image_url:
            payload["image_url"] = image_url

        job = _request("POST", f"{BASE}/videos/generations", payload)
        job_id = job.get("id")
        if not job_id:
            raise RuntimeError(f"网关没有返回作业 id: {job}")

        deadline = time.time() + POLL_TIMEOUT_S
        while True:
            result = _request("GET", f"{BASE}/videos/result/{job_id}")
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
        with urllib.request.urlopen(url, timeout=300) as src, open(local_path, "wb") as dst:
            dst.write(src.read())

        return {"ui": {"text": [local_path]}, "result": (url, local_path)}


NODE_CLASS_MAPPINGS = {"DSHCloudVideo": DSHCloudVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"DSHCloudVideo": "DSH Cloud 生视频"}
