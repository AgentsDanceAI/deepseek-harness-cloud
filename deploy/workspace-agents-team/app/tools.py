"""Operator 的工具层。

每个工具三件事: 给模型看的 schema、真正的执行、以及**给人看的一行摘要**
(前端时间线上显示的那行)。摘要不是装饰 —— 用户看不到 shell 输出全文, 只能靠
它判断智能体在干什么; 没有摘要的工具调用在界面上就是一个不透明的方块。

工具一律在**用户自己的容器里**执行, 工作目录是 NAS 挂进来的 /workspace。
容器就是隔离边界: 每人一个容器组, 组内回环, 出站受安全组约束。所以这里不再
自造一层路径白名单 —— 那种白名单挡不住真正的越权 (进程能开子进程), 只会让
正常用法频繁踩空, 而且**会给人一种有防护的错觉**。
"""

from __future__ import annotations

import asyncio
import base64
import os
import pathlib

#: 工作目录。与 products.py 里的 mounts 一致 —— 改一处必须改另一处,
#: 不然用户的文件写进容器本地, 实例一回收就没了 (而且不报错)。
WORKDIR = pathlib.Path(os.environ.get("AGENTS_TEAM_WORKDIR", "/workspace"))

#: 单次 shell 的墙钟上限。超时不是失败, 是**把已有输出还给模型**让它自己决定 ——
#: 直接报错会让"跑一个长任务"这种正常用法变成死路。
SHELL_TIMEOUT_S = float(os.environ.get("AGENTS_TEAM_SHELL_TIMEOUT", "120"))

#: 回给模型的输出上限。超了从**中间**截断而不是砍尾巴: 命令的结论通常在最后几行
#: (报错、退出码、汇总), 砍尾巴等于把最有用的部分丢掉。
MAX_OUTPUT_CHARS = 20_000


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    head = MAX_OUTPUT_CHARS // 2
    tail = MAX_OUTPUT_CHARS - head
    dropped = len(text) - MAX_OUTPUT_CHARS
    return f"{text[:head]}\n\n... [中间省略 {dropped} 字符] ...\n\n{text[-tail:]}"


def _resolve(path: str) -> pathlib.Path:
    """相对路径一律相对 WORKDIR 解析; 绝对路径原样放行。

    放行绝对路径是故意的: 智能体要能读 /etc/os-release、跑 /usr/bin 里的东西。
    隔离靠容器, 不靠这一行 (见模块开头)。
    """
    p = pathlib.Path(path)
    return p if p.is_absolute() else (WORKDIR / p)


async def run_shell(command: str, timeout: float | None = None) -> tuple[str, str]:
    """跑一条 shell 命令, 返回 (给模型的文本, 给人看的摘要)。

    **合并 stderr 到 stdout**: 分开回传时模型经常只读 stdout, 于是把一条写在
    stderr 上的报错当成"没有输出"继续往下走。
    """
    limit = SHELL_TIMEOUT_S if timeout is None else timeout
    WORKDIR.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(WORKDIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=limit)
        code = proc.returncode
        note = ""
    except asyncio.TimeoutError:
        proc.kill()
        out, _ = await proc.communicate()
        code = None
        note = f"\n\n[超过 {limit:.0f} 秒被终止; 以上是终止前的输出]"

    text = _truncate((out or b"").decode("utf-8", "replace")) + note
    body = f"$ {command}\n{text}" if text.strip() else f"$ {command}\n(无输出)"
    if code is not None:
        body += f"\n[退出码 {code}]"
    head = command.strip().splitlines()[0]
    summary = head if len(head) <= 60 else head[:57] + "..."
    return body, f"运行 {summary}"


async def read_file(path: str, max_bytes: int = 200_000) -> tuple[str, str]:
    p = _resolve(path)
    if not p.exists():
        return f"没有这个文件: {p}", f"读 {path} (不存在)"
    if p.is_dir():
        names = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
        return f"{p} 是目录, 内容:\n" + "\n".join(names[:500]), f"列出 {path}"
    data = p.read_bytes()[:max_bytes]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # 二进制文件别硬塞给模型 —— 乱码会把上下文烧光, 而且它什么也读不出来。
        return (
            f"{p} 是二进制文件 ({p.stat().st_size} 字节), 没有按文本读取。",
            f"读 {path} (二进制)",
        )
    return _truncate(text), f"读 {path}"


async def write_file(path: str, content: str) -> tuple[str, str]:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {p} ({len(content)} 字符)", f"写 {path}"


async def screenshot(display: str | None = None) -> tuple[str, str]:
    """抓一张桌面截图, 回 data URI。

    没有桌面时**明确说没有**, 而不是回一张黑图 —— 黑图会让模型以为页面是空的,
    然后开始编造它"看到"的内容。
    """
    env_display = display or os.environ.get("DISPLAY", "")
    if not env_display:
        return "这个工作台没有图形桌面, 截图不可用。", "截图 (无桌面)"
    out = WORKDIR / ".agents-team-shot.png"
    proc = await asyncio.create_subprocess_exec(
        "scrot",
        "-o",
        str(out),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0 or not out.exists():
        return f"截图失败: {(err or b'').decode('utf-8', 'replace')[:200]}", "截图失败"
    b64 = base64.b64encode(out.read_bytes()).decode()
    return f"data:image/png;base64,{b64}", "截了一张屏"


#: 给模型的工具定义 (OpenAI tools 格式)。描述里写**什么时候用**而不是"这是什么",
#: 模型选错工具几乎都是因为描述只说了功能没说场景。
SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "在工作台容器里执行一条 shell 命令并拿到输出。安装软件、跑脚本、"
                "查看进程、用 curl 访问网络都走它。工作目录是 /workspace(挂在持久存储上)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的完整命令"},
                    "timeout": {"type": "number", "description": "秒; 省略用默认 120"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读一个文件的内容; 传目录则列出目录。相对路径相对 /workspace。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容整份写进文件(覆盖)。父目录会自动建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "截取当前图形桌面。只在需要**看到**界面时用; 纯文本任务不要用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_HANDLERS = {
    "shell": lambda a: run_shell(a["command"], a.get("timeout")),
    "read_file": lambda a: read_file(a["path"]),
    "write_file": lambda a: write_file(a["path"], a.get("content", "")),
    "screenshot": lambda a: screenshot(),
}


async def dispatch(name: str, args: dict) -> tuple[str, str]:
    """执行一个工具调用。

    **未知工具名不抛异常**, 回一句给模型看的话: 模型偶尔会幻觉出工具名, 抛异常
    等于整轮对话炸掉, 而告诉它"没有这个工具"它自己会换一个。
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return (
            f"没有名为 {name} 的工具。可用的是: {', '.join(_HANDLERS)}",
            f"未知工具 {name}",
        )
    try:
        return await handler(args)
    except KeyError as e:
        return f"工具 {name} 缺少必填参数 {e}", f"{name} 参数不全"
    except Exception as e:  # noqa: BLE001 — 工具出错要还给模型, 不是终止整轮
        return f"工具 {name} 执行出错: {type(e).__name__}: {e}", f"{name} 出错"
