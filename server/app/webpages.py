"""Server-rendered web console: landing, auth pages, device activation,
dashboard, pricing, orders, legal, download.

Pages are Jinja2-rendered shells; interactivity is small vanilla JS in
static/app.js that talks to the JSON APIs. This router is included LAST by
main.py. All templates share templates/base.html.
"""

from __future__ import annotations

import html
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, credits, plans
from .accounts import try_resolve_user
from .redirects import safe_local_path

router = APIRouter(include_in_schema=False)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
# Cache-buster for static assets: CDN edges (Cloudflare) cache /static/* — a
# version query derived from file mtimes makes every deploy a fresh URL.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
try:
    ASSET_V = str(int(max(f.stat().st_mtime for f in _STATIC_DIR.rglob("*") if f.is_file())))
except ValueError:
    ASSET_V = "0"


# repo-root legal/ documents (terms.zh.md, ...). Overridable for tests/deploys.
def _legal_dir() -> Path:
    # resolved per request so tests/deploys can repoint via env at any time
    return Path(os.environ.get("DHC_LEGAL_DIR") or Path(__file__).resolve().parents[2] / "legal")


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# --- shared context ----------------------------------------------------------


def _ctx(request: Request, page: str, **extra) -> dict:
    try:
        user = try_resolve_user(request)
    except Exception:
        user = None

    _cur_ctx = _currency_ctx(request)
    ctx = {
        "request": request,
        "page": page,
        "user": user,
        "public_base": config.PUBLIC_BASE,
        "icp_number": config.ICP_NUMBER,
        "psb_number": config.PSB_NUMBER,
        "legal_entity_zh": config.LEGAL_ENTITY_ZH,
        "legal_contact_email": config.LEGAL_CONTACT_EMAIL,
        "year": time.localtime().tm_year,
        "asset_v": ASSET_V,
        **_i18n_ctx(request),
        **_cur_ctx,
        # Templates link to /dl/<key>; these flags only say whether a build
        # exists, so a platform with no artifact is shown as unavailable rather
        # than as a link that 404s.
        "has_mac_arm64": bool(download_url("mac-arm64")),
        "has_mac_x64": bool(download_url("mac-x64")),
        "has_win_x64": bool(download_url("win-x64")),
        "has_win_arm64": bool(download_url("win-arm64")),
        "has_android": bool(download_url("android")),
        # 页面据此决定说什么: 自部署默认不开云工作台 (要独立域名 + docker
        # socket 代理), 那时再劝人"去用云工作台"就是把他送去 /work, 而
        # /work 又跳回 /download, 绕成死循环。
        "work_enabled": config.WORK_ENABLED,
        # 只在"本部署不是托管版自己"时才显示托管版入口, 否则线上会给自己
        # 挂一个指向自己的按钮。
        "hosted_site": config.HOSTED_SITE
        if config.HOSTED_SITE not in ("", config.PUBLIC_BASE.rstrip("/"))
        else "",
        "work_idle_stop_min": config.WORK_IDLE_STOP_MIN,
        "work_free_minutes": config.WORK_FREE_MINUTES,
        **_stars_ctx(),
        # Seat price follows the same currency as everything else on the page;
        # a EUR page quoting a USD seat fee was the same mismatch in miniature.
        **_team_terms_ctx(_cur_ctx.get("currency")),
    }
    ctx.update(extra)
    return ctx


def _switch_url(request: Request, **overrides) -> str:
    """A link to this same page with one query parameter changed.

    The switchers used bare `?lang=en` hrefs, which replace the WHOLE query
    string — switching language on /pricing?cur=CNY silently dropped the
    currency. Keeping the other parameters is what makes the two pickers
    independent of each other.
    """
    from urllib.parse import urlencode

    params = dict(request.query_params)
    params.update(overrides)
    q = urlencode(params)
    return f"{request.url.path}?{q}" if q else request.url.path


