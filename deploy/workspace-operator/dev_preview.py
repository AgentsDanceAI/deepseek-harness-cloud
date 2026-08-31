"""本地预览: 在一个进程里同时跑 Operator 和一个假网关。

跑法: python deploy/workspace-operator/dev_preview.py   -> http://127.0.0.1:8710

只为看界面和跑通链路用, **不进镜像**。假网关按固定剧本回答, 所以每次点开看到的
时间线都一样 —— 界面改动能一眼比出差别。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PORT = 8710
_work = pathlib.Path(tempfile.mkdtemp(prefix="operator-dev-"))
# env 必须在 import app 之前设好 —— agent 在导入时就读它们。
os.environ.update(
    {
        "OPERATOR_WORKDIR": str(_work),
        "DSH_GATEWAY_BASE": f"http://127.0.0.1:{PORT}/stub/llm/v1",
        "DSH_CLOUD_TOKEN": "dev-token",
        "DSH_DEFAULT_MODEL": "deepseek-v4-flash",
        "DSH_MODELS": "deepseek-v4-flash claude-sonnet-5 gpt-5.6-terra",
    }
)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402

from app.main import app as operator_app  # noqa: E402

root = FastAPI()


@root.post("/stub/llm/v1/chat/completions")
async def stub(request: Request) -> StreamingResponse:
    body = await request.json()
    used_tool = any(m.get("role") == "tool" for m in body["messages"])

    def frame(d: dict) -> str:
        return f"data: {json.dumps(d)}\n\n"

    def gen():
        if used_tool:
            for piece in ("看到了。", "工作目录里现在有 ", "note.txt。"):
                yield frame({"choices": [{"delta": {"content": piece}}]})
            yield frame({"usage": {"total_tokens": 412}})
        else:
            yield frame({"choices": [{"delta": {"content": "我先看一眼工作目录。"}}]})
            yield frame(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "shell"}}
                ]}}]}
            )
            yield frame(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '{"command": "echo hi > note.txt && ls -la"}'}}
                ]}}]}
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


root.mount("/", operator_app)

if __name__ == "__main__":
    import uvicorn

    print(f"Operator 预览: http://127.0.0.1:{PORT}   (工作目录 {_work})")
    uvicorn.run(root, host="127.0.0.1", port=PORT, log_level="warning")
