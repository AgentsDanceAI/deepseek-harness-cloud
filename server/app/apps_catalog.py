"""云空间的产品目录 —— /apps 那张 4x4 卡片网格的数据源。

这份名单是**愿景**, products.py 的 registry 才是**事实**: 一个条目是否可点进
工作台, 由 products.enabled() 实时判定, 不在这里写死。于是接入一个新产品的
流程是: products.py 加 Product + 配好域名/镜像 -> 这页的卡自动从「即将上线」
翻成「进入工作台」, 本文件一个字都不用改 (id 对上即可)。

描述文案走 i18n (apps.d.<id>), 与站内其它文案同一套机制。图标是内联 SVG 而非
emoji —— 站内约定 (emoji 在没有彩色字体的平台上是豆腐块)。icon 字段是 SVG 的
**内部** 标记, 模板统一包上 <svg viewBox="0 0 24 24" ...>。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppEntry:
    id: str  # 与 products.py 的 Product.id 对齐; 对上了才算"已上线"
    name: str  # 产品名不翻译 —— 它们是专有名词
    # 卡片上那行小字的**文案键**, 文案在 i18n 的 apps.tag.<tag>。
    # 不是"类目": 页面是平铺的 4x4 网格, 没有分组也没有筛选, 这行字唯一的作用
    # 是让人一眼认出这张卡是什么。所以它按产品走 —— 几个产品确实同类时才共用一个键
    # (Codex 与 Claude Code、Open WebUI 与 Lobe Chat), 省得同一句话在 i18n 里写两遍。
    tag: str
    icon: str  # 24x24 viewBox 下的 SVG 内部标记 (stroke 图标)
    # 本站页面路径。非空 = 这个产品**不是云工作台**, 它就住在主站上, 卡片直接
    # 指过去 (数字人: 没有每用户容器可开, 通话页在 /avatar, 走 /api/avatar/*
    # 转发到我们自己的 GPU 节点)。空 = 老路, /work?product_id=<id>。
    href: str = ""


# 排布顺序即页面顺序: 已上线的两个放最前, 其余按"编码 -> 应用搭建 -> 媒体 ->
# 对话/知识 -> 工具"的叙事排。改顺序就是改这里。
# 下面这张表是**表**: 一行一个应用, 三列对齐着读。交给 formatter 会拆成一项一行,
# 16 个应用变成六十多行, 哪一列是什么就看不出来了。
# fmt: off
CATALOG: tuple[AppEntry, ...] = (
    AppEntry(
        "dsh", "DSH Agent", "deepseek",
        '<path d="M17.5 19a4.5 4.5 0 1 0-.9-8.9 6 6 0 1 0-11.1 3.4"/><path d="M6 19h11.5"/>',
    ),
    # Agents Team 顶掉了 JupyterLab (老板 2026-08-31 拍板): 这页是 4 列网格,
    # 16 个正好铺满四行, 加第 17 个会多出一行只有一张卡的空排 —— 所以是换不是加。
    # 换掉 Jupyter 而不是别的: 它是这 16 个里唯一**既没上线、也不是智能体**的坑位,
    # 而 Agents Team 是我们自己写的、已经上线的产品 (agentsteam.dshcloud.online)。
    AppEntry(
        "agents-team", "Agents Team", "team",
        '<circle cx="8" cy="9" r="2.5"/><circle cx="16" cy="9" r="2.5"/>'
        '<path d="M3.5 19a4.5 4.5 0 0 1 9 0M11.5 19a4.5 4.5 0 0 1 9 0"/>',
    ),
    AppEntry(
        "comfyui", "ComfyUI", "nodegraph",
        '<rect x="3" y="4" width="7" height="6" rx="1.5"/><rect x="14" y="14" width="7" height="6" rx="1.5"/>'
        '<path d="M10 7h4a3 3 0 0 1 3 3v4M7 10v4a3 3 0 0 0 3 3h4"/>',
    ),
    # Codex 顶掉了 OpenHands: 同为"自己读库改代码"的编码 agent, 而 Codex 与
    # Claude Code 共用一个镜像和一套接线, 多接一个几乎不要成本。
    AppEntry(
        "codex", "Codex", "coding",
        '<path d="m8 8-4 4 4 4M16 8l4 4-4 4"/><path d="m13.5 6-3 12"/>',
    ),
    # OpenClaw 顶掉了 Aider: 命令行编码 agent 这一格我们自己的 dsh 已经覆盖,
    # 而 OpenClaw 是"一个网关接几十个聊天渠道"的常驻个人助理, 是另一类东西。
    AppEntry(
        "openclaw", "OpenClaw 2.0", "lobster",
        '<path d="M7 10V6.5a2 2 0 1 1 4 0V10M11 10V5.5a2 2 0 1 1 4 0V10M15 10V7a2 2 0 1 1 4 0v7'
        'a7 7 0 0 1-7 7h-1a7 7 0 0 1-7-7v-3a2 2 0 1 1 4 0"/>',
    ),
    # Claude Code 顶掉了 code-server 这一格: 我们接进来的**就是** code-server
    # (见 deploy/workspace-codecli), 但它只是外壳 —— 值钱的是里面那个 agent。
    # 单卖一个空编辑器, 用户拿它干不了我们这儿该干的事。
    AppEntry(
        "claude-code", "Claude Code", "coding",
        '<path d="M12 3 4 7.5v9L12 21l8-4.5v-9L12 3z"/><path d="m8 10 4 2.2 4-2.2M12 12.2V17"/>',
    ),
    # Hermes 顶掉了 Bolt.diy: 搭应用这一格 Dify 和 Coze 已经占满, 而 Hermes 是
    # "会自己攒技能、带持久记忆"的常驻 agent, 与 OpenClaw 各占一端。
    AppEntry(
        "hermes", "Hermes Agent", "memory",
        '<path d="M12 3v6M9 6h6M6.5 9h11l-1.2 5.5a4.5 4.5 0 0 1-8.6 0L6.5 9z"/>'
        '<path d="M12 19v2M9 21h6M4 6.5 6.5 9M20 6.5 17.5 9"/>',
    ),
    AppEntry(
        "dify", "Dify", "orchestrate",
        '<rect x="3" y="3" width="8" height="8" rx="2"/><rect x="13" y="13" width="8" height="8" rx="2"/>'
        '<path d="M11 17H8a3 3 0 0 1-3-3v-3M13 7h3a3 3 0 0 1 3 3v3"/>',
    ),
    # Coze Studio 顶替了 Langflow: 同为 LLM 流程编排, Coze 的覆盖面 (Agent/知识库/
    # 工作流/发布渠道) 是它的超集, 老板 2026-08-28 点名要接。
    AppEntry(
        "coze", "Coze Studio", "studio",
        '<circle cx="12" cy="11" r="7.5"/><path d="M9 10h.01M15 10h.01M9.5 13.5a3.5 3.5 0 0 0 5 0"/>'
        '<path d="M12 18.5V21M8 20l1-1.8M16 20l-1-1.8"/>',
    ),
    # 数字人顶掉了 n8n: 后者的 Sustainable Use 许可证**明确禁止把它作为服务
    # 转售**, 而我们正是这个模式 —— 那一格本来就接不了, 一直空占着。
    # 数字人反过来是我们最独特的一块: 实时口型 + 用户自定义形象 + 定制音色,
    # 零件全在自己手上 (SoulX-FlashHead 跑在我们的 GPU 节点上)。
    AppEntry(
        "avatar", "数字人", "avatar",
        '<circle cx="12" cy="8" r="4"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>'
        '<path d="M19 5.5a5 5 0 0 1 0 5M21.5 3.5a8.5 8.5 0 0 1 0 9"/>',
        href="/avatar",
    ),
    # Open Design 顶替 SD WebUI: 生图已被 ComfyUI 覆盖, 而 open-design 是老板
    # 点名的 (nexu-io/open-design —— AI 设计智能体, 里面跑的就是我们的 dsh)。
    AppEntry(
        "open-design", "Open Design", "designer",
        '<path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/>'
        '<path d="M2 2l7.586 7.586"/><circle cx="11" cy="11" r="2"/>',
    ),
    # Penpot 曾在这个坑位 (还真上线过一天), 老板 2026-08-29 拍板换成 Excalidraw:
    # 设计坑位归 open-design, 这里补一个白板类, 凑齐 16 格。
    # 下面五格 2026-09-01 换成老板点名的五个智能体项目 (原先排的 excalidraw /
    # open-webui / lobe-chat / ragflow / whisper 都没建, 换掉不影响线上)。
    # 五个的许可证都查过, 全是 MIT —— 没有 n8n 那种"禁止作为服务转售"的条款。
    # pi 顶掉了 OpenHands (老板 2026-09-02): 盘点里它是最大的单镜像 (5.81GB)、
    # 第四个写代码的 agent、也是最难部署的一个。pi 的前端用社区的 pi-web-ui。
    AppEntry(
        "pi", "pi", "coder",
        '<path d="M4 6h16"/><path d="M8 6v12M16 6v12"/><path d="M5 18h6M13 18h6"/>',
    ),
    AppEntry(
        "autogen", "AutoGen Studio", "studio",
        '<circle cx="6.5" cy="7" r="2.5"/><circle cx="17.5" cy="7" r="2.5"/>'
        '<circle cx="12" cy="17.5" r="2.5"/>'
        '<path d="M8.6 8.6 10.6 15.4M15.4 8.6 13.4 15.4M9 7h6"/>',
    ),
    # 卡片叫 LangChain 而不是 LangGraph: LangChain 是伞, 跑的东西 (LangGraph 运行时
    # + 官方 agent-chat-ui + 我们写的智能体) 整套都在伞下 —— 名实相符, 而且以后
    # 换成他们别的运行时这个名字还成立。老板 2026-09-01 定的。
    AppEntry(
        "langchain", "LangChain", "agentkit",
        '<path d="M9.5 14.5 7 17a3.5 3.5 0 0 1-5-5l2.5-2.5"/>'
        '<path d="M14.5 9.5 17 7a3.5 3.5 0 0 1 5 5l-2.5 2.5"/><path d="M9 15l6-6"/>',
    ),
    AppEntry(
        "openmanus", "OpenManus", "agentcli",
        '<path d="M12 3v4M12 17v4M3 12h4M17 12h4"/><circle cx="12" cy="12" r="4.5"/>'
        '<path d="M6.3 6.3 8.8 8.8M15.2 15.2l2.5 2.5M17.7 6.3 15.2 8.8M8.8 15.2l-2.5 2.5"/>',
    ),
    AppEntry(
        "crewai", "CrewAI", "agentcli",
        '<circle cx="9" cy="8" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/>'
        '<circle cx="17" cy="9.5" r="2.2"/><path d="M14.8 19a4.4 4.4 0 0 1 5.7-4.2"/>',
    ),
)
# fmt: on


def site_apps() -> set[str]:
    """住在主站上的产品里, 本实例**真配好了**的那些 —— 与 products.enabled()
    对云工作台的作用相同, 只是判据不在 registry 里 (它们没有容器)。

    数字人要能签令牌才算上线: 没有 AVATAR_TOKEN_SECRET 就建立不了通话, 而失败
    发生在点了"开始通话"之后 —— 卡片却一路都是亮的。宁可不亮。
    """
    from . import config

    live = bool(config.AVATAR_GPU_URL and config.AVATAR_TOKEN_SECRET)
    return {"avatar"} if live else set()


def entries_with_status(enabled_ids: set[str]) -> list[dict]:
    """给模板用: 目录 + 实时上线状态。live 的判据只有一个 —— registry 里启用了."""
    return [
        {
            "id": a.id,
            "name": a.name,
            "tag": a.tag,
            "icon": a.icon,
            "href": a.href,
            "live": a.id in enabled_ids,
        }
        for a in CATALOG
    ]
