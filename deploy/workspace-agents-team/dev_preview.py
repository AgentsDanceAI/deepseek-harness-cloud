"""本地预览: 一个进程里同时跑 Operator 和一个假网关。

跑法: python deploy/workspace-agents-team/dev_preview.py   -> http://127.0.0.1:8710

假网关按成员给不同回答, 而且**故意让两个成员一快一慢** —— 界面上要看得出它们是
同时在说, 而不是排队。只为看界面用, 不进镜像。
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PORT = 8710
_work = pathlib.Path(tempfile.mkdtemp(prefix="agents-team-dev-"))
# env 必须在 import app 之前设好 —— agent 在导入时就读它们。
os.environ.update(
    {
        "AGENTS_TEAM_WORKDIR": str(_work),
        "DSH_GATEWAY_BASE": f"http://127.0.0.1:{PORT}/stub/llm/v1",
        "DSH_CLOUD_TOKEN": "dev-token",
        "DSH_DEFAULT_MODEL": "deepseek-v4-flash",
        "DSH_MODELS": "deepseek-v4-flash claude-sonnet-5 gpt-5.6-terra",
    }
)

from app.main import app as team_app
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

root = FastAPI()

SCRIPT = {
    "阿做": [
        ("先看看环境。", 0.1),
        ("__TOOL__", 0.2),
        ("装好了, 目录里有 note.txt。", 0.3),
    ],
    "阿查": [("我并行核一下。", 0.05), ("核完了, 没发现问题。", 0.9)],
    "阿谋": [("分三步: 先看环境, 再动手, 最后核对。", 0.15)],
}


@root.post("/stub/llm/v1/chat/completions")
async def stub(request: Request) -> StreamingResponse:
    body = await request.json()
    head = body["messages"][0]["content"]
    who = next((n for n in SCRIPT if f"名字是 {n}" in head), "阿做")
    used_tool = any(m.get("role") == "tool" for m in body["messages"])

    def frame(d: dict) -> str:
        return f"data: {json.dumps(d)}\n\n"

    # 工具跑完那一轮**只说工具之后的话**。剧本从头重放的话, 工具前那句会再说一遍 ——
    # 而主循环本来就该把一轮里前后说的话都收进去, 于是界面上看着像它复读了。
    script = SCRIPT[who]
    if used_tool:
        cut = next((i for i, (p, _) in enumerate(script) if p == "__TOOL__"), -1)
        script = script[cut + 1 :]

    async def gen():
        for piece, delay in script:
            await asyncio.sleep(delay)
            if piece == "__TOOL__":
                yield frame(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "c1",
                                            "function": {"name": "shell"},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
                yield frame(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {
                                                "arguments": '{"command": "echo hi > note.txt && ls"}'
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
                break
            yield frame({"choices": [{"delta": {"content": piece}}]})
        yield frame({"usage": {"total_tokens": 128}})
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


root.mount("/", team_app)

if __name__ == "__main__":
    import uvicorn

    print(f"Operator 预览: http://127.0.0.1:{PORT}   (工作目录 {_work})")
    uvicorn.run(root, host="127.0.0.1", port=PORT, log_level="warning")
