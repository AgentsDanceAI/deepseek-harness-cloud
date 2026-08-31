"""Operator 主循环的自检: 假造一个网关, 把一整轮真跑一遍。

跑法: python deploy/workspace-operator/verify.py

**故意让两个工具调用的分片交错到达**。这是流式工具调用最容易写错的地方: 按到达
顺序拼接的实现会把 A 的参数接到 B 上, 而拼出来的 JSON 照样能解析成功 —— 于是
"参数对不上却完全不报错", 表现是智能体读了个它没要读的文件, 然后基于错内容往下做。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import httpx  # noqa: E402

from app import agent, tools  # noqa: E402


def sse(chunks: list[dict]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def delta(**d) -> dict:
    return {"choices": [{"delta": d}]}


def tool_frag(index: int, *, cid: str = "", name: str = "", args: str = "") -> dict:
    fn = {}
    if name:
        fn["name"] = name
    if args:
        fn["arguments"] = args
    part = {"index": index, "function": fn}
    if cid:
        part["id"] = cid
    return {"choices": [{"delta": {"tool_calls": [part]}}]}


ROUND1 = sse([
    delta(content="我先看一下。"),
    # 两个调用交错送达 —— index 才是归位依据, 到达顺序不是。
    tool_frag(0, cid="call_a", name="shell"),
    tool_frag(1, cid="call_b", name="write_file"),
    tool_frag(0, args='{"comm'),
    tool_frag(1, args='{"path": "note.txt", "cont'),
    tool_frag(0, args='and": "echo hello-from-operator"}'),
    tool_frag(1, args='ent": "written by operator"}'),
])
ROUND2 = sse([delta(content="做完了。")])

_calls: list[dict] = []


def handler(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    _calls.append(payload)
    body = ROUND1 if len(_calls) == 1 else ROUND2
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


async def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="operator-verify-"))
    tools.WORKDIR = tmp
    agent.GATEWAY_BASE = "http://gateway.invalid/llm/v1"
    agent.GATEWAY_TOKEN = "tok"
    agent.DEFAULT_MODEL = "deepseek-v4-flash"

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def client(*a, **kw):
        kw["transport"] = transport
        return real(*a, **kw)

    agent.httpx.AsyncClient = client  # type: ignore[assignment]

    history: list[dict] = [{"role": "user", "content": "试一下"}]
    events = [e async for e in agent.run_turn(history)]
    agent.httpx.AsyncClient = real  # type: ignore[assignment]

    kinds = [e["type"] for e in events]
    fails: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            fails.append(msg)

    check(kinds.count("text") >= 1, "没有正文增量")
    check(kinds.count("tool") == 2, f"应当有 2 次工具调用, 实际 {kinds.count('tool')}")
    check(kinds.count("result") == 2, "工具结果数不对")
    check(kinds[-1] == "end", f"最后一个事件应当是 end, 实际 {kinds[-1]}")

    tool_events = [e for e in events if e["type"] == "tool"]
    by_name = {e["name"]: e["args"] for e in tool_events}
    # 归位正确的判据: 两组参数各自完整, 且**没有串到对方身上**
    check(
        by_name.get("shell", {}).get("command") == "echo hello-from-operator",
        f"shell 的参数拼错了: {by_name.get('shell')}",
    )
    check(
        by_name.get("write_file", {}).get("path") == "note.txt",
        f"write_file 的参数拼错了: {by_name.get('write_file')}",
    )

    # 工具是真执行的, 不是只发了事件
    check("hello-from-operator" in json.dumps(history, ensure_ascii=False), "shell 没真跑")
    check((tmp / "note.txt").exists(), "write_file 没真写")

    # 第二轮必须把 tool 结果带回去 —— 少了它模型看不到自己干了什么
    check(len(_calls) == 2, f"应当来回两次, 实际 {len(_calls)}")
    if len(_calls) == 2:
        roles = [m["role"] for m in _calls[1]["messages"]]
        check(roles.count("tool") == 2, f"第二轮没带上 tool 结果: {roles}")
        # OpenAI 格式要求 tool_calls 与 tool 消息严格配对, 不配对是每轮都 400
        ids = {
            tc["id"]
            for m in _calls[1]["messages"]
            if m["role"] == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]
        }
        tool_ids = {m["tool_call_id"] for m in _calls[1]["messages"] if m["role"] == "tool"}
        check(ids == tool_ids, f"tool_calls 与 tool 结果没配对: {ids} vs {tool_ids}")

    for f in fails:
        print("  ✗", f)
    if fails:
        print(f"\n自检失败 ({len(fails)} 项)")
        return 1
    print(f"事件序列: {' -> '.join(kinds)}")
    print("自检通过: 分片归位、工具真执行、结果回灌、消息配对 全部正确")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
