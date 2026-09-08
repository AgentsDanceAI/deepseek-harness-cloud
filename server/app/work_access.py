"""Cloud-workspace machine time — a resource of its own, not credits.

Two different things cost us money, and mixing them into one number made the
bill unreadable:

  * **tokens** (model + search) — elastic, priced per call → credits;
  * **machine time** — a container reserving RAM and CPU → minutes.

So minutes are metered like GitHub Actions: every plan includes an allowance per
billing period, and running out means "upgrade or buy more time", never "your
credits quietly drained". Credits are never charged for a workspace minute.

每一分钟“容器存在”都计入机时，而不是只统计智能体调用模型的时间
（见 workspace.reaper_tick）。这使计量口径与实际占用的计算资源一致；空闲回收
同时限制无活动容器持续占用容量。

Organisations pool minutes the same way they pool credits: seats contribute to
one balance, drawn by whoever works, bounded per member so a single person
cannot spend the team's month.
"""

from __future__ import annotations

import logging
import threading
import time

from . import config, db, plans, security

log = logging.getLogger("dhc.work")

# usage_log rows written by the reaper carry this kind; one row == one minute.
MINUTE_KIND = "workspace"


# --- billing period ----------------------------------------------------------


def period_start(user_id: str) -> float:
    """Start of the current allowance window.

    A paid plan's window follows its own renewal date, so someone who subscribes
    on the 20th is not handed a fresh allowance on the 1st. Free users ride the
    calendar month.
    """
    plan = plans.current_plan(user_id)
    expires = float(plan.get("expires") or 0)
    if plan.get("tier") != "free" and expires > 0:
        days = 366 if plan.get("cycle") == "yearly" else 31
        return expires - days * 86400
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))


# --- allowance ---------------------------------------------------------------


def included_minutes(user_id: str) -> int:
    """Minutes this user's plan includes per period (org seats add to this)."""
    plan = plans.current_plan(user_id)
    base = int(plan.get("work_minutes") or 0)
    if base == 0 and plan.get("tier") == "free":
        base = config.WORK_FREE_MINUTES
    return base


def used_minutes(user_id: str, since: float | None = None) -> int:
    """本期已用的机时**份数** —— 每分钟一行, 但一行折几份看格子多大。

    2026-09-06 之前一行就是一份, 与格子多大无关 (那是 0.5 核 1G 时代的口径)。现在
    按 products.minute_units 折算, 写在行上的 units 里。**老行没有这一列, 一律当
    1 份** —— 加权从上线那一刻起生效, 不追溯改写谁的历史用量 (追溯的话有人会在毫无
    动作的情况下突然超额)。
    """
    since = period_start(user_id) if since is None else since
    row = db.query_one(
        "SELECT COALESCE(SUM(CASE WHEN units IS NULL OR units < 1 THEN 1 ELSE units END), 0) AS n "
        "FROM usage_log WHERE user_id=? AND kind=? AND created>?",
        (user_id, MINUTE_KIND, since),
    )
    return int((row["n"] if row is not None else 0) or 0)


_POPULARITY_TTL_S = 300.0
_popularity: tuple[float, dict[str, int]] = (0.0, {})
_popularity_lock = threading.Lock()


def minutes_by_product(days: int = 30) -> dict[str, int]:
    """全站每个产品最近 N 天累计开了多少分钟 (不折算 —— "时长"就是时长)。

    云空间那页每次访问都要用它排序, 所以缓存 5 分钟: 这是个全表扫的分组查询, 而
    排序结果慢五分钟没有任何人看得出来。**缓存失败要返回空字典, 不能抛** —— 排序
    是锦上添花, 数据库抖一下不该让整页打不开。
    """
    now = time.time()
    with _popularity_lock:
        at, cached = _popularity
        if cached and now - at < _POPULARITY_TTL_S:
            return cached
    out: dict[str, int] = {}
    try:
        rows = db.query(
            "SELECT model, COUNT(*) AS n FROM usage_log WHERE kind=? AND created>? GROUP BY model",
            (MINUTE_KIND, now - max(1, days) * 86400),
        )
        for r in rows:
            model = r["model"] or ""
            pid = model[len("work:") :] if model.startswith("work:") else ""
            if pid:
                out[pid] = out.get(pid, 0) + int(r["n"] or 0)
    except Exception:  # noqa: BLE001
        log.warning("按产品统计时长失败, 这一轮不排序", exc_info=True)
        return {}
    with _popularity_lock:
        globals()["_popularity"] = (now, out)
    return out


