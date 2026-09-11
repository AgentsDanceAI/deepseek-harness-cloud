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


# --- 法律文书里的联系方式 ------------------------------------------------------
#
# 三个地址原先写死在 legal/ 下 8 份 markdown 里。它们是**合规入口**不是文案:
# 隐私政策那个收数据主体行权请求, AUP 那个收漏洞上报, 退款政策那个收退款申请 ——
# 换域名时只能换, 不能删, 而且换完必须真的有人收信。

LEGAL_DIR = Path(__file__).resolve().parents[2] / "legal"
LEGAL_DOCS = ("terms", "privacy", "refund", "aup")


@pytest.fixture()
def real_legal(monkeypatch):
    """默认测试环境挂的是空 legal 目录 (页面走"条款待发布"占位)。这几条要的是
    真文书。"""
    monkeypatch.setenv("DHC_LEGAL_DIR", str(LEGAL_DIR))


@pytest.mark.parametrize("doc", LEGAL_DOCS)
@pytest.mark.parametrize("lang", LANGS)
def test_no_unfilled_token_reaches_a_reader(client, real_legal, doc, lang):
    """占位符打错一个字, 用户读到的就是 `{{support_email}}` 本身 —— 页面照样 200,
    而这是一份法律文书。"""
    body = client.get(f"/legal/{doc}", headers={"accept-language": lang}).text
    assert "{{" not in body, f"/legal/{doc} ({lang}) 有没替换的占位符"


def test_every_token_in_the_documents_has_a_substitution():
    """反方向: 文书里写了个没人认识的占位符, 上一条测的是渲染结果, 这条在源头拦。"""
    import re

    from app.webpages import _LEGAL_TOKENS

    used = set()
    for f in LEGAL_DIR.glob("*.md"):
        used |= set(re.findall(r"\{\{[a-z_]+\}\}", f.read_text(encoding="utf-8")))
    unknown = sorted(used - set(_LEGAL_TOKENS))
    assert not unknown, f"legal/ 里用了没定义的占位符: {unknown}"


@pytest.mark.parametrize("doc", LEGAL_DOCS)
def test_changing_the_env_moves_every_address(client, real_legal, monkeypatch, doc):
    """换域名要是一个 env 就够 —— 否则下次又得改 8 份法律文书, 而改文书正文
    等于发布新版本条款。"""
    monkeypatch.setattr(config, "LEGAL_SUPPORT_EMAIL", "support@example.test")
    monkeypatch.setattr(config, "LEGAL_SECURITY_EMAIL", "security@example.test")
    monkeypatch.setattr(config, "LEGAL_PRIVACY_EMAIL", "legal@example.test")
    body = client.get(f"/legal/{doc}", headers={"accept-language": "zh"}).text
    assert "@agentsdance.ai" not in body, f"/legal/{doc} 还留着旧域的地址"


@pytest.mark.parametrize("doc", LEGAL_DOCS)
def test_each_document_still_names_a_way_to_reach_us(client, real_legal, doc):
    """ "隐藏品牌"最容易顺手把联系方式一起删掉。每篇文书都必须留至少一个能写信的
    地址 —— 没有的话, 退款/行权/上报三条通道当场断, 而且不报任何错。"""
    import re

    body = client.get(f"/legal/{doc}", headers={"accept-language": "zh"}).text
    assert re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", body), f"/legal/{doc} 上一个联系方式都没有"
