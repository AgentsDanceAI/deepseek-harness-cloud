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
import os
import pathlib
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field

from . import filmdir, teams

STATE_PATH = filmdir.ROOT / ".agents-team" / "rooms.json"
LOAD_RETRIES = int(os.environ.get("AGENTS_TEAM_LOAD_RETRIES", "5"))
LOAD_RETRY_S = float(os.environ.get("AGENTS_TEAM_LOAD_RETRY_S", "1.0"))


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
    #: 这部片自己的目录 (相对工作区根)。空 = 用扁平的根目录 —— 老 rooms.json 里
    #: 没有这个键, 读出来就是空, 于是老房间原地不动, 不用迁移。新片各自一个目录:
    #: 共用一个目录时, 第二部片会继承第一部的角色图和成片, 美术拒绝重做、出片
    #: 去重闸整片跳过, 而全程不报错 (见 filmdir.py)。
    dir: str = ""
    #: 接力停在了哪一棒 (成员下标)。用户回话后**从这一棒续跑**, 不从头重来 ——
    #: 从头重来的代价不只是慢: 美术会照着"再做一遍资产"的字面意思**再出一遍图**,
    #: 那是真花钱。-1 = 没有待续的棒 (下一轮从头开始, 即一部新片/新需求)。
    resume_at: int = -1
    #: 出自哪个团队模板 (teams.TEAMS 的 id)。空 = 手工拉的群。模板不进 rooms.json,
    #: 房间只记 id —— 模板里的顺序/提示改了, 老房间也跟着变 (与内置人格同理)。
    team: str = ""


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
        '\n画幅靠 ratio 参数锁, 提示词里写"横屏"模型不吃。'
        "\n拿不准提示词怎么写就去读 /opt/agents-team/skills/wan3-drama-prompt/ "
        "里的 references/wan3-formulas.md 与 prompt-craft-discipline.md。"
        "\n**duration 与 resolution 每次都要照镜头表写全** —— 漏掉 duration 会出成 5 秒, "
        "镜头表写 10 秒的镜头出成 5 秒是废片, 而且照样扣钱。"
        "\n**出片是花钱的, 同一个镜头不要反复跑。** 工具会挡住已有成片的路径并告诉你 "
        "「已经有成片了」—— 那不是错误, 是省钱。真要重做就先 rm 掉那个文件再调, "
        "那是一个需要你明确决定的动作。"
        "\n参数不对就先想清楚再发一次, 不要靠反复重跑试参数 —— 每试一次都是真金白银。"
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


