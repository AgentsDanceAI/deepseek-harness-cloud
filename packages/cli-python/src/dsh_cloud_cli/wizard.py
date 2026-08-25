"""First-run setup wizard — the Python twin of ``src/wizard.mjs``.

Both installers advertise the same stack contract, so the guided first run has
to behave identically: same three questions, same defaults, same .env keys.

Two rules carried over from the CLI's existing stance:
  * secrets are read from the TTY with echo off (``getpass``), never from argv,
    where they would land in shell history and in every local user's ``ps``;
  * a non-interactive run (CI, --json, --yes, no TTY) asks nothing and behaves
    exactly as before, so automation never blocks on a hidden prompt.
"""

from __future__ import annotations

import getpass
import sys

DEEPSEEK_BASE = "https://api.deepseek.com/v1"


def should_run_wizard(parsed: dict, *, is_tty: bool, fresh_init: bool) -> bool:
    """Only a brand-new deployment driven by a human gets asked."""
    if not fresh_init or not is_tty:
        return False
    options = parsed.get("options", {})
    if options.get("json") or options.get("dryRun") or options.get("yes"):
        return False
    return parsed.get("command") in {"start", "init"}


def apply_answers(pairs: list[tuple[str, str]], answers: dict) -> list[tuple[str, str]]:
    """Merge answers into the generated (key, value) pairs. Pure."""
    merged = list(pairs)

    def put(key: str, value: str) -> None:
        for index, (existing, _) in enumerate(merged):
            if existing == key:
                merged[index] = (key, value)
                return
        merged.append((key, value))

    if answers.get("upstreamBase"):
        put("UPSTREAM_BASE_URL", answers["upstreamBase"])
    if answers.get("upstreamKey"):
        put("UPSTREAM_API_KEY", answers["upstreamKey"])
    if answers.get("searchKey"):
        put("SEARCH_PROVIDER", "zhipu")
        put("ZHIPU_SEARCH_API_KEY", answers["searchKey"])
    return merged


def next_steps(*, url: str, directory: str, has_upstream_key: bool) -> str:
    """The closing panel. Pure so its content is testable."""
    lines = [
        "",
        "  云工作台已就绪",
        "",
        f"  打开    {url}",
        "  登录    用任意邮箱收验证码即可注册；试用模式下没有配邮件服务器，",
        "          验证码打印在服务端日志里：",
        "            dsh-cloud logs | grep -A1 dev-mail",
        "",
    ]
    if not has_upstream_key:
        lines += [
            "  注意    还没有配模型上游，聊天会返回 503。把 UPSTREAM_API_KEY 填进",
            f"          {directory}/.env 后执行 dsh-cloud up 生效。",
            "",
        ]
    lines += [
        f"  配置    {directory}/.env（改完 dsh-cloud up 生效）",
        "  停止    dsh-cloud down（数据保留）",
        "",
    ]
    return "\n".join(lines)


def _ask(prompt: str) -> str:
    """Read one line; EOF (Ctrl-D, or `start < /dev/null`) means "take the default"."""
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        return ""


def _secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt).strip()
    except EOFError:
        print()
        return ""


def prompt_answers(*, version: str, out=sys.stdout) -> dict:
    """Run the three questions. Returns answers for apply_answers()."""
    out.write(f"\n  DSH Cloud {version} · 自部署引导\n")
    out.write("  按回车用默认值，任何一项都可以稍后在 .env 里改。\n\n")

    out.write("  模型上游（你自己的 OpenAI 兼容 API）\n")
    out.write(f"    1) DeepSeek 官方  {DEEPSEEK_BASE}\n")
    out.write("    2) 其他 OpenAI 兼容端点\n")
    out.flush()

    upstream_base = DEEPSEEK_BASE
    if _ask("  选择 [1]: ") == "2":
        entered = _ask("  端点 URL（形如 https://host/v1）: ")
        if entered:
            upstream_base = entered.rstrip("/")

    upstream_key = _secret("  API Key（不回显，回车可跳过）: ")

    out.write("\n  联网搜索（可选，智谱 open.bigmodel.cn；回车跳过）\n")
    out.flush()
    search_key = _secret("  搜索 API Key: ")

    out.write("\n")
    out.flush()
    return {"upstreamBase": upstream_base, "upstreamKey": upstream_key, "searchKey": search_key}
