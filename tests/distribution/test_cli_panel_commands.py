"""收尾面板印出来的命令, 必须真的能跑。

2026-08-25 连着两轮实测失败, 都是这一类: 面板印 `dsh-cloud logs …`, 而
① CLI 不在 PATH 上 (npx/uvx 跑完什么都不留), ② 它默认 --dir ./dsh-cloud,
所以必须站在部署目录的上一级。单测把面板文本对得再准也照样漏 —— 那些用例
断言的是"字符串里有没有这几个词", 而缺陷在于"这条命令在用户的环境里跑不动"。

所以这里换个断言方式: 把面板里的命令**原样抓出来执行**, 且刻意制造用户的
真实处境 —— 换一个工作目录、PATH 里没有 dsh-cloud。docker 用桩顶替, 于是
整条 CLI 代码路径 (参数解析 / --dir 解析 / 状态读取 / 面板打印) 都真的跑,
只有最后那次容器调用是空操作。
"""

from __future__ import annotations

import os
import pty
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE_BIN = ROOT / "packages/cli-npm/bin/dsh-cloud.mjs"


# 服务端在开发模式下打给日志的样子 (server/app/accounts.py 的 _send_mail)。
# 桩按原样吐出来, 面板里那条 grep 的模式对不对也就一并验了。
_DEV_MAIL_SAMPLE = (
    "[dev-mail] to=someone@example.com subject=DSH Cloud 登录验证码\n"
    "您的登录验证码是 145210，10 分钟内有效。若非本人操作请忽略。\n"
)


def _stub_docker(directory: Path) -> Path:
    """顶替 docker: 让 CLI 走完全程而不真起容器。

    `logs` 子命令吐一段与线上一致的开发模式邮件日志, 其余一律静默成功 ——
    于是面板里的取码命令是被**当作一条完整管道**验证的, 而不只是"docker 能跑"。
    """
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / "docker"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "logs" ]; then\n'
        f"  printf '%s' '{_DEV_MAIL_SAMPLE}'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _run_in_pty(argv: list[str], *, cwd: Path, env: dict) -> tuple[int, str]:
    """在 pty 里跑 —— 面板只在 stdout 是终端时才打印。

    不能用 pty.spawn: 它既不接受 cwd 也不接受 env, 会照搬当前进程的环境 ——
    于是桩 docker 失效、真 docker 上阵。自己开 pty 再交给 subprocess 才能同时
    拿到"stdout 是终端"和"环境由我说了算"。
    """
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=slave, stderr=slave, close_fds=True,
        )
        os.close(slave)
        out = bytearray()
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out.extend(chunk)
        code = process.wait()
    finally:
        os.close(master)
    return code, out.decode("utf-8", "replace").replace("\r\n", "\n")


def _panel_commands(panel: str) -> list[str]:
    """抓出面板里所有可执行的命令行。"""
    commands = []
    for line in panel.splitlines():
        text = line.strip()
        if text.startswith("docker logs "):
            commands.append(text)
            continue
        # "  重启    <命令>" / "  停止    <命令>（数据保留）"
        match = re.match(r"^(?:重启|停止)\s{2,}(.+?)(?:（.*）)?$", text)
        if match:
            commands.append(match.group(1).strip())
    return commands


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_panel_commands_run_from_anywhere_without_the_cli_on_path(tmp_path: Path):
    node_dir = str(Path(shutil.which("node")).parent)  # type: ignore[arg-type]
    stub_dir = tmp_path / "stub-bin"
    _stub_docker(stub_dir)
    # 用户的真实处境: PATH 上有 node 和 docker, 唯独没有 dsh-cloud。
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{node_dir}:/usr/bin:/bin",
        "DSH_CLOUD_TEST_RANDOM_HEX": "0" * 64,
    }
    assert shutil.which("dsh-cloud", path=env["PATH"]) is None, "前提: dsh-cloud 不在 PATH 上"

    # 独占的项目名。默认的 dsh-selfhost 会和开发机上真在跑的部署撞名 ——
    # 2026-08-25 这条测试的第一版就用 `up` 把本机那套的容器改指到了临时目录。
    project = f"dsh-paneltest-{os.getpid()}"
    target = tmp_path / "deploy"
    init = subprocess.run(
        ["node", str(NODE_BIN), "init", str(target), "--mode", "trial", "--yes", "--json",
         "--project-name", project],
        cwd=ROOT, env=env, text=True, capture_output=True,
    )
    assert init.returncode == 0, init.stderr

    # 刻意换一个与部署目录无关的工作目录 —— 面板命令不该依赖 cwd。
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    code, panel = _run_in_pty(
        ["node", str(NODE_BIN), "up", "--dir", str(target), "--project-name", project],
        cwd=elsewhere, env=env,
    )
    assert code == 0, panel
    assert "云工作台已就绪" in panel, f"没拿到面板:\n{panel}"

    commands = _panel_commands(panel)
    assert len(commands) >= 3, f"面板里没抓到命令:\n{panel}"
    for command in commands:
        result = subprocess.run(command, shell=True, cwd=elsewhere, env=env,
                                text=True, capture_output=True)
        assert result.returncode == 0, (
            f"面板给的命令跑不了:\n  {command}\n  退出码 {result.returncode}\n  {result.stderr.strip()}"
        )
