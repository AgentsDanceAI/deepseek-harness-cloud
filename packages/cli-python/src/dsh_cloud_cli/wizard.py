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
REPOSITORY = "https://github.com/AgentsDanceAI/deepseek-harness-cloud"


def command_prefix(*, resolved: str = "", entry: str = "") -> str:
    """怎么拼出"再跑我一次" —— 按这个进程实际是怎么被启动的来。

    直接印 `dsh-cloud …` 对绝大多数人都是错的: uvx 用完什么都不留在 PATH 上,
    从源码跑更不会。所以按事实推断 (名字真在 PATH 上吗 / argv 长什么样), 而不是
    假设大家都 `uv tool install` 过。纯函数, 两个事实由调用方给, 便于测试。
    """
    # uvx 在**运行期间**把自己的 bin 挂上 PATH, 进程一退就没了 —— 落在 uv 缓存里
    # 的那份不算"装过"。Node 侧 2026-08-26 实测被这个坑到: 面板印了裸 dsh-cloud,
    # 照着敲得到 command not found。
    def _ephemeral(path: str) -> bool:
        return "/uv/" in path or "\\uv\\" in path or "uvx" in path

    if resolved and not _ephemeral(resolved):
        return "dsh-cloud"
    if _ephemeral(resolved) or _ephemeral(entry):
        return "uvx dsh-cloud"
    return f"{sys.executable} -m dsh_cloud_cli"


def should_run_wizard(parsed: dict, *, is_tty: bool, fresh_init: bool) -> bool:
    """Only a brand-new deployment driven by a human gets asked."""
    if not fresh_init or not is_tty:
        return False
    options = parsed.get("options", {})
    if options.get("json") or options.get("dryRun") or options.get("yes"):
        return False
    if parsed.get("command") not in {"start", "init"}:
        return False
    # 试用模式一句不问: 那条命令的意义就是"一行装完看看长什么样", 而看的东西
    # (下载、云工作台) 本来就指向官方站点, 本机根本用不到上游密钥。想在本地真
    # 跑模型的人去改 .env。自部署才问 —— 没有 SMTP/OAuth 谁也注册不了第一个账号。
    return str(options.get("mode", "trial")) == "selfhost"


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
    for key, value in (answers.get("identity") or {}).items():
        if value:
            put(key, value)
    return merged


def next_steps(*, url: str, directory: str, has_upstream_key: bool,
               project_name: str = "dsh-selfhost", prefix: str = "dsh-cloud",
               work_domain: str = "", dev_mail: bool = True) -> str:
    """The closing panel. Pure so its content is testable.

    命令必须从任何目录粘过去都能跑: 取日志用 docker 而不是 `dsh-cloud logs`,
    后者既要求 CLI 在 PATH 上 (npx/uvx 用完就没了), 又依赖当前目录是部署目录
    的上一级 —— 两个前提对刚装完的人都不成立。up/down 显式带 --dir 同理。
    """
    lines = [
        "",
        "  云工作台已就绪",
        "",
        f"  打开    {url}",
    ]
    # 自部署配了 SMTP, 验证码是真发邮件的 —— 那时再说"去日志里捞"就是错的。
    if dev_mail:
        lines += [
            "  登录    用任意邮箱收验证码即可注册；本部署没有配邮件服务器，",
            "          验证码打印在服务端日志里：",
            f"            docker logs {project_name}-dhc-server-1 2>&1 | grep -A1 dev-mail",
        ]
    else:
        lines += ["  登录    用任意邮箱收验证码即可注册，验证码走你配置的邮件服务器。"]
    lines += [
        "",
    ]
    if not has_upstream_key:
        # 不再写成"注意/警告": 试用模式本来就不在本机跑模型 —— 下载与云工作台都
        # 指向官方站点, 没有上游密钥是**正常状态**而不是出错。
        lines += [
            f"  想在本机跑模型：把 UPSTREAM_API_KEY 填进 {directory}/.env，再执行一次上面的启动命令",
            "",
        ]
    if work_domain:
        # 工作台按 host 路由, 这条 DNS 记录不加的话它开着也访问不到。
        lines += [
            f"  还差一步  给 {work_domain} 加一条指向本机的 DNS 记录（云工作台按域名路由）",
            "",
        ]
    lines += [
        f"  配置    {directory}/.env",
        f"  重启    {prefix} up --dir {directory}",
        f"  停止    {prefix} down --dir {directory}（数据保留）",
        "",
        # 装完就走的人从没打开过仓库页 —— 14 天里 82 个克隆者对 3 个 star, 差距
        # 全在"没被邀请过"。给个链接让他自己点, 绝不代他操作账号。
        f"  觉得有用就给个 star：{REPOSITORY}",
        "",
    ]
    return "\n".join(lines)


