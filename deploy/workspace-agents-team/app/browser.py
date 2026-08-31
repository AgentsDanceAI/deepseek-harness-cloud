"""浏览器工具: 让机器人真的能打开网页、看见内容、点下去。

**按可见文字操作, 不用 CSS 选择器。** 模型写 `点击"登录"` 是可靠的, 写
`button.btn-primary:nth-child(3)` 基本靠猜 —— 而且页面一改版选择器就全废, 表现是
"昨天还好好的今天全点不中"。可见文字是用户和模型看到的同一个东西。

**每个动作都把动作之后的页面状态一起回给模型** (url/标题/正文摘要), 而不是让它
再调一次"读页面": 少一次来回就少一次它凭想象决定下一步的机会。

一个容器一个浏览器、一个页面。不做多标签页 —— 那要引入"当前是哪个标签"的状态,
而模型管不好这种隐式状态, 表现是它在 A 页面上执行了本该在 B 页面做的操作。
"""

from __future__ import annotations

import asyncio
import contextlib

#: 单页文本回给模型的上限。网页正文很容易几十万字符, 整段塞回去会把上下文烧光,
#: 而模型真正需要的只是"这页上有什么、能点什么"。
MAX_TEXT = 6_000

_lock = asyncio.Lock()
_pw = None
_browser = None
_page = None


class BrowserUnavailable(RuntimeError):
    pass


async def _ensure():
    """懒启动。没装 Playwright 时给一句**说明白的话**, 而不是抛 ImportError ——
    后者会被当成工具崩溃, 模型看不懂也就不会换个办法。"""
    global _pw, _browser, _page
    if _page is not None:
        return _page
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise BrowserUnavailable(
            "这个工作台没装浏览器组件, 网页类工具用不了; 需要联网取数据的话用 shell 里的 curl。"
        ) from e
    _pw = await async_playwright().start()
    # --no-sandbox: 容器里没有 user namespace, 不关沙箱 Chromium 起不来 (报错是
    # "Running as root without --no-sandbox is not supported", 出现在 stderr 里,
    # 而工具层只会显示成"浏览器启动失败")。隔离由容器本身提供。
    _browser = await _pw.chromium.launch(
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    _page = await _browser.new_page(viewport={"width": 1280, "height": 800})
    _page.set_default_timeout(15_000)
    return _page


async def _state(page, note: str = "") -> str:
    """动作之后的页面状态, 给模型看。"""
    try:
        title = await page.title()
        text = await page.inner_text("body")
    except Exception:  # noqa: BLE001 — 页面正在跳转时读不到是常事, 不该让整个工具失败
        title, text = "", ""
    text = " ".join(text.split())
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f" …[还有 {len(text) - MAX_TEXT} 字符未显示]"
    head = f"{note}\n" if note else ""
    return f"{head}当前页面: {title}\n{page.url}\n\n{text}"


async def _guarded(fn, summary: str) -> tuple[str, str]:
    """所有浏览器工具共用的外壳: 串行化 + 把异常变成给模型看的话。

    **串行化**是必须的: 几个机器人并行时会同时操作这一个页面, 不排队的话甲的点击
    落在乙刚跳走的页面上 —— 而两边的日志各自看都正常。
    """
    async with _lock:
        try:
            return await fn(), summary
        except BrowserUnavailable as e:
            return str(e), "浏览器不可用"
        except Exception as e:  # noqa: BLE001
            return f"浏览器操作失败: {type(e).__name__}: {e}", summary + " (失败)"


async def open_url(url: str) -> tuple[str, str]:
    async def go():
        page = await _ensure()
        if not url.startswith(("http://", "https://")):
            target = "https://" + url
        else:
            target = url
        await page.goto(target, wait_until="domcontentloaded")
        return await _state(page)

    return await _guarded(go, f"打开 {url[:50]}")


async def read_page() -> tuple[str, str]:
    async def go():
        page = await _ensure()
        return await _state(page)

    return await _guarded(go, "读当前页面")


async def click(text: str) -> tuple[str, str]:
    async def go():
        page = await _ensure()
        # get_by_text 命中多个时 Playwright 会报错而不是乱点 —— 这是好事,
        # 把"点哪个"的歧义暴露给模型, 它会换个更具体的文字。
        loc = page.get_by_text(text, exact=False).first
        await loc.click()
        await page.wait_for_load_state("domcontentloaded")
        return await _state(page, f"已点击「{text}」。")

    return await _guarded(go, f"点击「{text[:30]}」")


async def type_text(label: str, text: str, submit: bool = False) -> tuple[str, str]:
    async def go():
        page = await _ensure()
        # 先按无障碍标签找, 找不到再按占位符 —— 这两个是页面上**给人看**的提示,
        # 模型能从上一步的页面文本里读到它们。
        try:
            loc = page.get_by_label(label, exact=False).first
            await loc.fill(text)
        except Exception:  # noqa: BLE001
            loc = page.get_by_placeholder(label, exact=False).first
            await loc.fill(text)
        if submit:
            await loc.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
        return await _state(
            page, f"已在「{label}」里填入内容{'并提交' if submit else ''}。"
        )

    return await _guarded(go, f"填写「{label[:24]}」")


async def screenshot() -> tuple[str, str]:
    """整页截图, 回 data URI (前端直接显示给用户)。"""

    async def go():
        import base64

        page = await _ensure()
        png = await page.screenshot(full_page=False)
        return "data:image/png;base64," + base64.b64encode(png).decode()

    return await _guarded(go, "网页截图")


async def shutdown() -> None:
    """关掉浏览器。**关不掉就算了**: 这是收尾路径, 唯一的调用场景是进程要退出或
    自检跑完 —— 在那里为一个关不掉的子进程抛异常, 只会把真正的退出原因盖掉。"""
    global _pw, _browser, _page
    for obj, closer in ((_browser, "close"), (_pw, "stop")):
        if obj is not None:
            with contextlib.suppress(Exception):
                await getattr(obj, closer)()
    _pw = _browser = _page = None


#: 工具定义。描述写**什么时候用**, 不写"这是什么" —— 模型选错工具几乎都是因为
#: 描述只说了功能没说场景。
SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": (
                "用浏览器打开一个网址, 返回页面标题和正文。需要**看网页内容**、"
                "或者要在网站上操作时用它; 只是下载文件或调接口用 shell 里的 curl 更快。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_read",
            "description": "重新读一遍当前页面的内容。页面自己变了(比如加载完成)之后用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": (
                "点击页面上带指定文字的元素。**用你在页面正文里看到的文字**, "
                "不要用 CSS 选择器。命中多个会报错, 那时换一段更独特的文字。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "元素上的可见文字"}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "在输入框里填内容。label 用输入框的标签或占位提示文字。submit=true 会顺带回车提交。",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "输入框的标签或占位文字",
                    },
                    "text": {"type": "string"},
                    "submit": {"type": "boolean"},
                },
                "required": ["label", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "把当前网页截图给用户看。**排版、样式、图片**这类文字描述不清的东西才用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

HANDLERS = {
    "browser_open": lambda a: open_url(a["url"]),
    "browser_read": lambda a: read_page(),
    "browser_click": lambda a: click(a["text"]),
    "browser_type": lambda a: type_text(
        a["label"], a.get("text", ""), bool(a.get("submit"))
    ),
    "browser_screenshot": lambda a: screenshot(),
}
