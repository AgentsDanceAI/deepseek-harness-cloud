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

POLLS_BEFORE_SUCCESS = 2      # 前两次查询故意返回 PROCESSING
JOBS: dict[str, int] = {}     # job_id -> 已被查询次数
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

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_POST(self):
        if self.path.endswith("/images/generations"):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            print(f"[stub] 收到生图 model={payload.get('model')} "
                  f"prompt={payload.get('prompt')!r}", flush=True)
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
        auth = self.headers.get("Authorization", "")
        print(f"[stub] 收到作业 model={payload.get('model')} "
              f"prompt={payload.get('prompt')!r} 鉴权={'有' if auth else '无'}", flush=True)
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = 0
        self._json(200, {"id": job_id, "model": payload.get("model"),
                         "video_result": None, "task_status": "PROCESSING",
                         "request_id": job_id})

    def do_GET(self):
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