def wall_clock_minutes(user_id: str, since: float | None = None) -> int:
    """本期实际开着的**分钟数** (不折算)。给报表用 —— 额度看份数, 而"开了多久"看这个。"""
    since = period_start(user_id) if since is None else since
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM usage_log WHERE user_id=? AND kind=? AND created>?",
        (user_id, MINUTE_KIND, since),
    )
    return int((row["n"] if row is not None else 0) or 0)


def minute_packs_left(user_id: str) -> int:
    """Unexpired purchased minutes (top-ups outlive the plan period)."""
    row = db.query_one(
        "SELECT COALESCE(SUM(remaining),0) AS n FROM minute_grants WHERE user_id=? AND expires>?",
        (user_id, time.time()),
    )
    return int((row["n"] if row is not None else 0) or 0)


# 通行证的 kind 前缀: work_passes.kind 存的是 "pass:<产品 id>"。
PASS_KIND = "pass"
#: 通配产品 id: 一张 "pass:*" 免掉**整面**试用墙, 不是某一格。管理员在后台发,
#: 走的是和买来的证完全同一条路 —— 于是拦截点一处都不用多判一个分支, 到期、
#: 撤销、审计也都沿用现成的那套。
PASS_ANY = "*"


def pass_active(user_id: str, product_id: str) -> bool:
    """这个人现在有没有这一格的有效通行证 (通配证也算)。

    **认不出来就当没有** —— 数据库抖一下应该表现为"要买", 不是"白送"。
    """
    try:
        row = db.query_one(
            "SELECT 1 AS ok FROM work_passes WHERE user_id=? AND kind IN (?,?) AND expires>? LIMIT 1",
            (user_id, f"{PASS_KIND}:{product_id}", f"{PASS_KIND}:{PASS_ANY}", time.time()),
        )
    except Exception:  # noqa: BLE001
        log.warning("查通行证失败 (%s / %s)", user_id, product_id, exc_info=True)
        return False
    return row is not None


def can_open_locked(user: dict, product_id: str) -> bool:
    """上锁的格子, 这个人现在能不能开。**所有拦截点都该问这一个函数。**

    管理员天然免: 服务是他们在运营, 把自己挡在门外没有意义 —— 出事的时候第一
    个要能进去看的就是他们。这里读的是 accounts 已经算好的 is_admin (role 或
    ADMIN_EMAILS), 不再查库, 因为这条路每个静态资源都会走一遍。

    其余人靠通行证: 自己买的单格证, 或管理员发的通配证 (见 PASS_ANY)。
    """
    if user.get("is_admin"):
        return True
    return pass_active(user["id"], product_id)


def lock_exempt(user_id: str) -> bool:
    """有没有"免整面墙"的通配证。给后台展示按钮状态用。"""
    return pass_active(user_id, PASS_ANY)


def revoke_lock_exemption(user_id: str) -> int:
    """撤掉通配证。返回撤了几张。

    直接删而不是把 expires 改成过去: 这张证没有收过钱 (price=0, 管理员发的),
    留着只会让对账时多一行看不懂的记录。买来的单格证不受影响。
    """
    rows = db.query(
        "SELECT id FROM work_passes WHERE user_id=? AND kind=?",
        (user_id, f"{PASS_KIND}:{PASS_ANY}"),
    )
    if not rows:
        return 0
    with db.tx() as conn:
        conn.execute(
            "DELETE FROM work_passes WHERE user_id=? AND kind=?",
            (user_id, f"{PASS_KIND}:{PASS_ANY}"),
        )
    log.info("[work] 撤销通配通行证 %d 张 (%s)", len(rows), user_id)
    return len(rows)