def _currency_ctx(request: Request) -> dict:
    """Currency shown to this visitor, and the table that goes with it."""
    from . import currency as _cur

    cur, _explicit = _cur.resolve(request)
    return {
        "currency": cur,
        "currency_symbol": _cur.symbol(cur),
        "supported_currencies": _cur.SUPPORTED,
        # The picker exists because an explicit ?cur= sticks for a year in a
        # cookie: without a visible way back, one link pinned a visitor to a
        # currency their country would never have chosen.
        "currency_glyph": _cur.glyph(cur),
        "currency_options": [
            {"code": c, "symbol": _cur.glyph(c), "active": c == cur, "href": _switch_url(request, cur=c)}
            for c in _cur.SUPPORTED
        ],
        "currency_auto": _cur.from_country(request.headers.get("cf-ipcountry", "")) or _cur.DEFAULT,
    }


def _i18n_ctx(request: Request) -> dict:
    """Language and the bound translator every template uses."""
    from . import i18n

    lang, _explicit = i18n.resolve(request)
    # Only the js.* namespace crosses into the browser. Shipping the whole
    # catalog would put every page's copy on every page for no benefit.
    js_strings = {k: i18n.t(lang, k) for k in i18n.catalog(lang) if k.startswith("js.")}
    if lang != i18n.DEFAULT:
        for k in i18n.catalog(i18n.DEFAULT):
            if k.startswith("js.") and k not in js_strings:
                js_strings[k] = i18n.t(lang, k)
    return {
        "lang": lang,
        "t": lambda key, **kw: i18n.t(lang, key, **kw),
        "js_i18n": js_strings,
        "other_lang": i18n.other(lang),
        "lang_switch_url": _switch_url(request, lang=i18n.other(lang)),
        "other_lang_label": "EN" if lang == "zh" else "中文",
    }


def _stars_ctx() -> dict:
    """Star badge inputs. Absent until the repo is public — see github_stars."""
    try:
        from . import github_stars

        n = github_stars.stars()
    except Exception:  # noqa: BLE001 — the badge must never break a page
        n = None
    return {
        "github_stars": n,
        "github_stars_text": None
        if n is None
        else __import__("app.github_stars", fromlist=["x"]).format_count(n),
        "github_repo_url": "https://github.com/AgentsDanceAI/deepseek-harness-cloud",
    }


def _team_terms_ctx(cur: str | None = None) -> dict:
    """Seat terms for templates — read from the price table the visitor is being
    quoted in, so the displayed price is the one their order actually charges."""
    try:
        from .payments import base as _pay

        t = _pay.team_terms(cur)
    except Exception:
        t = {}
    return {
        "team_seat_price": int(t.get("seat_cents", config.TEAM_SEAT_PRICE)),
        "team_seat_credits": int(t.get("seat_credits", config.TEAM_SEAT_CREDITS)),
        "team_seat_minutes": int(t.get("seat_minutes", config.TEAM_SEAT_MINUTES)),
        "team_seat_min": int(t.get("min_seats", config.TEAM_SEAT_MIN)),
    }


def _render(request: Request, template: str, page: str, **extra):
    from . import i18n

    response = templates.TemplateResponse(request, template, _ctx(request, page, **extra))
    lang, explicit = i18n.resolve(request)
    from . import currency as _cur

    cur, cur_explicit = _cur.resolve(request)
    if cur_explicit:
        response.set_cookie(
            _cur.COOKIE,
            cur,
            max_age=_cur.COOKIE_MAX_AGE,
            path="/",
            samesite="lax",
            secure=config.PUBLIC_BASE.startswith("https://"),
        )
    if explicit:
        # Persist the click. Without this the switcher works for exactly one
        # page view and every link after it snaps back to the browser locale.
        response.set_cookie(
            i18n.COOKIE,
            lang,
            max_age=i18n.COOKIE_MAX_AGE,
            path="/",
            samesite="lax",
            secure=config.PUBLIC_BASE.startswith("https://"),
        )
    return response


