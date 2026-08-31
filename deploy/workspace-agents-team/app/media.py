"""出图 / 出片 / 拼片 —— 让剧组这几个工位真能干活。

DSH Cloud 的媒体端点与对话端点在**同一个网关、同一把 token** 上, 所以这里不写
任何后端: 直接打 /v1/images/generations 与 /v1/videos/generations 就是了。

设计上只有一条硬原则 —— **产物必须落成工作区里的文件**, 而不是塞回对话里。
理由是这个产品的骨架就建立在"结构化中间产物"上: 角色图要被后面十几个镜头反复
引用, 镜头片段要被剪辑师拼接, 这些东西活在文件系统里才能被别的 agent 用
read_file 读到、被用户在文件树里看到、被单独重做而不牵动其它。塞回对话等于
让下游只能拿到一段转述。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

import httpx

#: 不 import tools —— 它会反过来 import 本模块 (工具表合并), 成环。WORKDIR 本来
#: 就只是个环境变量派生的常量, 各算各的即可, 两边同源同值。
WORKDIR = Path(os.environ.get("AGENTS_TEAM_WORKDIR", "/workspace"))

GATEWAY_BASE = os.environ.get("DSH_GATEWAY_BASE", "").rstrip("/")
GATEWAY_TOKEN = os.environ.get("DSH_CLOUD_TOKEN", "")
IMAGE_MODEL = os.environ.get("DSH_IMAGE_MODEL", "")
VIDEO_MODEL = os.environ.get("DSH_VIDEO_MODEL", "")

#: 出片是整条流水线里最慢也最贵的一步, 轮询上限给足 (上游 30 秒的片子实测要几分钟)。
VIDEO_POLL_TIMEOUT_S = float(os.environ.get("DSH_VIDEO_POLL_TIMEOUT", "900"))
VIDEO_POLL_INTERVAL_S = 6.0


def _headers() -> dict:
    return {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _unconfigured() -> str | None:
    if not GATEWAY_BASE or not GATEWAY_TOKEN:
        return "工作台没有配置 DSH 网关地址或令牌, 生成类工具不可用。"
    return None


def _safe_rel(path: str) -> Path:
    """把模型给的相对路径钉在 /workspace 里 —— 它偶尔会写 ../ 或绝对路径。"""
    p = (WORKDIR / path).resolve()
    if not str(p).startswith(str(WORKDIR.resolve())):
        raise ValueError(f"路径越界: {path}")
    return p


def _err_text(r: httpx.Response) -> str:
    try:
        d = r.json()
        return str((d.get("error") or {}).get("message") or d)[:300]
    except Exception:  # noqa: BLE001
        return r.text[:300]


async def generate_image(prompt: str, path: str, size: str = "", model: str = "") -> tuple[str, str]:
    """出一张图并存到工作区。角色四视图、场景参考、分镜草图都走它。"""
    bad = _unconfigured()
    if bad:
        return bad, "出图未配置"
    out = _safe_rel(path)
    body = {"model": model or IMAGE_MODEL, "prompt": prompt}
    if size:
        body["size"] = size
    if not body["model"]:
        return "没有可用的图像模型 (DSH_IMAGE_MODEL 未配置)。", "出图无模型"
    async with _client(300) as c:
        r = await c.post(f"{GATEWAY_BASE}/v1/images/generations", headers=_headers(), json=body)
    if r.status_code >= 400:
        return f"出图失败 HTTP {r.status_code}: {_err_text(r)}", "出图失败"
    data = (r.json() or {}).get("data") or []
    if not data:
        return "上游没有返回图片。", "出图无结果"
    item = data[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    if item.get("b64_json"):
        out.write_bytes(base64.b64decode(item["b64_json"]))
    elif item.get("url"):
        async with _client(300) as c:
            got = await c.get(item["url"])
        if got.status_code >= 400:
            return f"图片下载失败 HTTP {got.status_code}", "出图下载失败"
        out.write_bytes(got.content)
    else:
        return "上游返回里既没有 url 也没有 b64_json。", "出图无结果"
    rel = out.relative_to(WORKDIR)
    kb = out.stat().st_size // 1024
    return f"已出图并存到 {rel} ({kb} KB)。下游可以用这个路径当参考图。", f"出图 {rel.name}"


async def generate_video(prompt: str, path: str, duration: int = 5, resolution: str = "720P",
                         ratio: str = "", image: str = "", model: str = "") -> tuple[str, str]:
    """出一段视频并存到工作区 (提交 → 轮询 → 下载, 一次调用走完)。

    做成同步是刻意的: 让模型"提交完自己去轮询"会让它在等待期间反复调工具、
    把上下文塞满无用的 pending 状态, 而且它经常忘了回来取。
    """
    bad = _unconfigured()
    if bad:
        return bad, "出片未配置"
    out = _safe_rel(path)
    body: dict = {
        "model": model or VIDEO_MODEL,
        "prompt": prompt,
        "duration": int(duration),
        "resolution": resolution,
    }
    if not body["model"]:
        return "没有可用的视频模型 (DSH_VIDEO_MODEL 未配置)。", "出片无模型"
    if ratio:
        body["ratio"] = ratio
    if image:
        # 首帧参考图: 上传进资产库换一个可被上游取到的 url
        url, why = await _upload_blob(_safe_rel(image))
        if not url:
            return f"参考图上传失败: {why}", "参考图上传失败"
        body["image_url"] = url
    async with _client(120) as c:
        r = await c.post(f"{GATEWAY_BASE}/v1/videos/generations", headers=_headers(), json=body)
    if r.status_code >= 400:
        return f"出片提交失败 HTTP {r.status_code}: {_err_text(r)}", "出片提交失败"
    job = (r.json() or {}).get("id") or ""
    if not job:
        return "上游没有返回作业 id。", "出片无作业"

    deadline = time.time() + VIDEO_POLL_TIMEOUT_S
    url = ""
    while time.time() < deadline:
        await asyncio.sleep(VIDEO_POLL_INTERVAL_S)
        async with _client(60) as c:
            g = await c.get(f"{GATEWAY_BASE}/v1/videos/result/{job}", headers=_headers())
        if g.status_code >= 400:
            return f"查询作业失败 HTTP {g.status_code}: {_err_text(g)}", "出片查询失败"
        d = g.json() or {}
        st = str(d.get("task_status") or "")
        if st == "SUCCESS":
            url = ((d.get("video_result") or [{}])[0] or {}).get("url") or ""
            break
        if st == "FAIL":
            return f"出片失败: {str(d.get('error') or '')[:300]}", "出片失败"
    if not url:
        return (f"出片超时 (等了 {int(VIDEO_POLL_TIMEOUT_S)} 秒还没好), 作业 id {job} — "
                f"可以稍后用 shell 手动查询, 不必重新下单。"), "出片超时"

    out.parent.mkdir(parents=True, exist_ok=True)
    async with _client(600) as c, c.stream("GET", url) as resp:
        if resp.status_code >= 400:
            return f"视频下载失败 HTTP {resp.status_code}", "出片下载失败"
        with out.open("wb") as f:
            async for chunk in resp.aiter_bytes():
                f.write(chunk)
    rel = out.relative_to(WORKDIR)
    mb = out.stat().st_size / 1024 / 1024
    return f"已出片并存到 {rel} ({mb:.1f} MB, {duration}秒 {resolution})。", f"出片 {rel.name}"


async def _upload_blob(p: Path) -> tuple[str, str]:
    """把本地文件放进 DSH 资产库, 换一个上游能取到的 url。"""
    if not p.exists():
        return "", f"文件不存在: {p}"
    async with _client(60) as c:
        r = await c.post(f"{GATEWAY_BASE}/v1/media/uploads", headers=_headers(),
                         json={"content_type": "image/png", "size": p.stat().st_size})
        if r.status_code >= 400:
            return "", f"HTTP {r.status_code}: {_err_text(r)}"
        d = r.json() or {}
        put, get = d.get("upload_url") or "", d.get("url") or ""
        if not put:
            return "", "资产库没有返回上传地址"
        up = await c.put(put, content=p.read_bytes(),
                         headers={**_headers(), "Content-Type": "image/png"})
        if up.status_code >= 400:
            return "", f"上传 HTTP {up.status_code}"
    return get, ""


_TIME_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


async def concat_videos(clips: list[str], path: str, audio: str = "") -> tuple[str, str]:
    """把若干片段按顺序拼成一条成片 (剪辑师的活)。

    用 ffmpeg 的 concat demuxer 而不是 filter: 片段都出自同一个模型同一档参数,
    编码参数一致, 直接 -c copy 拼接是无损且秒级的; 走 filter 要全部重编码,
    十几段就是几分钟, 还掉一次画质。
    """
    if not clips:
        return "没有给任何片段。", "拼片无输入"
    outp = _safe_rel(path)
    paths: list[Path] = []
    for c in clips:
        p = _safe_rel(c)
        if not p.exists():
            return f"片段不存在: {c}", "拼片缺片段"
        paths.append(p)
    outp.parent.mkdir(parents=True, exist_ok=True)
    listing = outp.parent / ".concat.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in paths), encoding="utf-8")

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing)]
    if audio:
        ap = _safe_rel(audio)
        if not ap.exists():
            return f"配乐文件不存在: {audio}", "拼片缺配乐"
        cmd += ["-i", str(ap), "-map", "0:v", "-map", "1:a", "-shortest", "-c:v", "copy"]
    else:
        cmd += ["-c", "copy"]
    cmd.append(str(outp))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    listing.unlink(missing_ok=True)
    if proc.returncode != 0 or not outp.exists():
        tail = (err or b"").decode("utf-8", "replace")[-400:]
        return f"拼片失败: {tail}", "拼片失败"
    rel = outp.relative_to(WORKDIR)
    mb = outp.stat().st_size / 1024 / 1024
    return f"已拼成 {rel} ({len(paths)} 段, {mb:.1f} MB)。", f"拼片 {rel.name}"


async def media_models() -> tuple[str, str]:
    """看看这个工作台能用哪些图/视频模型 (含分辨率与单价)。"""
    bad = _unconfigured()
    if bad:
        return bad, "查目录未配置"
    async with _client(60) as c:
        r = await c.get(f"{GATEWAY_BASE}/v1/media/models", headers=_headers())
    if r.status_code >= 400:
        return f"查询失败 HTTP {r.status_code}: {_err_text(r)}", "查目录失败"
    return json.dumps(r.json(), ensure_ascii=False)[:4000], "查了媒体模型目录"


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "生成一张图片并**存进工作区文件**。角色定妆图/四视图、场景参考图、"
                "道具图、分镜草图都用它。出图便宜且快, 视频之前把视觉资产先定下来, "
                "后面十几个镜头的一致性都锚在这些图上 —— 别跳过这一步直接出片。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "画面描述, 越具体越好"},
                    "path": {"type": "string", "description": "存到哪 (相对 /workspace), 如 项目/角色/华强.png"},
                    "size": {"type": "string", "description": "如 1024x1024; 省略用默认"},
                    "model": {"type": "string", "description": "省略用工作台默认图像模型"},
                },
                "required": ["prompt", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": (
                "生成一段视频并存进工作区 (提交+等待+下载一次做完, 可能要等几分钟)。"
                "这是全流程里**最慢最贵**的一步 —— 调用之前务必确认镜头提示词已经过"
                "用户确认, 不要拿着没审过的分镜一口气把十几个镜头全跑掉。"
                "传 image 可以用一张角色/场景图当首帧, 这是保人物一致性的主要手段。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "镜头描述: 画面内容 + 运镜 + 情绪"},
                    "path": {"type": "string", "description": "存到哪, 如 项目/片段/01.mp4"},
                    "duration": {"type": "number", "description": "秒; 省略 5"},
                    "resolution": {"type": "string", "description": "480P/720P/1080P; 省略 720P"},
                    "ratio": {"type": "string", "description": "如 16:9 / 9:16"},
                    "image": {"type": "string", "description": "首帧参考图的工作区路径"},
                    "model": {"type": "string", "description": "省略用工作台默认视频模型"},
                },
                "required": ["prompt", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "concat_videos",
            "description": (
                "把多段视频按给定顺序拼成一条成片, 可选叠一条音轨。剪辑师的活。"
                "片段参数一致时是无损秒拼。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "clips": {"type": "array", "items": {"type": "string"},
                              "description": "片段路径, 按成片顺序"},
                    "path": {"type": "string", "description": "成片存到哪, 如 项目/成片.mp4"},
                    "audio": {"type": "string", "description": "可选配乐/配音文件路径"},
                },
                "required": ["clips", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_models",
            "description": "查这个工作台可用的图像/视频模型、分辨率与单价。挑模型或估成本时用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

HANDLERS = {
    "generate_image": lambda a: generate_image(
        a["prompt"], a["path"], a.get("size", ""), a.get("model", "")),
    "generate_video": lambda a: generate_video(
        a["prompt"], a["path"], int(a.get("duration") or 5), a.get("resolution") or "720P",
        a.get("ratio", ""), a.get("image", ""), a.get("model", "")),
    "concat_videos": lambda a: concat_videos(
        list(a.get("clips") or []), a["path"], a.get("audio", "")),
    "media_models": lambda a: media_models(),
}
