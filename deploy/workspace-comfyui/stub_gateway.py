"""冒充 DSH Cloud 网关的视频端点, 用来证明 ComfyUI 那侧的编排链路。

刻意做成异步 + 需要轮询几次才成功 —— 真实厂商 (智谱/Kling/Runway) 都是这样,
如果节点只会同步取结果, 接真货时会当场翻车。
"""
import base64
import io
import json
import os
import pathlib
import shutil
import subprocess
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

# 在售清单。video 里刻意混了两个厂商: Seedance 走 Ark 形状的官方节点,
# wan2.7-* 走 DashScope 形状的官方节点 —— 两条转译路径都得被 shim_check 走到。
# 型号 -> **在售的分辨率档**。分辨率也要能不在售: wan2.7 的 480p 是厂商根本不
# 提供的那档 (生产的 media_models.json 里就是 null)。一律说成「该型号未开放」
# 会把人引去换型号, 而他真正该做的是换分辨率。
SOLD_VIDEO = {
    "doubao-seedance-2-5-260628": ("480p", "720p", "1080p"),
    "wan2.7-t2v": ("720p", "1080p"),
    "wan2.7-i2v": ("720p", "1080p"),
    "wan3.0-video": ("480p", "720p", "1080p"),
}
SOLD_IMAGE = ("gpt-image-2", "qwen-image-3.0-pro")

POLLS_BEFORE_SUCCESS = 2      # 前两次查询故意返回 PROCESSING
JOBS: dict[str, int] = {}     # job_id -> 已被查询次数
# 最后一次收到的建视频报文。自检据此断言**字段转译**真的发生了 ——
# 否则把 DashScope 的 size 折成 resolution 这段改坏了也没人会红。
LAST_VIDEO: dict = {}
# 素材中转: blob_id -> 字节。带 image/video/audio 输入的官方节点都要先走这条,
# 换一个**上游厂商能从公网取到**的 URL。
BLOBS: dict[str, bytes] = {}
# 注定被内容审核拒掉的作业号
FAILED_JOBS: set[str] = set()
LAST_IMAGE: dict = {}
PORT = 9797
# 容器里要 host.docker.internal, 宿主机自测要 localhost。
ADVERTISE = os.environ.get("ADVERTISE_HOST", "host.docker.internal")
# 按脚本自身定位, 不按 cwd —— 相对路径在换目录跑时会静默断掉 (实测踩过)。
HERE = pathlib.Path(__file__).resolve().parent
SAMPLE = HERE / "sample.mp4"