def _pricing_safe(cur: str | None = None) -> dict:
    """The price table to DISPLAY. Charging still resolves its own table from
    the order's currency — a page showing EUR must never decide what a card
    gets debited."""
    try:
        return plans.pricing(cur)
    except Exception:
        return {"currency": cur or "USD", "tiers": {}, "packs": {}}


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _fmt_date(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


# --- minimal markdown -> HTML (headings/paragraphs/lists/bold/links/tables) --

_H_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_SEP_CELL_RE = re.compile(r"^:?-{3,}:?$")
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:https?://|mailto:|/)[^)\s]*)\)")


def _inline(text: str) -> str:
    text = html.escape(text)
    text = _CODE_RE.sub(r"<code>\1</code>", text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _LINK_RE.sub(r'<a href="\2" rel="noopener">\1</a>', text)
    return text


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_to_html(md: str) -> str:
    """Tiny, dependency-free converter for the legal documents. Escapes all
    input first; supports headings, paragraphs, ul/ol lists, bold, inline
    code, links and pipe tables. Anything else degrades to plain paragraphs."""
    out: list[str] = []
    para: list[str] = []
    list_tag: str | None = None
    table: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def flush_table() -> None:
        if not table:
            return
        rows = [_split_row(r) for r in table]
        has_header = len(rows) >= 2 and all(_SEP_CELL_RE.match(c) for c in rows[1] if c)
        out.append('<div class="table-wrap"><table>')
        body = rows
        if has_header:
            out.append("<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in rows[0]) + "</tr></thead>")
            body = rows[2:]
        out.append("<tbody>")
        for r in body:
            out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
        out.append("</tbody></table></div>")
        table.clear()

    for raw in md.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("|") or (table and "|" in stripped and stripped):
            flush_para()
            close_list()
            table.append(stripped)
            continue
        flush_table()

        if not stripped:
            flush_para()
            close_list()
            continue

        m = _H_RE.match(stripped)
        if m:
            flush_para()
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue

        if _HR_RE.match(stripped):
            flush_para()
            close_list()
            out.append("<hr>")
            continue

        m = _UL_RE.match(line)
        if m:
            flush_para()
            if list_tag != "ul":
                close_list()
                out.append("<ul>")
                list_tag = "ul"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        m = _OL_RE.match(line)
        if m:
            flush_para()
            if list_tag != "ol":
                close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        para.append(stripped)

    flush_para()
    close_list()
    flush_table()
    return "\n".join(out)


# --- landing -----------------------------------------------------------------


def _apps_ctx(user: dict | None = None) -> dict:
    """云空间目录 + 实时上线状态 + 链接语义。主页与 /apps 共用 —— 两处各算一份
    必然漂, 漂的结果是主页能点而 /apps 不能 (或反过来)。

    clickable 与 base 分开给: 本站前缀是空串, 在 Jinja 里是假值, 合成一个变量
    会把上线的卡误判成不可点 (踩过)。
    """
    from . import apps_catalog, products, work_access

    enabled = {p.id for p in products.enabled()} | apps_catalog.site_apps()
    # 没登录的人看到的是"要买"的样子 —— 那正是实情, 而且比登录后才发现要买诚实。
    # 查证失败当作没买 (work_access.pass_active 自己兜底), 宁可多问一次也不白送。
    # 免墙的人 (管理员、拿了通配证的) 这里也不该看到锁: 卡上挂着锁却点得进去,
    # 比拦住还让人糊涂。
    locked = {pid for pid in products.locked_ids() if not (user and work_access.can_open_locked(user, pid))}
    apps = apps_catalog.entries_with_status(enabled, work_access.minutes_by_product(), locked)
    if config.WORK_ENABLED:
        target = ""
    else:
        hosted = config.HOSTED_SITE if config.HOSTED_SITE not in ("", config.PUBLIC_BASE.rstrip("/")) else ""
        target = hosted.rstrip("/") if hosted else None
    return {
        "apps": apps,
        # h1 上那个数字**必须**由货架算出来。写死的话, 加一个产品或下架一个,
        # 首页最大那行字就当场变成假话, 而没有任何测试会红。
        "app_count": len(apps),
        "live_count": sum(1 for a in apps if a["live"]),
        "apps_clickable": target is not None,
        "apps_base": target or "",
    }


@router.get("/")
def landing(request: Request):
    pricing = _pricing_safe()
    return _render(request, "index.html", "landing", pricing=pricing, **_apps_ctx(try_resolve_user(request)))


# --- auth pages --------------------------------------------------------------


@router.get("/login")
def login_page(request: Request, next: str = "/console"):
    safe_next = safe_local_path(next, "/console")
    if safe_next != next:
        from urllib.parse import urlencode

        return RedirectResponse("/login?" + urlencode({"next": safe_next}), status_code=303)
    # 开发模式且没配 SMTP 时, 验证码只打到服务端日志 —— 页面必须说出来,
    # 否则自部署用户在登录页上永远等不到那封邮件 (首次运行必踩)。
    dev_mail_to_logs = bool(config.DEV_MODE and not config.MAIL_SMTP_HOST)
    return _render(request, "login.html", "login", dev_mail_to_logs=dev_mail_to_logs)


@router.get("/activate")
def activate_page(request: Request, code: str = ""):
    return _render(request, "activate.html", "activate", code=code.strip().upper())


# --- marketing sections (the nav's Product / Solutions / Resources) ----------


@router.get("/product")
def product_page(request: Request):
    return _render(request, "product.html", "product")


@router.get("/avatar")
def avatar_page(request: Request):
    """数字人通话页。

    与其它产品不同, 它**不是一个云工作台** —— 没有每用户容器可开, 页面就在主站
    这里, 通话经 /api/avatar/* 转发到我们自己的 GPU 节点。所以它不走
    /work/... 那条路, 也不出现在工作台的启动/回收逻辑里。

    未登录先送去登录: 页面上**每一个**动作都要账号 (形象清单、背景图、通话本身
    全挂 resolve_user)。让人先看到界面再一路 401, 只会像是坏了。
    """
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse("/login?next=/avatar", status_code=303)
    # 数字人也可以上锁 (老板 2026-09-06 锁了它和 Coze)。它不是工作台, 所以
    # /api/work/route 那道闸够不着它 —— 页面这里自己拦一道。
    from . import products as _products
    from . import work_access as _wa

    if _products.is_locked("avatar") and not _wa.can_open_locked(user, "avatar"):
        return RedirectResponse("/pricing?reason=locked&product_id=avatar#unlock", status_code=303)
    return _render(request, "avatar.html", "avatar")


def _live_gate(request: Request):
    """进直播页前的两道闸。过了返回 None, 没过返回要跳去哪。

    先登录再进: 与其余十五格一致, 而且公屏发言本来就要账号。
    """
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    from . import products as _products
    from . import work_access as _wa

    if _products.is_locked("live") and not _wa.can_open_locked(user, "live"):
        return RedirectResponse("/pricing?reason=locked&product_id=live#unlock", status_code=303)
    return None


@router.get("/live")
def live_rooms_page(request: Request):
    """直播间列表。

    ⚠️ 这里**不取每间的状态** —— 那要挨个问 GPU 节点, 而页面渲染不该等在上游身上
    (它是别人的共享机, 够不着是常态)。状态由前端拿 /api/live/rooms 填, 那条路带
    两秒缓存, 一百个观众也只打上游一次。
    """
    from . import live as _live

    names = _live.rooms()
    # 只有一间时不要列表页 —— 一张卡片的"列表"是纯粹的多一次点击。
    # 这也是回滚成单间的方式: LIVE_ROOMS 只留一个 id, 这一页就消失, 站点行为
    # 与做多间之前一模一样。多间要回来, 改一个环境变量。
    if len(names) == 1:
        return RedirectResponse(f"/live/{names[0]}", status_code=303)
    gate = _live_gate(request)
    if gate is not None:
        return gate
    return _render(request, "live_rooms.html", "live", rooms=names)


@router.get("/live/console")
def live_console_page(request: Request):
    """直播间控制台 —— **管理员专用** (老板 2026-09-08 定)。

    多间之后这里是"选一间来改" —— 房间清单由 LIVE_ROOMS 定, 每间的名称/形象/
    音色/话术都存在上游, 由这一页配。
    接口那边也各自拦了一道; 这里提前拦是不想让非管理员看到一个自己用不了的壳。
    """
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse("/login?next=/live/console", status_code=303)
    if not user.get("is_admin"):
        return RedirectResponse("/live", status_code=303)
    from . import live as _live

    names = _live.rooms()
    room = request.query_params.get("room") or ""
    if room not in names:
        room = names[0]
    return _render(request, "live_console.html", "live", rooms=names, room=room)


@router.get("/live/{room}")
def live_page(request: Request, room: str):
    """某一间的播放页。

    与数字人通话一样**不是云工作台** —— 没有每用户容器, 画面来自我们自己的 GPU
    节点。**而且它烧 GPU**: 每一句都是当场生成的, 一路实时流占掉那张卡串行吞吐的
    约三分之一。(这段以前写着"离线预渲染、不占并发槽位", 那是第一版, 早就反了。)
    """
    # ⚠️ 这条**必须**注册在 /live/console 之后 —— 路由按注册顺序匹配, 排在前面
    # 就会把 console 当成一个叫 "console" 的房间吃掉 (然后跳回 /live, 而控制台
    # 从此打不开, 且没有任何报错)。
    from . import live as _live

    if room not in _live.rooms():
        return RedirectResponse("/live", status_code=303)
    gate = _live_gate(request)
    if gate is not None:
        return gate
    return _render(request, "live.html", "live", room=room, rooms_count=len(_live.rooms()))


@router.get("/apps")
def apps_page(request: Request):
    """云空间: 16 个开源 AI 产品的 4x4 卡片网格。

    哪些能点进工作台由 products.enabled() 实时判定 —— 目录 (apps_catalog) 只是
    愿景清单, registry 才是事实。本实例没开云工作台时 (自部署默认), 卡片指向
    官方托管版; 连托管地址都没配就只作陈列, 不放会 404 的按钮。
    """
    return _render(request, "apps.html", "apps", **_apps_ctx(try_resolve_user(request)))


@router.get("/solutions")
def solutions_page(request: Request):
    return _render(request, "solutions.html", "solutions")


@router.get("/resources")
def resources_page(request: Request):
    return _render(request, "resources.html", "resources")


@router.get("/console/admin")
def admin_page(request: Request):
    """Operator console. The APIs already enforce admin; this refuses early so a
    non-admin never sees the shell of a page they cannot use."""
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse("/login?next=/console/admin", status_code=303)
    if not user.get("is_admin"):
        return RedirectResponse("/console", status_code=303)
    return _render(request, "admin.html", "admin")


@router.get("/console/team")
def team_page(request: Request):
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse("/login?next=/console/team", status_code=303)
    return _render(
        request,
        "team.html",
        "team",
        team_seat_price=config.TEAM_SEAT_PRICE,
        team_seat_credits=config.TEAM_SEAT_CREDITS,
    )


@router.get("/team/join")
def team_join_page(request: Request):
    """Invite links land here; sign-in first, then the code is applied."""
    code = request.query_params.get("code", "")
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse(f"/login?next=/team/join%3Fcode%3D{code}", status_code=303)
    return _render(
        request,
        "team.html",
        "team",
        team_seat_price=config.TEAM_SEAT_PRICE,
        team_seat_credits=config.TEAM_SEAT_CREDITS,
        auto_join_code=code,
    )


# --- cloud workspace paywall --------------------------------------------------


@router.get("/console")
def console_page(request: Request):
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse("/login?next=/console", status_code=303)
    uid = user["id"]
    balance = credits.balance(uid)
    plan = plans.current_plan(uid)
    lt = time.localtime()
    month_start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    usage = credits.usage_since(uid, month_start)
    recent = []
    for row in credits.recent_usage(uid, 20):
        r = dict(row)
        r["created_str"] = _fmt_ts(r.get("created"))
        recent.append(r)
    from . import teams, work_access

    wa = work_access.state(uid)
    org = teams.org_of(uid)
    return _render(
        request,
        "console.html",
        "console",
        balance=balance,
        balance_yuan=f"{balance / 100:.2f}",
        work_used=wa["used_minutes"],
        work_included=wa["included_minutes"],
        work_packs=wa["pack_minutes"],
        work_left=wa["minutes_left"],
        work_scope=wa["scope"],
        org=org,
        org_pool=teams.pool_balance(org["id"]) if org else 0,
        plan=plan,
        plan_expires_str=_fmt_date(plan.get("expires")) if plan.get("tier") != "free" else "",
        usage=usage,
        recent=recent,
    )


# --- pricing / orders --------------------------------------------------------

# Every advertised installer is reached through here rather than linked
# directly. Two reasons: the download count becomes a real number instead of a
# decoration (nothing was incrementing kv.downloads_total, so the homepage
# proudly displayed the base constant), and the actual bytes can move to GitHub
# Releases or object storage by editing one env var — no frontend change, and
# the counter keeps working across the move.
DOWNLOAD_TARGETS = {
    "mac-arm64": ("DOWNLOAD_URL_MAC", "Apple 芯片"),
    "mac-x64": ("DOWNLOAD_URL_MAC_X64", "Intel 芯片"),
    "win-x64": ("DOWNLOAD_URL_WIN", "Windows x64"),
    "win-arm64": ("DOWNLOAD_URL_WIN_ARM", "Windows ARM"),
    "android": ("DOWNLOAD_URL_ANDROID", "Android"),
}


def download_url(key: str) -> str:
    env, _ = DOWNLOAD_TARGETS.get(key, ("", ""))
    return os.environ.get(env, "").strip() if env else ""


@router.get("/dl/{key}")
def download_redirect(key: str):
    """Count the download, then hand off to wherever the file actually lives."""
    url = download_url(key)
    if not url:
        return JSONResponse(status_code=404, content={"detail": "not_available"})
    from . import db

    with db.tx() as conn:
        # UPSERT keeps this one statement on both backends; a read-modify-write
        # would lose counts whenever two people download at the same moment.
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT (k) DO UPDATE SET v = CAST(CAST(kv.v AS INTEGER) + 1 AS TEXT)",
            (f"dl_{key}", "1"),
        )
        conn.execute(
            "INSERT INTO kv (k, v) VALUES ('downloads_total', '1') "
            "ON CONFLICT (k) DO UPDATE SET v = CAST(CAST(kv.v AS INTEGER) + 1 AS TEXT)"
        )
    return RedirectResponse(url, status_code=302)


@router.get("/api/public/stats")
def public_stats():
    """Numbers the homepage shows. Real counts, not decoration: downloads are
    served from our own /releases so they can be counted honestly, and logins
    are device authorisations plus browser sign-ins."""
    from . import db

    def _n(sql, params=()):
        row = db.query_one(sql, params)
        return int((row["n"] if row is not None else 0) or 0)

    return {
        "downloads": _n("SELECT COALESCE(v,'0') AS n FROM kv WHERE k='downloads_total'")
        + config.DOWNLOAD_COUNT_BASE,
        "logins": _n("SELECT COUNT(*) AS n FROM devices")
        + _n("SELECT COUNT(*) AS n FROM users WHERE last_login>0"),
        "users": _n("SELECT COUNT(*) AS n FROM users"),
        "models": len(__import__("app.model_catalog", fromlist=["x"]).catalog()),
    }


@router.get("/api/models")
def models_public():
    """Public model catalog with credit multipliers — the pricing page reads it
    so the advertised rate and the billed rate can never disagree."""
    from . import model_catalog

    return {
        "baseline": model_catalog.meta().get("baseline_model"),
        "credits_per_baseline_m": model_catalog.meta().get("credits_per_baseline_m"),
        "models": model_catalog.public_catalog(),
    }


@router.get("/pricing")
def pricing_page(request: Request):
    from . import currency as _cur

    cur, _ = _cur.resolve(request)
    pricing = _pricing_safe(cur)
    tier_order = [t for t in ("free", "plus", "pro", "max") if t in pricing.get("tiers", {})]
    # Arriving from a gate (out of credits, out of machine hours) without being
    # told why makes the price list look like an ad. `reason` was already being
    # passed on those redirects and had never been rendered.
    reason = request.query_params.get("reason")
    # 从上锁的格子跳过来时, 顶部给一条"买这一格"的横幅 —— 让人从"为什么进不去"
    # 直接走到"多少钱、买"。价来自价目表 (与结账用的是同一处, 不会两处各写一遍)。
    unlock = None
    if reason == "locked":
        from . import apps_catalog as _catalog
        from . import products as _products

        pid = request.query_params.get("product_id") or ""
        pdef = (pricing.get("passes") or {}).get(pid)
        name = _catalog.name_of(pid)
        if pdef and name and _products.is_locked(pid):
            unlock = {"id": pid, "name": name, "cents": int(pdef["cents"]), "days": int(pdef["days"])}
    return _render(
        request,
        "pricing.html",
        "pricing",
        pricing=pricing,
        tier_order=tier_order,
        reason=reason if reason in ("work", "credits", "locked") else None,
        unlock=unlock,
    )


@router.get("/orders")
def orders_page(request: Request):
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse("/login?next=/orders", status_code=303)
    return _render(request, "orders.html", "orders")


# --- legal -------------------------------------------------------------------

LEGAL_DOCS = {
    "terms": "服务条款",
    "privacy": "隐私政策",
    "refund": "退款政策",
    "aup": "可接受使用政策",
}


@router.get("/legal/{doc}")
def legal_page(request: Request, doc: str):
    if doc not in LEGAL_DOCS:
        return RedirectResponse("/legal/terms", status_code=303)
    title = LEGAL_DOCS[doc]
    # Whole-document translation: legal text is not assembled from phrases, and
    # a half-translated clause is a liability. Falls back to Chinese so a
    # not-yet-translated policy still renders its real text.
    from . import i18n

    lang, _ = i18n.resolve(request)
    path = _legal_dir() / f"{doc}.{lang}.md"
    if not path.is_file():
        path = _legal_dir() / f"{doc}.{i18n.DEFAULT}.md"
    body_html = ""
    pending = True
    try:
        if path.is_file():
            body_html = markdown_to_html(path.read_text(encoding="utf-8"))
            pending = False
    except Exception:
        body_html, pending = "", True
    return _render(
        request, "legal.html", f"legal-{doc}", doc=doc, doc_title=title, body_html=body_html, pending=pending
    )


@router.get("/privacy")
def privacy_redirect():
    return RedirectResponse("/legal/privacy", status_code=308)


@router.get("/terms")
def terms_redirect():
    return RedirectResponse("/legal/terms", status_code=308)


# --- download ----------------------------------------------------------------


@router.get("/logout")
def logout_alias():
    """Top-level sign-out.

    The account menu linked here while the handler lived at /api/auth/logout, so
    the link 404'd — and a 404 on sign-out is the same trap as a broken logout
    button: the person cannot get out. Kept as its own route rather than fixing
    the one link, because /logout is what people type and what any future
    template will reach for.
    """
    from .accounts import clear_session_cookie

    response = RedirectResponse(config.PUBLIC_BASE.rstrip("/") + "/", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/download")
def download_page(request: Request):
    # Availability comes from the shared context (has_* flags); the page links
    # to /dl/<key> so downloads from here are counted like any other.
    return _render(request, "download.html", "download")
