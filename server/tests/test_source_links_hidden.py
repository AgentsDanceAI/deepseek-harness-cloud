"""站点不再对外讲"我们的代码开源"时, 到底还漏不漏 (config.SHOW_SOURCE_LINKS)。

**这一条只能靠真渲染**: 模板里那些仓库地址一个字都没删, 全靠 `{% if %}` 罩着。
静态扫文件永远是绿的 —— 少罩一处、或者哪天有人挪了 include 的位置, 只有把页面
真渲染出来再搜才看得见。

同时钉住反方向的两件事, 它们**不是**宣传, 不许跟着一起收:
  * 页脚那句 MIT / 商标声明 —— MIT 再分发的前提条件, 收掉是许可违约;
  * GitHub **登录** —— 那是登录方式。删了等于把一批存量用户关在门外。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from . import test_webpages as _env

# 隔壁模块在 **import 期**就备好了测试环境 (DHC_DEV / AUTH_SECRET / 可写的
# DHC_DATA_DIR / 空 legal 目录)。app.config 是在自己 import 的那一刻读环境变量的,
# 所以这两行的先后顺序是硬的: 反过来就是 `OSError: Read-only file system: '/app'`。
# 这条空语句是给 isort 看的 —— 没有它, 下面两行会被并进上面那个 import 块重排。
_ENV_READY = _env is not None

from app import config  # noqa: E402
from app.main import app  # noqa: E402

I18N = Path(__file__).resolve().parent.parent / "config" / "i18n"

#: 关掉露出之后, 任何一个公开页面上都不该再出现这些。
FORBIDDEN = ("github.com/AgentsDanceAI", "star-badge")

#: 这些串来自只在露出打开时才渲染的文案键。逐字比对渲染结果, 不比对键名 ——
#: 键名在页面上本来就看不见, 比它等于什么也没比。
OSS_KEYS = (
    "home.oss.title",
    "home.oss.lede",
    "home.oss.repo",
    "home.oss.selfhost",
    "home.check.oss",
    "login.point.oss_title",
    "resources.selfhost.body",
    "resources.oss.title",
    "resources.oss.body",
    "pricing.team.selfhost_h3",
    "pricing.team.selfhost_note",
    "solutions.enterprise.title",
    "nav.s.enterprise_desc",
)

PUBLIC_PAGES = (
    "/",
    "/login",
    "/apps",
    "/pricing",
    "/solutions",
    "/resources",
    "/download",
    "/product",
)

LANGS = ("zh", "en")


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def oss_copy(lang: str) -> list[tuple[str, str]]:
    cat = json.loads((I18N / f"{lang}.json").read_text())
    return [(k, cat[k]) for k in OSS_KEYS if cat.get(k)]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
@pytest.mark.parametrize("lang", LANGS)
def test_no_repo_links_when_source_links_are_off(client, monkeypatch, path, lang):
    monkeypatch.setattr(config, "SHOW_SOURCE_LINKS", False)
    r = client.get(path, headers={"accept-language": lang})
    assert r.status_code == 200, path
    for needle in FORBIDDEN:
        assert needle not in r.text, f"{path} ({lang}) 仍然露出 {needle}"


@pytest.mark.parametrize("path", PUBLIC_PAGES)
@pytest.mark.parametrize("lang", LANGS)
def test_no_open_source_copy_when_off(client, monkeypatch, path, lang):
    monkeypatch.setattr(config, "SHOW_SOURCE_LINKS", False)
    body = client.get(path, headers={"accept-language": lang}).text
    for key, text in oss_copy(lang):
        assert text not in body, f"{path} ({lang}) 仍然渲染了 {key}"


def test_flipping_it_back_on_restores_the_section(client, monkeypatch):
    """开关要真的是开关 —— 否则"隐藏"和"删掉"就没区别了, 而可逆正是老板要的。"""
    monkeypatch.setattr(config, "SHOW_SOURCE_LINKS", True)
    assert "github.com/AgentsDanceAI" in client.get("/").text
    assert "git clone" in client.get("/resources").text


@pytest.mark.parametrize("on", (True, False))
def test_licence_notice_survives(client, monkeypatch, on):
    """MIT / 商标声明不是宣传, 是许可义务 —— 两个方向都必须在。"""
    monkeypatch.setattr(config, "SHOW_SOURCE_LINKS", on)
    body = client.get("/", headers={"accept-language": "zh"}).text
    assert "MIT" in body and "DeepSeek" in body, f"SHOW_SOURCE_LINKS={on} 时页脚声明没了"


def test_github_login_survives(client, monkeypatch):
    """GitHub 登录是登录方式, 不是源码链接。"""
    monkeypatch.setattr(config, "SHOW_SOURCE_LINKS", False)
    assert "/api/auth/github/start" in client.get("/login").text
