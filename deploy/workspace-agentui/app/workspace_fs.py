"""文件树与读写, 以及 git。都限定在 /workspace 里。

限定不是防用户 —— 容器是他一个人的, 而且他手上就有一个能跑任意命令的 agent。
限定是防**路径拼接出错**: 前端传个 `../../root/.claude.json` 过来, 不设防的话
我们就把他的凭据渲染到页面上了。这类事故不需要有人恶意。
"""

from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(os.environ.get("DSH_WORKSPACE", "/workspace")).resolve()

#: 不进树的目录。放行的话 node_modules 一个就能让文件树卡死几十秒。
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".next", ".cache", "target", ".pytest_cache", ".mypy_cache"}
#: 超过这个大小就不当文本读 —— 前端拿不动, 浏览器也会卡。
MAX_TEXT = 512 * 1024


def _safe(rel: str) -> pathlib.Path:
    """把前端给的相对路径解析成 ROOT 内的真实路径, 越界就抛。

    必须 resolve 之后再判: 只做字符串前缀比较的话, 符号链接可以指到外面去,
    而 `..` 也能被绕过 (`/workspace/a/../../etc`)。
    """
    p = (ROOT / rel.lstrip("/")).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ValueError("路径越界")
    return p


def tree(rel: str = "") -> list[dict]:
    base = _safe(rel)
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        if entry.name in SKIP_DIRS:
            continue
        try:
            is_dir = entry.is_dir()
            size = 0 if is_dir else entry.stat().st_size
        except OSError:
            continue
        out.append({
            "name": entry.name,
            "path": str(entry.relative_to(ROOT)),
            "dir": is_dir,
            "size": size,
        })
    return out


def read(rel: str) -> dict:
    p = _safe(rel)
    if not p.is_file():
        return {"error": "不是文件"}
    if p.stat().st_size > MAX_TEXT:
        return {"error": f"文件太大 ({p.stat().st_size // 1024} KB), 不在浏览器里打开"}
    try:
        return {"path": rel, "text": p.read_text("utf-8")}
    except UnicodeDecodeError:
        return {"error": "二进制文件"}


def write(rel: str, text: str) -> dict:
    p = _safe(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, "utf-8")
    return {"ok": True, "path": rel}


def _git(*args: str, timeout: float = 20.0) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def git_status() -> dict:
    code, _ = _git("rev-parse", "--is-inside-work-tree")
    if code != 0:
        return {"repo": False}
    _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    _, porcelain = _git("status", "--porcelain")
    files = []
    for line in porcelain.splitlines():
        if len(line) > 3:
            files.append({"state": line[:2].strip(), "path": line[3:].strip()})
    return {"repo": True, "branch": branch.strip(), "files": files}


def git_diff(path: str = "") -> dict:
    args = ["diff", "--no-color"]
    if path:
        args += ["--", _safe(path).relative_to(ROOT).as_posix()]
    _, out = _git(*args)
    if not out.strip():
        # 没有未暂存的改动时看已暂存的 —— 否则用户点了有改动的文件却看到空白。
        args.insert(1, "--cached")
        _, out = _git(*args)
    return {"diff": out}


def git_commit(message: str, paths: list[str] | None = None) -> dict:
    if not message.strip():
        return {"error": "提交信息不能为空"}
    if paths:
        rels = [_safe(p).relative_to(ROOT).as_posix() for p in paths]
        code, out = _git("add", "--", *rels)
    else:
        code, out = _git("add", "-A")
    if code != 0:
        return {"error": out[-400:]}
    code, out = _git("commit", "-m", message)
    return {"ok": code == 0, "output": out[-800:]}
