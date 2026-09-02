"""这一棒该在哪个目录里干活 —— **每部片一个目录**。

2026-09-01 验收跑废在这里: 所有房间共用扁平的 `/workspace`, 第二部片一开机就看见
上一部的 `角色/` `场景/` 和 S01..S20.mp4 ——

  · 美术照"同一角色只做一张权威图"的铁律**拒绝重做** (它的判断完全正确, 它只是
    不知道那是上一部片的人);
  · 出片的去重闸把上一部的成片当成"这镜已经出过了"直接跳过。

于是"新片"跑完等于把旧片重剪一遍, 而**全程没有一处报错** —— 日志、聊天、文件树
看上去都正常, 只有对着片子看才发现人物和剧本对不上。

用 ContextVar 而不是给每个工具加参数: 每个房间的一轮跑在自己的 asyncio 任务里,
contextvar 天然隔离, 不用把 room 一路穿过 run_turn -> dispatch -> 每个工具的签名。

**老房间不迁移**: `Room.dir` 空字符串 = 还用扁平的根目录 (老 rooms.json 里没有这个
键, 读出来就是空)。用户手上那部已经跑到 S20 的片不会凭空消失, 也不用写迁移代码。
"""

from __future__ import annotations

import contextvars
import os
import pathlib
import re

#: 工作区根。与 products.py 里的 mounts 一致 —— 改一处必须改另一处。
ROOT = pathlib.Path(os.environ.get("AGENTS_TEAM_WORKDIR", "/workspace"))

#: 片子们放在根下这个目录里, 而不是直接摊在根上 —— 根上还有老片的散件。
FILMS = "片"

#: 默认存 None 而不是 ROOT: ContextVar 的 default 在创建时就定死了, 而 ROOT 是
#: 会被改写的 (镜像自检把根指到临时目录再跑一遍工具)。存 None、取值时回落到当前
#: 的 ROOT, 改写才生效 —— 否则自检的覆盖被静默忽略, 而工具"看起来"都成功了。
_CUR: contextvars.ContextVar[pathlib.Path | None] = contextvars.ContextVar(
    "film_dir", default=None
)

#: 文件名里留下汉字、字母数字和连字符; 其余一律折成 "-"。片名是用户随手起的,
#: 里面有空格、引号、斜杠都很正常, 直接拿去当目录名会炸或者穿出去。
_UNSAFE = re.compile(r"[^\w一-鿿-]+")


def current() -> pathlib.Path:
    """当前这一棒的工作目录。没设过就是根 (老房间、单元测试、镜像自检)。"""
    return _CUR.get() or ROOT


def use(path: pathlib.Path) -> contextvars.Token:
    return _CUR.set(path)


def reset(token: contextvars.Token) -> None:
    _CUR.reset(token)


def slug_for(room_id: str, name: str, prefix: str = FILMS) -> str:
    """给新房间算一个目录名, 存进 Room.dir。

    带上 room_id 是因为片名可以重复 —— 两部都叫"新片"的话, 不带 id 就又共用一个
    目录了, 那正是本模块要治的病。prefix 是团队模板的产物目录 (片/报告/店铺…),
    不同团队的产物分开放, 文件面板里一眼分得出这是哪类东西。
    """
    s = _UNSAFE.sub("-", (name or "").strip()).strip("-")[:24].strip("-")
    top = _UNSAFE.sub("-", (prefix or FILMS).strip()).strip("-") or FILMS
    return f"{top}/{s}-{room_id}" if s else f"{top}/{room_id}"


def resolve(rel: str) -> pathlib.Path:
    """把 Room.dir 变成真路径; 空串 = 根 (老房间)。"""
    return (ROOT / rel) if rel else ROOT
