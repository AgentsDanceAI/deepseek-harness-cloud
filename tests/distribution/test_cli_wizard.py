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
