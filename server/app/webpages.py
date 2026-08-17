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
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, credits, plans
from .accounts import try_resolve_user

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
    from . import plans as _plans
    try:
        currency = _plans.pricing().get("currency", "CNY")
    except Exception:
        currency = "CNY"
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
        "currency": currency,
        "currency_symbol": {"CNY": "¥", "USD": "$"}.get(currency, currency + " "),
        "download_url_mac": os.environ.get("DOWNLOAD_URL_MAC", ""),
        "download_url_win": os.environ.get("DOWNLOAD_URL_WIN", ""),
        "download_url_mac_x64": os.environ.get("DOWNLOAD_URL_MAC_X64", ""),
        "download_url_android": os.environ.get("DOWNLOAD_URL_ANDROID", ""),
        "download_url_win_arm": os.environ.get("DOWNLOAD_URL_WIN_ARM", ""),
        "work_enabled": config.WORK_ENABLED,
        "work_credits_per_min": config.WORK_CREDITS_PER_MIN,
        "work_idle_stop_min": config.WORK_IDLE_STOP_MIN,
        "work_free_minutes": config.WORK_FREE_MINUTES,
        "work_pass_days": config.WORK_PASS_DAYS,
        "work_pass_intro_price": config.WORK_PASS_INTRO_PRICE,
        "work_pass_price": config.WORK_PASS_PRICE,
        **_team_terms_ctx(),
    }
    ctx.update(extra)
    return ctx


def _team_terms_ctx() -> dict:
    """Seat terms for templates — read from the active price table so the
    displayed price is the one an order would actually charge."""
    try:
        from .payments import base as _pay
        t = _pay.team_terms()
    except Exception:
        t = {}
    return {
        "team_seat_price": int(t.get("seat_cents", config.TEAM_SEAT_PRICE)),
        "team_seat_credits": int(t.get("seat_credits", config.TEAM_SEAT_CREDITS)),
        "team_seat_minutes": int(t.get("seat_minutes", config.TEAM_SEAT_MINUTES)),
        "team_seat_min": int(t.get("min_seats", config.TEAM_SEAT_MIN)),
    }


def _render(request: Request, template: str, page: str, **extra):
    return templates.TemplateResponse(request, template, _ctx(request, page, **extra))


def _pricing_safe() -> dict:
    try:
        return plans.pricing()
    except Exception:
        return {"currency": "CNY", "tiers": {}, "packs": {}}


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

@router.get("/")
def landing(request: Request):
    pricing = _pricing_safe()
    return _render(request, "index.html", "landing", pricing=pricing)


# --- auth pages --------------------------------------------------------------

@router.get("/login")
def login_page(request: Request, next: str = "/console"):
    return _render(request, "login.html", "login")


@router.get("/activate")
def activate_page(request: Request, code: str = ""):
    return _render(request, "activate.html", "activate", code=code.strip().upper())


# --- marketing sections (the nav's Product / Solutions / Resources) ----------

@router.get("/product")
def product_page(request: Request):
    return _render(request, "product.html", "product")


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
    return _render(request, "team.html", "team",
                   team_seat_price=config.TEAM_SEAT_PRICE,
                   team_seat_credits=config.TEAM_SEAT_CREDITS)


@router.get("/team/join")
def team_join_page(request: Request):
    """Invite links land here; sign-in first, then the code is applied."""
    code = request.query_params.get("code", "")
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse(f"/login?next=/team/join%3Fcode%3D{code}", status_code=303)
    return _render(request, "team.html", "team",
                   team_seat_price=config.TEAM_SEAT_PRICE,
                   team_seat_credits=config.TEAM_SEAT_CREDITS,
                   auto_join_code=code)


# --- cloud workspace paywall --------------------------------------------------

@router.get("/work/upgrade")
def work_upgrade_page(request: Request):
    """Shown when the free machine-time allowance is spent. Deliberately a page
    rather than a modal: it is a purchase decision, and it needs a way back."""
    user = try_resolve_user(request)
    if user is None:
        return RedirectResponse("/login?next=/work/upgrade", status_code=303)
    from . import work_access
    st = work_access.state(user["id"])
    return _render(
        request, "work_upgrade.html", "work_upgrade",
        pass_active=st["pass_active"],
        pass_expires_text=_fmt_date(st["pass_expires"]) if st["pass_expires"] else "",
        next_price=st["next_price"],
        next_price_kind=st["next_price_kind"],
        standard_price=st["standard_price"],
        free_minutes_left=st["free_minutes_left"],
    )


# --- console -----------------------------------------------------------------

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
        request, "console.html", "console",
        balance=balance,
        balance_yuan=f"{balance / 100:.2f}",
        work_used=wa["used_minutes"],
        work_included=wa["included_minutes"],
        work_packs=wa["pack_minutes"],
        work_left=wa["minutes_left"],
        work_scope=wa["scope"],
        work_pass_active=wa["pass_active"],
        work_pass_expires_text=_fmt_date(wa["pass_expires"]) if wa["pass_expires"] else "",
        org=org,
        org_pool=teams.pool_balance(org["id"]) if org else 0,
        plan=plan,
        plan_expires_str=_fmt_date(plan.get("expires")) if plan.get("tier") != "free" else "",
        usage=usage,
        recent=recent,
    )


# --- pricing / orders --------------------------------------------------------

@router.get("/api/models")
def models_public():
    """Public model catalog with credit multipliers — the pricing page reads it
    so the advertised rate and the billed rate can never disagree."""
    from . import model_catalog
    return {"baseline": model_catalog.meta().get("baseline_model"),
            "credits_per_baseline_m": model_catalog.meta().get("credits_per_baseline_m"),
            "models": model_catalog.public_catalog()}


@router.get("/pricing")
def pricing_page(request: Request):
    pricing = _pricing_safe()
    tier_order = [t for t in ("free", "plus", "pro", "max") if t in pricing.get("tiers", {})]
    return _render(request, "pricing.html", "pricing", pricing=pricing, tier_order=tier_order)


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
    path = _legal_dir() / f"{doc}.zh.md"
    body_html = ""
    pending = True
    try:
        if path.is_file():
            body_html = markdown_to_html(path.read_text(encoding="utf-8"))
            pending = False
    except Exception:
        body_html, pending = "", True
    return _render(request, "legal.html", f"legal-{doc}",
                   doc=doc, doc_title=title, body_html=body_html, pending=pending)


@router.get("/privacy")
def privacy_redirect():
    return RedirectResponse("/legal/privacy", status_code=308)


@router.get("/terms")
def terms_redirect():
    return RedirectResponse("/legal/terms", status_code=308)


# --- download ----------------------------------------------------------------

@router.get("/download")
def download_page(request: Request):
    return _render(
        request, "download.html", "download",
        url_mac=os.environ.get("DOWNLOAD_URL_MAC", "").strip(),
        url_win=os.environ.get("DOWNLOAD_URL_WIN", "").strip(),
    )
