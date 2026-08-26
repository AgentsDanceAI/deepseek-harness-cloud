"""首次运行引导 (Python 侧)。Node 侧的对应用例在 packages/cli-npm/test/wizard.test.mjs。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages/cli-python/src"))

from dsh_cloud_cli import wizard  # noqa: E402


def parsed(command, **options):
    return {"command": command, "options": options, "positionals": []}


def test_wizard_runs_only_for_a_fresh_interactive_start_or_init():
    assert wizard.should_run_wizard(parsed("start"), is_tty=True, fresh_init=True)
    assert wizard.should_run_wizard(parsed("init"), is_tty=True, fresh_init=True)
    # 已初始化的目录不能再问 —— 那等于诱导用户覆盖自己的配置
    assert not wizard.should_run_wizard(parsed("start"), is_tty=True, fresh_init=False)
    assert not wizard.should_run_wizard(parsed("up"), is_tty=True, fresh_init=True)


def test_automation_never_blocks_on_a_hidden_prompt():
    for options in ({"json": True}, {"dryRun": True}, {"yes": True}):
        assert not wizard.should_run_wizard(parsed("start", **options), is_tty=True, fresh_init=True)
    assert not wizard.should_run_wizard(parsed("start"), is_tty=False, fresh_init=True)


def test_answers_replace_placeholders_in_place():
    pairs = [("DOMAIN", "localhost"), ("UPSTREAM_BASE_URL", "https://api.deepseek.com/v1"),
             ("UPSTREAM_API_KEY", ""), ("SEARCH_PROVIDER", "zhipu"), ("ZHIPU_SEARCH_API_KEY", "")]
    out = wizard.apply_answers(pairs, {"upstreamBase": "https://gw.example.com/v1",
                                       "upstreamKey": "sk-x", "searchKey": "zp-y"})
    keys = [k for k, _ in out]
    assert keys.count("UPSTREAM_API_KEY") == 1, "不能重复追加同名键"
    assert dict(out)["UPSTREAM_BASE_URL"] == "https://gw.example.com/v1"
    assert dict(out)["UPSTREAM_API_KEY"] == "sk-x"
    assert dict(out)["ZHIPU_SEARCH_API_KEY"] == "zp-y"
    assert dict(out)["DOMAIN"] == "localhost", "无关行原样保留"


def test_skipped_answers_leave_placeholders_untouched():
    pairs = [("UPSTREAM_BASE_URL", "https://api.deepseek.com/v1"), ("UPSTREAM_API_KEY", "")]
    assert wizard.apply_answers(pairs, {}) == pairs
    assert wizard.apply_answers(pairs, {"upstreamKey": ""}) == pairs


def test_closing_panel_tells_the_user_where_the_sign_in_code_went():
    panel = wizard.next_steps(url="http://localhost:8787", directory="/x", has_upstream_key=True)
    assert "http://localhost:8787" in panel
    # 2026-08-25 验收: 验证码走日志而页面不说, 用户首次登录必卡
    assert "dev-mail" in panel
    # 命令必须从任何目录都能粘: 不依赖 CLI 在 PATH 上, 也不依赖 cwd
    assert "docker logs" in panel, "取码要用 docker, 不能用 dsh-cloud logs"
    assert "--dir /x" in panel, "up/down 必须显式带 --dir"
    assert "503" not in panel
    # 装完就走的人从没打开过仓库页, 也就从没被邀请过 —— 给链接, 不代他点
    assert "star" in panel and "github.com/AgentsDanceAI/deepseek-harness-cloud" in panel


def test_closing_panel_warns_when_chat_would_answer_503():
    panel = wizard.next_steps(url="http://localhost:8787", directory="/x", has_upstream_key=False)
    assert "503" in panel and "UPSTREAM_API_KEY" in panel


def test_prompts_collect_a_custom_endpoint(monkeypatch, capsys):
    answers = iter(["2", "https://gw.example.com/v1/"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": "sk-secret")
    result = wizard.prompt_answers(version="0.2.0")
    assert result["upstreamBase"] == "https://gw.example.com/v1", "尾部斜杠要规范化"
    assert result["upstreamKey"] == "sk-secret"
    assert "sk-secret" not in capsys.readouterr().out, "密钥绝不能出现在屏幕上"


def test_eof_mid_question_falls_back_to_defaults(monkeypatch):
    """Ctrl-D 或 `start < /dev/null` 必须取默认值继续, 而不是永久挂起。"""
    def boom(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    monkeypatch.setattr(wizard.getpass, "getpass", boom)
    result = wizard.prompt_answers(version="0.2.0")
    assert result["upstreamBase"] == "https://api.deepseek.com/v1"
    assert result["upstreamKey"] == ""


def _answers(monkeypatch, plain, secrets):
    plain_it, secret_it = iter(plain), iter(secrets)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(plain_it))
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": next(secret_it))


def test_trial_mode_never_asks_about_login(monkeypatch):
    _answers(monkeypatch, [""], ["", ""])
    result = wizard.prompt_answers(version="0.2.0")
    assert result["identity"] == {}, "试用模式验证码走日志, 问登录纯属噪音"


def test_selfhost_collects_smtp_so_the_first_account_can_exist(monkeypatch, capsys):
    # 自部署没有 SMTP/OAuth 就没人能注册, start 会硬拒 —— 引导必须问到
    _answers(monkeypatch, ["", "1", "smtp.example.com", "bot@example.com", ""], ["", "", "pw"])
    identity = wizard.prompt_answers(version="0.2.0", mode="selfhost")["identity"]
    assert identity["MAIL_SMTP_HOST"] == "smtp.example.com"
    assert identity["MAIL_SMTP_PASS"] == "pw"
    assert identity["MAIL_FROM"] == "bot@example.com", "发件地址留空时回落到用户名"
    assert "pw" not in capsys.readouterr().out, "SMTP 密码不能回显"


def test_selfhost_can_pick_oauth_instead(monkeypatch):
    _answers(monkeypatch, ["", "2", "client-id"], ["", "", "client-secret"])
    identity = wizard.prompt_answers(version="0.2.0", mode="selfhost")["identity"]
    assert identity["GITHUB_LOGIN_CLIENT_ID"] == "client-id"
    assert identity["GITHUB_LOGIN_CLIENT_SECRET"] == "client-secret"
    assert "MAIL_SMTP_HOST" not in identity


def test_identity_answers_land_in_the_env():
    out = wizard.apply_answers(
        [("MAIL_SMTP_HOST", ""), ("MAIL_FROM", "")],
        {"identity": {"MAIL_SMTP_HOST": "smtp.example.com", "MAIL_FROM": "bot@example.com"}},
    )
    assert dict(out)["MAIL_SMTP_HOST"] == "smtp.example.com"
    assert [k for k, _ in out].count("MAIL_SMTP_HOST") == 1


def test_printed_command_matches_how_the_process_was_started():
    # 装了才用裸名字
    assert wizard.command_prefix(resolved="/usr/local/bin/dsh-cloud") == "dsh-cloud"
    # uvx 用完 PATH 上什么都没有
    assert wizard.command_prefix(entry="/Users/x/.cache/uv/archive/bin/dsh-cloud") == "uvx dsh-cloud"
    # 运行期间它确实在 PATH 上, 但那份住在 uv 缓存里, 进程一退就没了
    assert wizard.command_prefix(resolved="/Users/x/.cache/uv/archive/bin/dsh-cloud") == "uvx dsh-cloud"
    # 从源码跑: 印出那条真的能用的调用
    assert wizard.command_prefix(entry="/repo/src/dsh_cloud_cli/__main__.py").endswith("-m dsh_cloud_cli")


def test_panel_uses_the_resolved_prefix_everywhere():
    panel = wizard.next_steps(url="u", directory="/x", has_upstream_key=True, prefix="uvx dsh-cloud")
    assert "uvx dsh-cloud up --dir /x" in panel
    assert "uvx dsh-cloud down --dir /x" in panel


def test_selfhost_panel_names_the_dns_record_the_workspace_needs():
    # 工作台按 host 路由, 这条记录不加它开着也打不开 —— 装完必须点名
    panel = wizard.next_steps(url="https://dsh.example.com", directory="/x",
                              has_upstream_key=True, work_domain="work.dsh.example.com")
    assert "work.dsh.example.com" in panel and "DNS" in panel


def test_trial_panel_says_nothing_about_dns():
    panel = wizard.next_steps(url="http://localhost:8787", directory="/x", has_upstream_key=True)
    assert "DNS" not in panel, "试用模式没有工作台, 提 DNS 是噪音"


def test_selfhost_env_turns_the_workspace_on_trial_leaves_it_off(tmp_path):
    """自部署有域名就默认开工作台; 试用模式必定关 (cookie 过不去 work.localhost)。"""
    import json as _json
    import subprocess as _sp
    import sys as _sys

    def env_of(*args):
        target = tmp_path / args[0]
        _sp.run([_sys.executable, "-m", "dsh_cloud_cli", "init", str(target), "--yes", "--json", *args[1:]],
                cwd=ROOT, env={**__import__("os").environ,
                               "PYTHONPATH": str(ROOT / "packages/cli-python/src")},
                check=True, capture_output=True)
        return dict(line.split("=", 1) for line in
                    (target / ".env").read_text(encoding="utf-8").splitlines() if "=" in line)

    selfhost = env_of("s", "--mode", "selfhost", "--domain", "dsh.example.com", "--admin-email", "me@x.com")
    assert selfhost["WORK_ENABLED"] == "1"
    assert selfhost["WORK_DOMAIN"] == "work.dsh.example.com"
    assert selfhost["COOKIE_DOMAIN"] == ".dsh.example.com", "少了前导点会话带不到子域, 表现为无限跳登录页"
    assert selfhost["COMPOSE_PROFILES"] == "work"

    trial = env_of("t", "--mode", "trial")
    assert trial["WORK_ENABLED"] == "0"
    assert trial["COMPOSE_PROFILES"] == "", "试用模式不该启动 socket 代理"


def test_selfhost_does_not_tell_you_to_fish_the_code_out_of_the_log():
    # 自部署配了 SMTP, 验证码真发邮件 —— 那时再说"去日志里捞"就是错的
    panel = wizard.next_steps(url="https://x", directory="/d", has_upstream_key=True, dev_mail=False)
    assert "dev-mail" not in panel
    assert "你配置的邮件服务器" in panel


def test_init_summary_says_what_init_actually_did():
    # init 只写配置不起容器 —— 不能沿用"已就绪"那张面板
    panel = wizard.init_summary(directory="/x", prefix="uvx dsh-cloud")
    assert "容器还没起" in panel
    assert "uvx dsh-cloud up --dir /x" in panel
    assert "已就绪" not in panel


def test_selfhost_init_summary_warns_it_is_not_for_this_machine():
    panel = wizard.init_summary(directory="/x", mode="selfhost", work_domain="work.a.com")
    assert "80/443" in panel and "目标服务器" in panel
    assert "work.a.com" in panel and "DNS" in panel
    assert "80/443" not in wizard.init_summary(directory="/x")