def _png_b64() -> str:
    """现造一张 8x8 的 PNG。不引第三方库 —— 桩只是为了证明节点能把 b64 解成
    张量, 图里是什么无关紧要。"""
    import struct
    import zlib

    w = h = 8
    raw = b"".join(b"\x00" + bytes([200, 60, 60] * w) for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


def ensure_sample() -> None:
    """现造一段样片, 免得往仓库里塞二进制。

    有 ffmpeg 就出真视频; 没有就手搓一个最小但合法的 MP4 容器头 —— 验证脚本
    只断言 ftyp/isom 魔数与字节数, 两者都满足。
    """
    if SAMPLE.exists():
        return
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=2",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-y", str(SAMPLE), "-loglevel", "error"],
            check=True,
        )
        return
    ftyp = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
    mdat = b"\x00\x00\x00\x08mdat"
    SAMPLE.write_bytes(ftyp + mdat)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_ua(self) -> bool:
        """节点必须带正经 User-Agent。

        生产网关前面是 Cloudflare, 它按 UA 拦机器人 —— urllib 的默认
        "Python-urllib/3.x" 吃 403 `error code: 1010`。桩前面没有 CDN, 所以这条
        在这里**永远不会自然暴露**; 2026-08-27 就是这样漏到线上的: 桩全绿, 而
        节点从没成功打通过生产网关。所以桩自己来把这道关。
        """
        ua = self.headers.get("User-Agent", "")
        if not ua or ua.lower().startswith(("python-urllib", "python-requests")):
            print(f"[stub] !! 拒绝: User-Agent 不合格 {ua!r} —— 生产上会被 CDN 403", flush=True)
            self._json(403, {"error": "bad user agent", "ua": ua})
            return False
        return True

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_PUT(self):
        if not self._check_ua():
            return
        if "/media/uploads/" not in self.path:
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0))
        bid = self.path.rsplit("/", 1)[-1]
        BLOBS[bid] = self.rfile.read(n) if n else b""
        print(f"[stub] 收到素材 {bid} {len(BLOBS[bid])} 字节", flush=True)
        self._json(200, {"id": bid, "bytes": len(BLOBS[bid])})

    def do_POST(self):
        if not self._check_ua():
            return
        if self.path.endswith("/media/uploads"):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            ctype = str(payload.get("content_type") or "")
            # 与生产同款: 只收媒体, 别的一律 415
            if not ctype.startswith(("image/", "video/", "audio/")):
                print(f"[stub] 拒收类型 {ctype!r} -> 415", flush=True)
                return self._json(415, {"error": {"message": "unsupported media type"}})
            bid = uuid.uuid4().hex
            return self._json(200, {
                "id": bid,
                "upload_url": f"http://{ADVERTISE}:{PORT}/llm/v1/media/uploads/{bid}",
                "download_url": f"http://{ADVERTISE}:{PORT}/llm/v1/media/blobs/{bid}",
            })
        if self.path.endswith("/images/generations"):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            print(f"[stub] 收到生图 model={payload.get('model')} "
                  f"prompt={payload.get('prompt')!r}", flush=True)
            LAST_IMAGE.clear()
            LAST_IMAGE.update(payload)
            # 「200 但没出图」是真会发生的 —— 内容审核拦下来时上游就这么回。
            # 桩得能造出这一幕, 否则垫片里那条空结果分支永远没人走过。
            if payload.get("prompt") == "__noimage__":
                print("[stub] 造一次「200 但没出图」", flush=True)
                return self._json(200, {"created": 0, "data": [], "usage": {}})
            if payload.get("model") not in SOLD_IMAGE:
                print(f"[stub] 图像型号 {payload.get('model')!r} 未在售 -> 404", flush=True)
                return self._json(404, {"error": {"message": "model not offered"}})
            return self._json(200, {
                "created": 0,
                "data": [{"url": None, "b64_json": _png_b64(), "revised_prompt": None}],
                "usage": {"input_tokens": 8, "output_tokens": 196},
                "credits": 15,
            })
        if not self.path.endswith("/videos/generations"):
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        LAST_VIDEO.clear()
        LAST_VIDEO.update(payload)
        auth = self.headers.get("Authorization", "")
        print(f"[stub] 收到作业 model={payload.get('model')} "
              f"prompt={payload.get('prompt')!r} 鉴权={'有' if auth else '无'}", flush=True)
        # 只卖 /media/models 里列的那些 —— 与生产同款: 未定价 = 404。
        # 错误措辞照抄生产 (media.py 的 create_video), 垫片会把它原样转给用户。
        model = payload.get("model")
        if model not in SOLD_VIDEO:
            print(f"[stub] 型号 {model!r} 未在售 -> 404", flush=True)
            return self._json(404, {"error": {"message": f"Model '{model}' is not offered."}})
        res = payload.get("resolution") or "720p"
        if res not in SOLD_VIDEO[model]:
            print(f"[stub] {model} 的 {res} 档未在售 -> 404", flush=True)
            return self._json(404, {"error": {"message":
                f"Model '{model}' at {res} is not offered for video generation."}})
        # 「跑完了但被内容审核拒绝」是**正常结果**, 线上真会遇到 (2026-08-28:
        # 提示词点了真实人物, 上游回 "suspected to include real human faces")。
        # 桩得能造出这一幕, 否则垫片里那条翻译永远没人走过。
        job_id = uuid.uuid4().hex[:12]
        if "__moderated__" in str(payload.get("prompt") or ""):
            FAILED_JOBS.add(job_id)
        JOBS[job_id] = 0
        self._json(200, {"id": job_id, "model": payload.get("model"),
                         "video_result": None, "task_status": "PROCESSING",
                         "request_id": job_id})

    def do_GET(self):
        if not self._check_ua():
            return
        if self.path.endswith("/_debug/last-video"):
            return self._json(200, LAST_VIDEO)
        if self.path.endswith("/_debug/last-image"):
            return self._json(200, LAST_IMAGE)
        if self.path.endswith("/media/models"):
            # 垫片靠这份清单把官方节点发来的厂商公开名 (dreamina-…) 映射到我们
            # 在售的型号 (doubao-…)。返回的 id 刻意用 doubao- 前缀, 好让
            # shim_check 里那个 dreamina- 的请求真的走一次映射。
            return self._json(200, {
                "video": [{"id": m, "name": m, "resolutions": list(rs)}
                          for m, rs in SOLD_VIDEO.items()],
                "image": [{"id": m, "name": m} for m in SOLD_IMAGE],
            })
        if "/media/blobs/" in self.path:
            bid = self.path.rsplit("/", 1)[-1]
            if bid not in BLOBS:
                return self._json(404, {"error": "no such blob"})
            return self._send(200, BLOBS[bid], "image/png")
        if self.path.endswith("/sample.mp4"):
            return self._send(200, SAMPLE.read_bytes(), "video/mp4")
        if "/videos/result/" in self.path:
            job_id = self.path.rsplit("/", 1)[-1]
            if job_id not in JOBS:
                return self._json(404, {"error": "unknown job"})
            JOBS[job_id] += 1
            n = JOBS[job_id]
            if n <= POLLS_BEFORE_SUCCESS:
                print(f"[stub] 轮询 #{n} -> PROCESSING", flush=True)
                return self._json(200, {"id": job_id, "task_status": "PROCESSING"})
            if job_id in FAILED_JOBS:
                print(f"[stub] 轮询 #{n} -> FAIL (内容审核)", flush=True)
                return self._json(200, {
                    "id": job_id, "task_status": "FAIL",
                    "error": "The output content is suspected to include real human faces.",
                })
            print(f"[stub] 轮询 #{n} -> SUCCESS", flush=True)
            return self._json(200, {
                "id": job_id, "task_status": "SUCCESS",
                "video_result": [{
                    "url": f"http://{ADVERTISE}:{PORT}/sample.mp4",
                    "cover_image_url": "",
                }],
            })
        self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ensure_sample()
    print(f"[stub] 样片 {SAMPLE.name} {SAMPLE.stat().st_size} 字节", flush=True)
    print(f"[stub] 监听 :{PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
