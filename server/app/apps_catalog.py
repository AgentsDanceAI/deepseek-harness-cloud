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
    AppEntry(
        "n8n", "n8n", "automation",
        '<circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="19" cy="18" r="2.5"/>'
        '<path d="M7.5 12h4M16.6 6.8 11 10.7M16.6 17.2 11 13.3"/>',
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
    AppEntry(
        "excalidraw", "Excalidraw", "sketch",
        '<path d="M4 20l3.5-1 11-11a2.1 2.1 0 0 0-3-3l-11 11L4 20z"/><path d="M13 6.5 17.5 11"/>',
    ),
    AppEntry(
        "open-webui", "Open WebUI", "chatui",
        '<path d="M21 12a8 8 0 0 1-8 8H4l2-3a8 8 0 1 1 15-5z"/><path d="M9 11h.01M13 11h.01M17 11h.01"/>',
    ),
    AppEntry(
        "lobe-chat", "Lobe Chat", "chatui",
        '<rect x="3" y="6" width="18" height="12" rx="4"/><path d="M8 11v2M16 11v2M12 2v4"/>',
    ),
    AppEntry(
        "ragflow", "RAGFlow", "docqa",
        '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v18H6.5A2.5 2.5 0 0 1 4 18.5v-13z"/>'
        '<path d="M4 18.5A2.5 2.5 0 0 1 6.5 16H20M9 8h7"/>',
    ),
    AppEntry(
        "whisper", "Whisper WebUI", "stt",
        '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8"/>',
    ),
)
# fmt: on


def entries_with_status(enabled_ids: set[str]) -> list[dict]:
    """给模板用: 目录 + 实时上线状态。live 的判据只有一个 —— registry 里启用了."""
    return [
        {"id": a.id, "name": a.name, "tag": a.tag, "icon": a.icon, "live": a.id in enabled_ids}
        for a in CATALOG
    ]
