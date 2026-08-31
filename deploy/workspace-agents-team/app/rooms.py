"""房间与机器人: Operator 的多智能体模型。

产品形态是**群聊**, 不是"一个助手"。所以这里的第一性概念是房间, 不是对话:

    房间 = 一份共享的消息记录 + 一组成员机器人

一条用户消息进来, 房间里**该说话的机器人同时开跑** —— 各自独立一轮工具循环,
各自把话说进同一份记录。它们能看见彼此说了什么 (这是群聊的意义), 但看不见彼此
的工具过程 (那是各自的工作草稿, 摊开只会把记录冲垮)。

**记录是唯一事实来源**。每个机器人临要说话时, 才把记录渲染成它自己视角的
messages —— 而不是各存一份对话。各存一份的话, 群里加一个成员它就看不见之前发生
过什么, 而"新来的人读不到上文"在群聊里是致命的。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from . import tools

STATE_PATH = tools.WORKDIR / ".agents-team" / "rooms.json"


@dataclass
class Bot:
    id: str
    name: str
    emoji: str
    persona: str
    model: str = ""  # 空 = 用工作台的默认模型


@dataclass
class Message:
    id: str
    room: str
    #: 谁说的: "user" 或某个 bot id
    sender: str
    text: str
    ts: float
    #: 这条消息期间该机器人跑过的工具 (只留摘要, 给人看的时间线)
    tools: list[str] = field(default_factory=list)


@dataclass
class Room:
    id: str
    name: str
    members: list[str]
    created: float


#: 开箱自带的几个角色。**不是花活**: 群聊的价值要靠"成员各有所长"才立得住,
#: 开箱只有一个通用助手的话, 用户没有理由去拉第二个人进来, 群聊这个形态就死了。
#: 人格写得具体一点 —— "你是助手"这种会让几个成员说出一模一样的话, 群里就成了回音。
BUILTIN_BOTS: tuple[Bot, ...] = (
    Bot(
        "doer",
        "阿做",
        "🔧",
        "你负责动手。能跑命令解决的就别讨论, 直接做完再说结果。"
        "你说话短, 只报做了什么和看到什么。别人在分析时你可以先去把环境准备好。",
    ),
    Bot(
        "checker",
        "阿查",
        "🔍",
        "你负责查证和挑错。别人给的结论你要自己跑一遍验证, 不轻信。"
        "发现问题直接指出来, 说清楚哪一步不对、你是怎么验的。没问题就说没问题, 别客套。",
    ),
    Bot(
        "planner",
        "阿谋",
        "🗺️",
        "你负责拆解和排序。把用户要的东西拆成几步, 说清楚哪一步先做、为什么。"
        "你不动手, 也不要复述别人已经做完的事 —— 只在方向不清楚的时候说话。",
    ),
)


def _now() -> float:
    return time.time()


class Store:
    """房间、机器人、消息的全部状态。

    整份存一个 JSON。**不上数据库**: 一个工作台就一个人在用, 消息量是人手打字的
    量级, 而多一个数据库就多一个"起不来"的理由 —— 这个产品的价值在于打开就能用。
    """

    def __init__(self) -> None:
        self.bots: dict[str, Bot] = {b.id: b for b in BUILTIN_BOTS}
        self.rooms: dict[str, Room] = {}
        self.messages: list[Message] = []
        self._load()
        if not self.rooms:
            self._seed()

    # -- 持久化 -------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(STATE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return
        for b in raw.get("bots", []):
            self.bots[b["id"]] = Bot(**b)
        for r in raw.get("rooms", []):
            self.rooms[r["id"]] = Room(**r)
        self.messages = [Message(**m) for m in raw.get("messages", [])]

    def save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            # 内置角色不落盘 —— 落了之后改代码里的人格就再也生效不了,
            # 而用户完全看不出为什么"更新了却没变"。
            "bots": [asdict(b) for b in self.bots.values() if b.id not in _BUILTIN_IDS],
            "rooms": [asdict(r) for r in self.rooms.values()],
            "messages": [asdict(m) for m in self.messages[-500:]],
        }
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.replace(STATE_PATH)  # 原子替换: 写一半被杀不会留下半个 JSON

    def _seed(self) -> None:
        self.create_room("和 阿做 的对话", ["doer"])

    # -- 房间 ---------------------------------------------------------------
    def create_room(self, name: str, members: list[str]) -> Room:
        rid = uuid.uuid4().hex[:8]
        room = Room(rid, name, [m for m in members if m in self.bots], _now())
        self.rooms[rid] = room
        self.save()
        return room

    def transcript(self, room_id: str, limit: int = 200) -> list[Message]:
        return [m for m in self.messages if m.room == room_id][-limit:]

    def add(
        self,
        room_id: str,
        sender: str,
        text: str,
        tool_summaries: list[str] | None = None,
    ) -> Message:
        m = Message(
            uuid.uuid4().hex[:12], room_id, sender, text, _now(), tool_summaries or []
        )
        self.messages.append(m)
        return m

    # -- 渲染给某个机器人看 ---------------------------------------------------
    def render_for(self, bot: Bot, room_id: str) -> list[dict]:
        """把房间记录渲染成这个机器人视角的 messages。

        **别人说的话进 user 角色而不是 assistant**: 放 assistant 的话模型会把它当成
        自己说过的, 于是接着往下编, 表现是"它替别人把话说完了"。前面加上名字标签,
        它才知道这是谁在说。
        """
        room = self.rooms[room_id]
        others = [
            self.bots[m].name for m in room.members if m != bot.id and m in self.bots
        ]
        roster = (
            ("；同房间还有：" + "、".join(others)) if others else "；房间里只有你和用户"
        )
        head = (
            f"{bot.persona}\n\n"
            f"你在群聊「{room.name}」里, 名字是 {bot.name}{roster}。\n"
            "别人已经做过或说过的事不要重复做。轮到你没有可补充的, 就只回一句"
            "「没有要补充的」——群聊里刷存在感比不说话更糟。"
        )
        out: list[dict] = [{"role": "system", "content": head}]
        for m in self.transcript(room_id):
            if m.sender == "user":
                out.append({"role": "user", "content": m.text})
            elif m.sender == bot.id:
                out.append({"role": "assistant", "content": m.text})
            else:
                who = self.bots[m.sender].name if m.sender in self.bots else m.sender
                out.append({"role": "user", "content": f"[{who}] {m.text}"})
        return out


_BUILTIN_IDS = {b.id for b in BUILTIN_BOTS}
