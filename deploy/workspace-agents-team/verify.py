"""Operator 自检: 假造网关, 把单个机器人的一轮和一个群的并行一轮都真跑一遍。

跑法: python deploy/workspace-agents-team/verify.py

钉两件最容易悄悄坏掉的事:
1. **流式工具调用按 index 归位**。按到达顺序拼的实现会把 A 的参数接到 B 上, 而拼出来
   的 JSON 照样解析成功 —— "参数对不上却完全不报错", 表现是它读了个没人要它读的文件。
2. **并行时事件不串台**。几个成员同时跑, 事件交错到达; 归位错了就是甲的话进了乙的
   气泡, 而两边都是通顺的中文, 光看界面看不出来。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import httpx
from app import agent, filmdir

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="agents-team-verify-"))
filmdir.ROOT = _tmp   # 唯一的根 (tools 不再自留一份)

from app import rooms

rooms.STATE_PATH = _tmp / ".agents-team" / "rooms.json"

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILS.append(msg)


def sse(chunks: list[dict]) -> bytes:
    return (
        "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    ).encode()


def frag(i: int, *, cid: str = "", name: str = "", args: str = "") -> dict:
    fn = {}
    if name:
        fn["name"] = name
    if args:
        fn["arguments"] = args
    part = {"index": i, "function": fn}
    if cid:
        part["id"] = cid
    return {"choices": [{"delta": {"tool_calls": [part]}}]}


def text(s: str) -> dict:
    return {"choices": [{"delta": {"content": s}}]}


# ---- 1. 单机器人: 分片交错归位 ---------------------------------------------
ROUND1 = sse(
    [
        text("我先看一下。"),
        frag(0, cid="a", name="shell"),
        frag(1, cid="b", name="write_file"),
        frag(0, args='{"comm'),
        frag(1, args='{"path": "note.txt", "cont'),
        frag(0, args='and": "echo hello-from-team"}'),
        frag(1, args='ent": "written"}'),
    ]
)
ROUND2 = sse([text("做完了。")])


async def check_single() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = ROUND1 if len(seen) == 1 else ROUND2
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    with _mock(handler):
        msgs = [
            {"role": "system", "content": "你是测试"},
            {"role": "user", "content": "试一下"},
        ]
        events = [e async for e in agent.run_turn("doer", msgs)]

    kinds = [e["type"] for e in events]
    check(kinds.count("tool") == 2, f"应当 2 次工具调用, 实际 {kinds.count('tool')}")
    check(kinds[-1] == "end", f"最后应当是 end, 实际 {kinds[-1]}")
    check(all(e.get("bot") == "doer" for e in events), "事件没有正确标上 bot")

    by = {e["name"]: e["args"] for e in events if e["type"] == "tool"}
    check(
        by.get("shell", {}).get("command") == "echo hello-from-team",
        f"shell 参数拼错: {by.get('shell')}",
    )
    check(
        by.get("write_file", {}).get("path") == "note.txt",
        f"write_file 参数拼错: {by.get('write_file')}",
    )
    check((_tmp / "note.txt").exists(), "write_file 没真写")

    end = events[-1]
    check("做完了。" in end.get("text", ""), "end 没带上最终正文")
    check(len(end.get("tools") or []) == 2, "end 没带上工具摘要")
    if len(seen) == 2:
        roles = [m["role"] for m in seen[1]["messages"]]
        check(roles.count("tool") == 2, f"第二轮没带上 tool 结果: {roles}")


# ---- 2. 群聊: 两个成员并行, 事件不串台 --------------------------------------
async def check_room() -> None:
    from app import main as srv

    store = rooms.Store()
    srv.store = store
    room = store.create_room("测试群", ["doer", "checker"])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # 认人只能看**身份标记**, 不能看人格里出没出现某个名字 —— 成员名单里
        # 两个人都会看到对方的名字, 拿名字当判据两边会认成同一个人。
        who = "doer" if "名字是 阿做" in body["messages"][0]["content"] else "checker"
        return httpx.Response(
            200,
            content=sse([text(f"我是{who}"), text("-已完成")]),
            headers={"content-type": "text/event-stream"},
        )

    with _mock(handler):
        events = [e async for e in srv._run_room(room, None)]

    ends = [e for e in events if e["type"] == "end"]
    check(len(ends) == 2, f"两个成员应当各收尾一次, 实际 {len(ends)}")
    said = {e["bot"]: e["text"] for e in ends}
    # 串台的判据: 谁的正文里出现了别人的名字
    check(
        said.get("doer") == "我是doer-已完成",
        f"doer 的正文串台了: {said.get('doer')!r}",
    )
    check(
        said.get("checker") == "我是checker-已完成",
        f"checker 的正文串台了: {said.get('checker')!r}",
    )

    # 两条都要写进房间记录, 且发送人正确
    tr = store.transcript(room.id)
    senders = [m.sender for m in tr]
    check(
        senders.count("doer") == 1 and senders.count("checker") == 1,
        f"房间记录里成员发言数不对: {senders}",
    )

    # 新成员看得见入群前的记录 (render_for 每轮现渲染整份记录)
    view = store.render_for(store.bots["planner"], room.id)
    joined = json.dumps(view, ensure_ascii=False)
    check("我是doer" in joined and "我是checker" in joined, "新成员读不到入群前的对话")
    check(
        all(m["role"] != "assistant" for m in view),
        "别人的话被放进了 assistant —— 模型会当成自己说的然后接着编",
    )


class _mock:
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        agent.GATEWAY_BASE = "http://gw.invalid/llm/v1"
        agent.GATEWAY_TOKEN = "tok"
        agent.DEFAULT_MODEL = "deepseek-v4-flash"
        self.real = httpx.AsyncClient
        transport = httpx.MockTransport(self.handler)

        def client(*a, **kw):
            kw["transport"] = transport
            return self.real(*a, **kw)

        agent.httpx.AsyncClient = client
        return self

    def __exit__(self, *a):
        agent.httpx.AsyncClient = self.real
        return False


async def main() -> int:
    await check_single()
    await check_room()
    for f in FAILS:
        print("  ✗", f)
    if FAILS:
        print(f"\n自检失败 ({len(FAILS)} 项)")
        return 1
    print("自检通过: 分片归位、工具真执行、结果回灌、并行不串台、新成员读得到上文")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