def init_summary(*, directory: str, prefix: str = "dsh-cloud", mode: str = "trial",
                 work_domain: str = "") -> str:
    """``init`` 的收尾摘要。

    init 只写配置、不起容器, 所以不能用 next_steps (那张面板说的是"已就绪")。
    它原本无论如何都吐一整坨 JSON —— 那是给脚本解析的, 而人在终端前只想知道
    "写到哪了、下一步敲什么"。带 --json 或非 TTY 时仍然只吐 JSON。
    """
    lines = [
        "",
        "  配置已写入，容器还没起",
        "",
        f"  目录    {directory}",
        f"  启动    {prefix} up --dir {directory}",
        "",
    ]
    if mode == "selfhost":
        lines += [
            "  这是对外服务的配置：绑 0.0.0.0、占用 80/443、申请真证书，",
            "  请在目标服务器上启动，而不是本机。",
            "",
        ]
        if work_domain:
            lines += [f"  别忘了给 {work_domain} 加一条指向该服务器的 DNS 记录（云工作台按域名路由）", ""]
    lines += [f"  启动前可以先过一遍 {directory}/.env", ""]
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


def _prompt_identity(out) -> dict:
    """自部署的硬性要求: 没有 SMTP 或 OAuth 就没人能注册第一个账号, start 会直接
    拒绝运行。在这里问, 才是引导与一堵墙的区别。试用模式不问 —— 开发模式把
    验证码打到日志里。"""
    out.write("\n  登录方式（自部署必须配一种，否则没人能注册第一个账号）\n")
    out.write("    1) SMTP 邮件验证码\n")
    out.write("    2) GitHub OAuth\n")
    out.write("    3) Google OAuth\n")
    out.flush()
    choice = _ask("  选择 [1]: ")
    if choice in {"2", "3"}:
        vendor, name = ("GITHUB", "GitHub") if choice == "2" else ("GOOGLE", "Google")
        client_id = _ask(f"  {name} Client ID: ")
        secret = _secret(f"  {name} Client Secret（不回显）: ")
        return {f"{vendor}_LOGIN_CLIENT_ID": client_id, f"{vendor}_LOGIN_CLIENT_SECRET": secret}
    host = _ask("  SMTP 主机（如 smtp.example.com）: ")
    user = _ask("  SMTP 用户名: ")
    password = _secret("  SMTP 密码（不回显）: ")
    sender = _ask(f"  发件地址 [{user}]: ")
    return {
        "MAIL_SMTP_HOST": host,
        "MAIL_SMTP_USER": user,
        "MAIL_SMTP_PASS": password,
        "MAIL_FROM": sender or user,
    }


def prompt_answers(*, version: str, mode: str = "trial", out=sys.stdout) -> dict:
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

    identity = _prompt_identity(out) if mode == "selfhost" else {}

    out.write("\n")
    out.flush()
    return {
        "upstreamBase": upstream_base,
        "upstreamKey": upstream_key,
        "searchKey": search_key,
        "identity": identity,
    }