def pass_expires(user_id: str, product_id: str) -> float:
    row = db.query_one(
        "SELECT MAX(expires) AS e FROM work_passes WHERE user_id=? AND kind=? AND expires>?",
        (user_id, f"{PASS_KIND}:{product_id}", time.time()),
    )
    return float((row["e"] if row is not None else 0) or 0)


def grant_pass(
    user_id: str, product_id: str, days: int, *, price: int = 0, currency: str = "", ref: str = ""
) -> str:
    """发一张通行证。已有未过期的就**从它到期那刻往后续**, 不是从现在起算 ——
    否则连买两张的人白白损失第一张的剩余时间。"""
    if days <= 0:
        raise ValueError("days must be positive")
    now = time.time()
    base = max(now, pass_expires(user_id, product_id))
    pid = security.new_id("wpass_")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO work_passes (id,user_id,kind,started,expires,price,currency,ref,created) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, user_id, f"{PASS_KIND}:{product_id}", now, base + days * 86400, price, currency, ref, now),
        )
    log.info(
        "[work] 发通行证 %s 给 %s (%s, %d 天, 到 %.0f)", pid, user_id, product_id, days, base + days * 86400
    )
    return pid


def grant_minutes(user_id: str, minutes: int, ttl_s: float, kind: str, ref: str = "") -> str:
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    gid = security.new_id("mgrant_")
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO minute_grants (id, user_id, amount, remaining, expires, kind, ref, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (gid, user_id, minutes, minutes, now + ttl_s, kind, ref, now),
        )
    return gid


def consume_minute(user_id: str, units: int = 1) -> None:
    """扣 `units` 份机时: 先吃套餐额度, 再吃买来的机时券。

    在这一分钟**已经服务完**之后调用 —— 我们从不打断正在进行的活儿; 下一件事能不能
    开由后面的闸决定。

    一份不够扣时要**跨券排干** (一张券只剩 3 份而这一分钟要 8 份, 得接着扣下一张)。
    原先一次只扣一张券的一份, 加权之后那样会少扣。
    """
    if used_minutes(user_id) <= included_minutes(user_id):
        return  # 还在套餐额度里, 不动券
    left = max(1, int(units))
    with db.tx() as conn:
        while left > 0:
            row = conn.execute(
                "SELECT id, remaining FROM minute_grants WHERE user_id=? AND expires>? AND remaining>0 "
                "ORDER BY expires ASC LIMIT 1",
                (user_id, time.time()),
            ).fetchone()
            if row is None:
                return  # 券也用完了 —— 闸会拦住下一件事, 这一分钟照样如实记账
            take = min(left, int(row["remaining"]))
            conn.execute("UPDATE minute_grants SET remaining=remaining-? WHERE id=?", (take, row["id"]))
            left -= take


def state(user_id: str) -> dict:
    """Everything the UI needs to show the meter and decide go / paywall.

    A member of an organisation gets the org pool ON TOP of their own plan
    allowance, never instead of it — joining a team must not leave someone worse
    off than they were alone, which is what happens if the pool is empty and the
    personal allowance is ignored.
    """
    from . import teams

    org = teams.org_of(user_id)
    if org is not None:
        return teams.work_state(org, user_id, personal=_personal_state(user_id))

    return _personal_state(user_id)


def _personal_state(user_id: str) -> dict:
    included = included_minutes(user_id)
    used = used_minutes(user_id)
    packs = minute_packs_left(user_id)
    plan = plans.current_plan(user_id)
    left = max(0, included - used) + packs
    return {
        "scope": "personal",
        "plan_tier": plan.get("tier", "free"),
        "plan_name": plan.get("name", "Free"),
        "included_minutes": included,
        "used_minutes": used,
        "pack_minutes": packs,
        "minutes_left": left,
        "period_start": period_start(user_id),
        # Machine time is the single access meter for workspaces.
        "allowed": left > 0,
        # kept for older callers/templates that still read the free-hours wording
        "free_minutes_total": included,
        "free_minutes_left": left,
    }


def blocked_reason(user_id: str) -> str | None:
    """None when a new workspace task may start, else a machine-readable reason."""
    st = state(user_id)
    if st["allowed"]:
        return None
    return st.get("blocked_reason") or "work_quota"