def _read_with_retry(path: pathlib.Path, tries: int = LOAD_RETRIES) -> str:
    """NFS 刚挂上那几秒偶有 EIO/ESTALE; 重试几次比把它当成"没有存档"便宜得多。
    FileNotFoundError 不重试 —— 那是另一条路 (首次启动判定) 的事。"""
    last: OSError | None = None
    for i in range(max(1, tries)):
        try:
            return path.read_text()
        except FileNotFoundError:
            raise
        except OSError as e:
            last = e
            if i + 1 < tries:
                time.sleep(LOAD_RETRY_S)
    assert last is not None
    raise last


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
        #: 存档读失败的原因。非空时**拒绝保存** —— 否则内存里这份空表会把磁盘上真正的
        #: 存档盖掉。2026-09-02 事故: 工作台重建时读不到 rooms.json (多半是 NAS 还没挂好),
        #: 代码当成首次启动播了个默认房, 然后把只有一个房的存档写回 NAS, 老板所有房间
        #: 和聊天记录当场没了 —— 全程没有一行报错。
        self.load_failed: str = ""
        self._load()
        if (
            not self.rooms
            and not self.load_failed
            and not self._looks_like_first_boot()
        ):
            # 存档不在, 工作区却有别的东西 (片/、角色/…): 真正的首次启动 /workspace 是空的。
            # 这更像"存档暂时读不到" (NFS 刚挂上、目录列表还没同步), 不许当首次启动播种。
            self.load_failed = "存档不在, 但工作区非空 —— 不当首次启动处理"
        if not self.rooms and not self.load_failed:
            self._seed()

    # -- 持久化 -------------------------------------------------------------
    @staticmethod
    def _looks_like_first_boot() -> bool:
        root = STATE_PATH.parent.parent
        try:
            return not any(root.iterdir())
        except FileNotFoundError:
            return True
        except OSError:
            return False  # 连目录都读不了 —— 更不该播种

    def _load(self) -> None:
        """读存档。**只有"文件确实不存在"才算首次启动**; 其它任何失败都不许播种。

        读不到和不存在是两回事: 权限、挂载没就绪、半个 JSON、字段对不上 —— 这些情况下
        磁盘上很可能有一份好的存档, 播种再保存等于把它盖掉。主文件坏了先试 .bak。
        """
        for path in (STATE_PATH, STATE_PATH.with_suffix(".json.bak")):
            try:
                raw = json.loads(_read_with_retry(path))
            except FileNotFoundError:
                if path == STATE_PATH and not STATE_PATH.parent.exists():
                    # 连目录都没有 = 真正的首次启动 (或挂载没就绪 —— 见下面 save 的兜底)
                    continue
                continue
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
                self.load_failed = f"{path.name}: {type(e).__name__}: {e}"
                continue
            try:
                for b in raw.get("bots", []):
                    self.bots[b["id"]] = Bot(**b)
                for r in raw.get("rooms", []):
                    self.rooms[r["id"]] = Room(**r)
                self.messages = [Message(**m) for m in raw.get("messages", [])]
            except (TypeError, KeyError, ValueError) as e:
                self.rooms.clear()
                self.messages.clear()
                self.load_failed = f"{path.name}: 字段对不上: {type(e).__name__}: {e}"
                continue
            self.load_failed = ""
            if path != STATE_PATH:
                print(f"!! rooms.json 坏了, 已从 {path.name} 恢复", file=sys.stderr)
            return
        # 走到这里: 主文件与 .bak 都没读成。若主文件存在却读不了, load_failed 已置;
        # 若两个都不存在, 才是干净的首次启动 (load_failed 为空)。

    def save(self) -> None:
        if self.load_failed:
            # 内存里这份不可信 (读失败时是空的), 写回去就是把真存档盖掉。宁可这一轮不落盘。
            print(
                f"!! 存档读失败 ({self.load_failed}), 拒绝保存以免覆盖", file=sys.stderr
            )
            return
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 先留一份 .bak: 万一这次写出的东西有问题 (或者是错误地播种), 上一版还在。
        if STATE_PATH.exists():
            try:
                shutil.copyfile(STATE_PATH, STATE_PATH.with_suffix(".json.bak"))
            except OSError as e:
                print(f"!! rooms.json.bak 没写成: {e}", file=sys.stderr)
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
        """开一部新片 —— 就是 create_team_room("film")。保留这个名字给老调用方。"""
        return self.create_team_room("film", name)

    def create_team_room(self, team_id: str, name: str = "") -> Room:
        """按模板组一个团队: 成员、顺序、模式、产物目录都从模板来。

        对应千问那个"自动组队" —— 用户提一个想法就该开工, 不该先手工拉五个人,
        更不该自己记住谁先谁后。
        """
        team = teams.BY_ID[team_id]
        return self.create_room(
            name or f"新{team.name}",
            list(team.members),
            mode=team.mode,
            team=team.id,
            dir_prefix=team.dir,
        )

    # -- 房间 ---------------------------------------------------------------
    def create_room(
        self,
        name: str,
        members: list[str],
        mode: str = "parallel",
        team: str = "",
        dir_prefix: str | None = None,
    ) -> Room:
        rid = uuid.uuid4().hex[:8]
        room = Room(
            rid,
            name,
            [m for m in members if m in self.bots],
            _now(),
            mode if mode in ("parallel", "relay") else "parallel",
            filmdir.slug_for(rid, name, dir_prefix or filmdir.FILMS),
            team=team,
        )
        self.rooms[rid] = room
        self.save()
        return room

    def delete_room(self, room_id: str) -> bool:
        """删群: 房间和它的聊天记录一起删; **产物目录里的文件留在磁盘上**。

        照口袋专家"解散群, 专家本身还在"的口径: 成员是花名册里的人, 不属于某个群。
        文件不删是因为那是用户花钱出的东西 (成片、图), 删群不该顺手把它们抹掉 ——
        要清理让用户在「文件」面板里自己看着删。
        """
        if room_id not in self.rooms:
            return False
        del self.rooms[room_id]
        self.messages = [m for m in self.messages if m.room != room_id]
        self.save()
        return True

    def remove_member(self, room_id: str, bot_id: str) -> Room | None:
        """请一位出群。群里至少留一个人 —— 空群没有任何用, 而且 relay 的下标会越界。"""
        room = self.rooms.get(room_id)
        if room is None or bot_id not in room.members:
            return room
        if len(room.members) <= 1:
            raise ValueError("群里至少要留一个成员")
        idx = room.members.index(bot_id)
        room.members.remove(bot_id)
        # 接力停在被移出的人之后的话, 下标要跟着前移, 否则续跑会跳过一位
        if room.resume_at > idx:
            room.resume_at -= 1
        elif room.resume_at == idx and room.resume_at >= len(room.members):
            room.resume_at = -1
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


#: 其余九个团队的成员定义在 teams.py (人格是产品配置, 集中一处好改)。这里合并进
#: 花名册, 「拉个群」的选人列表和 /api/bots 都从这一份来。
BUILTIN_BOTS = BUILTIN_BOTS + tuple(Bot(*spec) for spec in teams.BOT_SPECS)
_BUILTIN_IDS = {b.id for b in BUILTIN_BOTS}
