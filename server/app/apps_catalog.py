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
    id: str        # 与 products.py 的 Product.id 对齐; 对上了才算"已上线"
    name: str      # 产品名不翻译 —— 它们是专有名词
    cat: str       # 类目键, 文案在 i18n 的 apps.cat.<cat>
    icon: str      # 24x24 viewBox 下的 SVG 内部标记 (stroke 图标)


# 排布顺序即页面顺序: 已上线的两个放最前, 其余按"编码 -> 应用搭建 -> 媒体 ->
# 对话/知识 -> 工具"的叙事排。改顺序就是改这里。
CATALOG: tuple[AppEntry, ...] = (
    AppEntry(
        "dsh", "DSH Agent", "agent",
        '<path d="M17.5 19a4.5 4.5 0 1 0-.9-8.9 6 6 0 1 0-11.1 3.4"/><path d="M6 19h11.5"/>',
    ),
    AppEntry(
        "comfyui", "ComfyUI", "media",
        '<rect x="3" y="4" width="7" height="6" rx="1.5"/><rect x="14" y="14" width="7" height="6" rx="1.5"/>'
        '<path d="M10 7h4a3 3 0 0 1 3 3v4M7 10v4a3 3 0 0 0 3 3h4"/>',
    ),
    AppEntry(
        "openhands", "OpenHands", "agent",
        '<path d="m8 8-4 4 4 4M16 8l4 4-4 4"/><path d="M12 3v3M12 18v3"/>',
    ),
    AppEntry(
        "aider", "Aider", "agent",
        '<path d="M4 5h16v11H4z"/><path d="m7 8 2.5 2.5L7 13M12 13h4M8 20h8"/>',
    ),
    AppEntry(
        "code-server", "code-server", "code",
        '<path d="m14 4-4 16M17 7l4 5-4 5M7 7l-4 5 4 5"/>',
    ),
    AppEntry(
        "jupyter", "JupyterLab", "data",
        '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    ),
    AppEntry(
        "bolt-diy", "Bolt.diy", "builder",
        '<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/>',
    ),
    AppEntry(
        "dify", "Dify", "builder",
        '<rect x="3" y="3" width="8" height="8" rx="2"/><rect x="13" y="13" width="8" height="8" rx="2"/>'
        '<path d="M11 17H8a3 3 0 0 1-3-3v-3M13 7h3a3 3 0 0 1 3 3v3"/>',
    ),
    # Coze Studio 顶替了 Langflow: 同为 LLM 流程编排, Coze 的覆盖面 (Agent/知识库/
    # 工作流/发布渠道) 是它的超集, 老板 2026-08-28 点名要接。
    AppEntry(
        "coze", "Coze Studio", "builder",
        '<circle cx="12" cy="11" r="7.5"/><path d="M9 10h.01M15 10h.01M9.5 13.5a3.5 3.5 0 0 0 5 0"/>'
        '<path d="M12 18.5V21M8 20l1-1.8M16 20l-1-1.8"/>',
    ),
    AppEntry(
        "n8n", "n8n", "flow",
        '<circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="19" cy="18" r="2.5"/>'
        '<path d="M7.5 12h4M16.6 6.8 11 10.7M16.6 17.2 11 13.3"/>',
    ),
    # Open Design 顶替 SD WebUI: 生图已被 ComfyUI 覆盖, 而 open-design 是老板
    # 点名的 (nexu-io/open-design —— AI 设计智能体, 里面跑的就是我们的 dsh)。
    AppEntry(
        "open-design", "Open Design", "design",
        '<path d="M12 19l7-7 3 3-7 7-3-3z"/><path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z"/>'
        '<path d="M2 2l7.586 7.586"/><circle cx="11" cy="11" r="2"/>',
    ),
    # Penpot 曾在这个坑位 (还真上线过一天), 老板 2026-08-29 拍板换成 Excalidraw:
    # 设计坑位归 open-design, 这里补一个白板类, 凑齐 16 格。
    AppEntry(
        "excalidraw", "Excalidraw", "whiteboard",
        '<path d="M4 20l3.5-1 11-11a2.1 2.1 0 0 0-3-3l-11 11L4 20z"/><path d="M13 6.5 17.5 11"/>',
    ),
    AppEntry(
        "open-webui", "Open WebUI", "chat",
        '<path d="M21 12a8 8 0 0 1-8 8H4l2-3a8 8 0 1 1 15-5z"/><path d="M9 11h.01M13 11h.01M17 11h.01"/>',
    ),
    AppEntry(
        "lobe-chat", "Lobe Chat", "chat",
        '<rect x="3" y="6" width="18" height="12" rx="4"/><path d="M8 11v2M16 11v2M12 2v4"/>',
    ),
    AppEntry(
        "ragflow", "RAGFlow", "knowledge",
        '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v18H6.5A2.5 2.5 0 0 1 4 18.5v-13z"/>'
        '<path d="M4 18.5A2.5 2.5 0 0 1 6.5 16H20M9 8h7"/>',
    ),
    AppEntry(
        "whisper", "Whisper WebUI", "audio",
        '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8"/>',
    ),
)


def entries_with_status(enabled_ids: set[str]) -> list[dict]:
    """给模板用: 目录 + 实时上线状态。live 的判据只有一个 —— registry 里启用了."""
    return [
        {"id": a.id, "name": a.name, "cat": a.cat, "icon": a.icon, "live": a.id in enabled_ids}
        for a in CATALOG
    ]
