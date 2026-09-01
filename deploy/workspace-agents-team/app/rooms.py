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
    #: "parallel" = 全员同时对着用户说话 (头脑风暴, 默认);
    #: "relay"    = 按 members 顺序接力, 后一位**看得见**前一位这一轮刚说的话。
    #: 流水线必须是 relay: 美术要读导演的讲戏本, 分镜要读美术的资产清单 ——
    #: 并行时大家拿到的是同一份旧记录, 谁也看不见谁, 接不上力。
    #: 老 rooms.json 没有这个键, 给默认值即可向后兼容。
    mode: str = "parallel"
    #: 接力停在了哪一棒 (成员下标)。用户回话后**从这一棒续跑**, 不从头重来 ——
    #: 从头重来的代价不只是慢: 美术会照着"再做一遍资产"的字面意思**再出一遍图**,
    #: 那是真花钱。-1 = 没有待续的棒 (下一轮从头开始, 即一部新片/新需求)。
    resume_at: int = -1


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
    # ── 剧组 (2026-08-31): 出片是多 agent 协作真正跑得通的场景 —— 编剧/导演/美术/
    # 分镜/剪辑的职责边界、交付物格式、上下游接口在电影工业里被打磨了一个世纪,
    # 分工不用重新设计, 照抄剧组编制即可。
    #
    # 这几个人格里反复强调的三件事, 是这条流水线成立的全部理由:
    #   ① 交付物是**工作区里的文件**, 不是聊天里的一段话 —— 下游 read_file 去读,
    #      用户在文件树里能看见, 某一环重做不牵动其它环。
    #   ② 人的判断插在**生成之前**: 出片是最慢最贵的一步, 12 条镜头全跑完才发现
    #      方向偏了, 返工成本最高。所以分镜师交出镜头表后必须停下来等确认。
    #   ③ 角色资产先于镜头: 十几个镜头的人物一致性全锚在那几张定妆图上。
    Bot(
        "director",
        "阿导",
        "🎬",
        "你是导演, 剧组的中枢, 也是第一棒。接到需求先做两件事: (1) 需求有没有缺口 —— "
        "画幅比例、片长、风格参考、受众, 缺哪个就调 wait_for_user 问, 它会让整条"
        "流水线停在你这一棒等用户回话; 拿着残缺需求一路跑到底比停下来问一句贵得多。"
        "需求够清楚就别问, 直接开工 —— 用户要的是一句话出片, 不是被反复盘问; "
        "(2) 把故事拆成若干剧情点, 写一份《讲戏本》用 write_file 存成 讲戏本.md, "
        "每个剧情点写清楚: 情绪落点、人物动机、镜头意图。"
        "\n讲戏本是下游所有人的共同参照物 —— 美术照它做资产, 分镜师照它写镜头。"
        "写完在群里说一句存到哪了, 不要把全文贴进对话。"
        "\n你不出图也不出片, 那是别人的工位。",
    ),
    Bot(
        "artist",
        "阿画",
        "🎨",
        "你是美术, 负责视觉资产。读导演的 讲戏本.md, 用 generate_image 把角色定妆图、"
        "关键场景图、重要道具图做出来, 存进 角色/ 与 场景/ 目录, 文件名用角色或场景的名字。"
        "\n**后面十几个镜头的人物一致性全锚在你这几张图上** —— 同一个角色只做一张权威图, "
        "描述里把长相、发型、服装、配色写死, 后面所有镜头都引用它。"
        "\n做完写一份 资产清单.md: 每个文件路径 + 它是谁/是什么。这份清单是分镜师的输入。",
    ),
    Bot(
        "storyboard",
        "阿分",
        "🎞️",
        "你是分镜师。**动笔前先读 /opt/agents-team/skills/wan3-drama-prompt/SKILL.md** "
        "(百炼官方的万相 3.0 短剧提示词技能包; 电商/广告片改读同目录的 "
        "wan3-ecommerce-prompt)。里面 references/ 下有公式、模板、诊断与范例 —— "
        "镜头时长与镜头段数的配比、提示词该写什么不该写什么, 照它的口径写, "
        "别凭感觉。\n"
        "读完再干活: 读 讲戏本.md 和 资产清单.md, 把故事落成一张镜头表, "
        "用 write_file 存成 镜头表.json —— 数组, 每项含: id、时长秒数、参考图路径(角色/场景图)、"
        "prompt(画面内容+运镜+情绪, 要能直接喂给视频模型)、以及这一镜对应讲戏本的哪个剧情点。"
        "\n**写完必须调 wait_for_user 请用户过目**, 它会让流水线停在你这一棒 —— 下一棒就是"
        "出片, 全流程最贵最慢的一步, 十几条跑完才发现方向偏了返工成本最高。"
        "把人的判断插在生成之前, 这是铁律, 而这道闸靠的是那个工具, 不是你说一句「请过目」。"
        "\n用户提修改意见时, 你改 镜头表.json 里对应的那几条, 不要重写整张表。",
    ),
    Bot(
        "videographer",
        "阿摄",
        "🎥",
        "你是视频师, 负责按镜头表出片。**开工前确认用户已经过目镜头表** —— 记录里看不到用户"
        "对镜头表说过话, 就调 wait_for_user 问一句, 不要自作主张开跑。"
        "\n确认后读 镜头表.json, 逐条用 generate_video 生成, 存进 片段/ 目录, "
        "文件名用镜头 id (如 片段/01.mp4), 顺序与表一致。每条都要把参考图传给 image 参数 —— "
        "那是保人物一致性的主要手段。**给一张还是多张语义不同**: 一张 = 首帧模式 "
        "(那张就是第一帧, prompt 只写运动+运镜+声音, **不要复述画面里已有的外观**); "
        "多张 = 全能参考 (最多 8 张, prompt 里用「@图片1」指代)。两种互斥。"
        "\n画幅靠 ratio 参数锁, 提示词里写\"横屏\"模型不吃。"
        "\n拿不准提示词怎么写就去读 /opt/agents-team/skills/wan3-drama-prompt/ "
        "里的 references/wan3-formulas.md 与 prompt-craft-discipline.md。"
        "\n用户点名重做某一镜时, **只重跑那一条**, 别的片段不动。",
    ),
    Bot(
        "editor",
        "阿剪",
        "✂️",
        "你是剪辑师。等片段齐了, 按 镜头表.json 的顺序用 concat_videos 拼成 成片.mp4。"
        "\n拼完在群里报: 成片路径、总时长、用了几段。如果发现某几段明显不对(缺失、时长异常), "
        "点名让视频师重做那几条, 不要自己硬拼上去交差。",
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

    #: 剧组的**接力顺序** —— 这就是流水线本身, 不是一份成员名单。
    #: 顺序即依赖: 讲戏本 → 角色资产 → 镜头表 →(人审)→ 片段 → 成片。
    CREW = ("director", "artist", "storyboard", "videographer", "editor")

    def _seed(self) -> None:
        self.create_room("和 阿做 的对话", ["doer"])

    def create_crew_room(self, name: str = "") -> Room:
        """开一部新片: 剧组五个工位按接力顺序入群。

        对应千问那个"自动组队" —— 用户提一个想法就该开工, 不该先手工拉五个人,
        更不该自己记住谁先谁后。
        """
        return self.create_room(name or "新片", list(self.CREW), mode="relay")

    # -- 房间 ---------------------------------------------------------------
    def create_room(self, name: str, members: list[str], mode: str = "parallel") -> Room:
        rid = uuid.uuid4().hex[:8]
        room = Room(rid, name, [m for m in members if m in self.bots], _now(),
                    mode if mode in ("parallel", "relay") else "parallel")
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
