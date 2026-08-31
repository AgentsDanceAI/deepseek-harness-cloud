"""会话清单。落 NAS, 因为闲置回收会把容器整个删掉。

只存**元数据**(标题/时间/CLI/会话 id) 和消息记录。真正的上下文由各个 CLI 自己
存 (Claude 在 ~/.claude/projects, Codex 在 ~/.codex) —— 那些目录也在 NAS 上,
所以 --resume 跨实例仍然有效。

写入用"先写临时文件再 rename": NFS 上直接覆写, 容器正好被回收的话会留下半截
JSON, 而下次启动读到它就是整个会话列表都没了。rename 在同一目录内是原子的。
"""

from __future__ import annotations

import json
import os
import pathlib
import time
import uuid

STORE = pathlib.Path(os.environ.get("DSH_STATE_DIR", "/root/.dsh-agentui"))


def _path(name: str) -> pathlib.Path:
    STORE.mkdir(parents=True, exist_ok=True)
    return STORE / name


def _read(name: str, fallback):
    p = _path(name)
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _write(name: str, data) -> None:
    p = _path(name)
    tmp = p.with_suffix(p.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def list_sessions() -> list[dict]:
    rows = _read("sessions.json", [])
    return sorted(rows, key=lambda r: r.get("updated", 0), reverse=True)


def create(cli: str, title: str = "") -> dict:
    rows = _read("sessions.json", [])
    row = {
        "id": uuid.uuid4().hex[:16],
        "cli": cli,
        "title": title or "新会话",
        "cli_session": "",  # 各 CLI 自己的会话 id, 拿来 --resume
        "created": time.time(),
        "updated": time.time(),
        "credits": 0,
    }
    rows.append(row)
    _write("sessions.json", rows)
    return row


def get(sid: str) -> dict | None:
    return next((r for r in _read("sessions.json", []) if r.get("id") == sid), None)


def update(sid: str, **fields) -> dict | None:
    rows = _read("sessions.json", [])
    for r in rows:
        if r.get("id") == sid:
            r.update(fields)
            r["updated"] = time.time()
            _write("sessions.json", rows)
            return r
    return None


def delete(sid: str) -> None:
    rows = [r for r in _read("sessions.json", []) if r.get("id") != sid]
    _write("sessions.json", rows)
    try:
        _path(f"msg-{sid}.json").unlink()
    except OSError:
        pass


def messages(sid: str) -> list[dict]:
    return _read(f"msg-{sid}.json", [])


def append(sid: str, msg: dict) -> None:
    rows = _read(f"msg-{sid}.json", [])
    rows.append(msg)
    # 一个会话留最近 400 条 —— NAS 上单文件太大读写都慢, 而更早的内容 CLI 自己
    # 的会话文件里还在, --resume 拿得回来。
    _write(f"msg-{sid}.json", rows[-400:])
