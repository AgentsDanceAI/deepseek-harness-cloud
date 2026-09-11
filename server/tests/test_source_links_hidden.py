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
import re
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
ROOT_DIR = Path(__file__).resolve().parents[2]

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
)

#: 「私有部署」2026-09-11 重新露出 —— 它讲的是"装进你自己的边界", 与代码开不开源
#: 无关, 所以**不**跟着 SHOW_SOURCE_LINKS 走。但它的老文案整段是靠"开源代码"讲的,
#: 所以改写之后要钉住: 这几条文案里不许再出现"开源 / open source"。
PRIVATE_DEPLOY_KEYS = (
    "nav.s.enterprise",
    "nav.s.enterprise_desc",
    "solutions.enterprise.title",
    "solutions.enterprise.body",
    "solutions.enterprise.cta",
    "solutions.lede",
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


def test_the_mit_notice_lives_where_the_obligation_actually_is():
    """MIT 的义务挂在**分发软件副本**上, 不在网站上。

    条件句原文: "The above copyright notice and this permission notice shall be
    included in all copies or substantial portions of the Software." 桌面包确实
    再分发了 deepseek-harness 与 deepseek-harness-desktop, 那份义务由随包走的
    legal/THIRD_PARTY_NOTICES.md 履行; 网站是托管服务, 不分发副本。

    这条测试原先断言的是**页脚**上有 MIT 字样 —— 那是照着我一个错的前提写的
    (我把"随包的声明文件"和"网页页脚"混成了一件事, 还拿它挡过好几次改动)。
    页脚那句 2026-09-11 已去掉 (它还说站点"基于 DeepSeek Harness 构建", 而站点
    现在是 16 格货架)。真正该钉住的是这里。
    """
    notices = (ROOT_DIR / "legal" / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert notices.count("MIT License") >= 2, "随桌面包分发的 MIT 许可全文没了 —— 这是许可违约"
    assert "shall be included in all" in notices, "MIT 的条件句被删了"
    assert "deepseek-harness" in notices, "没说清再分发的是哪个项目"
    assert "not affiliated" in notices or "无隶属" in notices, "商标免责没了"


def test_the_site_disclaims_every_third_party_mark_not_just_one(client, real_legal):
    """货架上摆着十几个第三方产品名。页脚原先只声明 DeepSeek 一个 —— 单点一个
    反而暗示其余的是我们的。现在由条款 15.5 一条覆盖全部。"""
    body = client.get("/legal/terms", headers={"accept-language": "zh"}).text
    assert "第三方产品名称" in body and "指示性使用" in body, "条款里的通用商标条款没了"
    assert "隶属" in body, "无隶属/背书声明没了"


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


# --- 运营方与服务名 ------------------------------------------------------------


@pytest.mark.parametrize("doc", LEGAL_DOCS)
@pytest.mark.parametrize("lang", LANGS)
def test_operator_name_comes_from_config_not_the_document(client, real_legal, monkeypatch, doc, lang):
    """运营方名原先写死在四篇文书里 (`| 法律实体 | AgentsDance AI |`)。改一次品牌
    要动四份法律文件, 而改文书正文 = 发布新版本条款。"""
    monkeypatch.setattr(config, "LEGAL_ENTITY_ZH", "某运营方")
    monkeypatch.setattr(config, "LEGAL_ENTITY_EN", "Some Operator")
    body = client.get(f"/legal/{doc}", headers={"accept-language": lang}).text
    assert "AgentsDance" not in body, f"/legal/{doc} ({lang}) 还写死着旧主体名"


def test_english_pages_use_the_english_entity(client, monkeypatch):
    """LEGAL_ENTITY_EN 配了却从来没人读 —— 英文页脚一直显示中文那份。"""
    monkeypatch.setattr(config, "LEGAL_ENTITY_ZH", "中文署名")
    monkeypatch.setattr(config, "LEGAL_ENTITY_EN", "English Operator")
    assert "English Operator" in client.get("/", headers={"accept-language": "en"}).text
    assert "中文署名" in client.get("/", headers={"accept-language": "zh"}).text


@pytest.mark.parametrize("doc", LEGAL_DOCS)
@pytest.mark.parametrize("lang", LANGS)
def test_documents_do_not_call_the_service_by_its_repo_name(client, real_legal, doc, lang):
    """四篇文书的第一句原先写的是仓库名 `deepseek-harness-cloud` —— 没有一个用户
    见过这个名字, 而合同里"本服务"这个定义项就锚在它上面。"""
    body = client.get(f"/legal/{doc}", headers={"accept-language": lang}).text
    assert "deepseek-harness-cloud" not in body, f"/legal/{doc} ({lang}) 还在用仓库名当服务名"


def visible_text(html: str) -> str:
    """把标签剥掉, 只留读者真能看见的字。

    量的是**看得见的正文**, 不是源码: `mailto:` 的 href 里仍然有地址 (不然"联系
    我们"这个按钮就只是个死链), 而老板要的是页面上读不到。两者是不同的东西,
    断言要落在后者上。
    """
    return re.sub(r"<[^>]*>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html))


@pytest.mark.parametrize("path", PUBLIC_PAGES)
@pytest.mark.parametrize("lang", LANGS)
def test_no_brand_name_anywhere_on_a_public_page(client, monkeypatch, path, lang):
    """老板两次说过: AgentsDance 这四个字不许再出现在页面上, 全称也不行。

    **页脚在每一页上**, 所以这条要逐页测 —— 一处漏掉就是全站漏掉。
    **大小写不敏感**: 第一版只比了 `AgentsDance`, 而漏掉的那两处是
    `support@agentsdance.ai` 里的小写, 测试当场是绿的。

    **联系邮箱必须在这里显式灌成带品牌的地址。** 默认测试环境里
    `LEGAL_CONTACT_EMAIL` 是空串, 于是 `{{ legal_contact_email }}` 渲染成空 ——
    把 /download 那行改回"显示地址"做变异验证时, 测试照样全绿。测试环境比生产
    少一个配置, 这条断言就等于没写。
    """
    monkeypatch.setattr(config, "SHOW_SOURCE_LINKS", False)
    monkeypatch.setattr(config, "LEGAL_ENTITY_ZH", "AI Store")
    monkeypatch.setattr(config, "LEGAL_ENTITY_EN", "AI Store")
    monkeypatch.setattr(config, "LEGAL_CONTACT_EMAIL", "support@agentsdance.ai")
    seen = visible_text(client.get(path, headers={"accept-language": lang}).text).lower()
    assert "agentsdance" not in seen, f"{path} ({lang}) 页面上读得到 AgentsDance"
    assert "灵舞" not in seen, f"{path} ({lang}) 页面上读得到中文全称"


@pytest.mark.parametrize("lang", LANGS)
def test_private_deployment_copy_never_mentions_open_source(lang):
    """私有部署这块是**重新露出**的, 不受 SHOW_SOURCE_LINKS 保护 —— 所以它的
    文案本身必须干净。老文案原话是"代码开源。把它部署到你自己的服务器…",
    照抄回来就等于把刚收起来的话从另一个入口放回站上。"""
    cat = json.loads((I18N / f"{lang}.json").read_text())
    dirty = {k: cat[k] for k in PRIVATE_DEPLOY_KEYS if re.search(r"开源|open.?source", cat.get(k, ""), re.I)}
    assert not dirty, f"私有部署文案里还留着开源字样: {dirty}"


def test_private_deployment_is_reachable(client, monkeypatch):
    """导航里那条和 /solutions 上那张卡必须同时在 —— 只留一个就是"有入口没落点"
    或者"有内容没人找得到"。"""
    monkeypatch.setattr(config, "SHOW_SOURCE_LINKS", False)
    nav = client.get("/", headers={"accept-language": "zh"}).text
    page = client.get("/solutions", headers={"accept-language": "zh"}).text
    cat = json.loads((I18N / "zh.json").read_text())
    assert "/solutions#enterprise" in nav, "导航里没有私有部署入口"
    assert cat["solutions.enterprise.title"] in page, "/solutions 上没有私有部署那张卡"
    assert 'id="enterprise"' in page and 'id="contact"' in page, "锚点缺失, 导航点过去会落空"
