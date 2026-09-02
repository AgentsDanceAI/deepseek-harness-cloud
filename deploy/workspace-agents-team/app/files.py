"""文件面板的后端: 列目录 + 取文件。

这个产品的立足点是"产物落成工作区里的文件", 页面上却一直没有任何看文件的地方 ——
群里说"成片在 片/xxx/成片.mp4", 用户在界面上**够不到**。2026-09-02 老板验收第一句
就是"生成的内容在哪呢"。"落成文件"只做了一半: 文件在盘上, 不在用户眼前。

边界只有一条: **一个房间只能看自己那部片的目录** (老房间是根)。路径由用户/模型给,
resolve 之后必须仍在房间根之下, 否则 ValueError —— 和 media._safe_rel 同一套判法。
"""

from __future__ import annotations

import mimetypes
import pathlib

from . import filmdir, rooms

#: 根目录下这个是房间存档 (rooms.json), 不是用户的产物, 列表里藏掉。
_HIDDEN = {".agents-team"}


def root_for(room: rooms.Room) -> pathlib.Path:
    return filmdir.resolve(getattr(room, "dir", "") or "").resolve()


def safe(root: pathlib.Path, rel: str) -> pathlib.Path:
    """把相对路径钉在房间根之下; 越界抛 ValueError。"""
    p = (root / (rel or "")).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"路径越界: {rel}")
    return p


def listing(root: pathlib.Path, rel: str = "") -> dict:
    here = safe(root, rel)
    if not here.exists():
        raise FileNotFoundError(rel)
    if not here.is_dir():
        here = here.parent
    entries = []
    for p in sorted(here.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if p.name in _HIDDEN or p.name.startswith("."):
            continue
        st = p.stat()
        entries.append(
            {
                "name": p.name,
                "path": str(p.relative_to(root)),
                "dir": p.is_dir(),
                "size": 0 if p.is_dir() else st.st_size,
                "mtime": int(st.st_mtime),
                "kind": _kind(p),
            }
        )
    return {
        "dir": str(here.relative_to(root)) if here != root else "",
        "entries": entries,
    }


def _kind(p: pathlib.Path) -> str:
    """给前端选预览方式用: video / image / text / other。"""
    if p.is_dir():
        return "dir"
    mt = mimetypes.guess_type(p.name)[0] or ""
    if mt.startswith("video/"):
        return "video"
    if mt.startswith("image/"):
        return "image"
    if mt.startswith("text/") or p.suffix.lower() in {
        ".md",
        ".json",
        ".txt",
        ".csv",
        ".srt",
    }:
        return "text"
    return "other"
