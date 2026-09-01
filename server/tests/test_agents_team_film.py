"""Agents Team 剧组线契约 (2026-08-31)。

出发点是机器之心那篇千问创作实测: 多 agent 出片跑得通, 靠的不是"多几个机器人",
而是三条设计:
  ① agent 之间传的是**可读可改可回滚的结构化中间产物** (讲戏本/角色资产/镜头表),
     不是自然语言转述;
  ② 人的判断插在**生成之前** —— 出片是最贵最慢的一步, 十几条跑完才发现方向偏了
     返工成本最高;
  ③ 角色资产先于镜头 —— 十几个镜头的人物一致性全锚在那几张定妆图上。
下面每条都对应其中一环, 且都能红。
"""

from __future__ import annotations

import os
import pathlib
import tempfile

# ⚠️ 环境必须在 import app 之前钉死 —— config 是在 import 期读 env 的 (见
# test_core.py 同款注释)。本文件按字母序**排在最前**, 抢先 import 会把 config
# 冻结成未设值的样子, 后面 test_core / test_concurrency_security 再 import
# app.main 时就去建 /app (macOS 只读) → 整轮收集 13 个 ERROR。
_TMP = tempfile.mkdtemp(prefix="dhc-film-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import products  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEAM = ROOT / "deploy" / "workspace-agents-team"
ROOMS = (TEAM / "app" / "rooms.py").read_text(encoding="utf-8")
MEDIA = (TEAM / "app" / "media.py").read_text(encoding="utf-8")
TOOLS = (TEAM / "app" / "tools.py").read_text(encoding="utf-8")
DOCKERFILE = (TEAM / "Dockerfile").read_text(encoding="utf-8")


def test_media_models_come_from_the_live_catalog_not_hardcoded():
    """型号写死就是"哪天下架每次 404", 而错误只出现在容器里没人看的日志。"""
    env = products.env_for("agents-team", "tok")
    from app import media

    off = media.offered()
    ids = {m["id"] for m in off.get("video", [])}
    if ids:
        assert env["DSH_VIDEO_MODEL"] in ids, "挑出来的型号必须真在在售目录里"
    iids = {m["id"] for m in off.get("image", [])}
    if iids:
        assert env["DSH_IMAGE_MODEL"] in iids
    # 型号名可以作为**偏好子串**出现在源码里 (不中会回落), 但不许被直接赋给
    # env —— 那才是硬依赖: 该型号下架时每次 404, 且只在容器日志里出声。
    src = (ROOT / "server" / "app" / "products.py").read_text(encoding="utf-8")
    for key in ("DSH_VIDEO_MODEL", "DSH_IMAGE_MODEL"):
        for m in ids | iids:
            assert f'{key}"] = "{m}"' not in src, f"{key} 被硬编码成了 {m}"
            assert f'"{key}": "{m}"' not in src, f"{key} 被硬编码成了 {m}"


def test_video_model_prefers_longer_single_shot():
    """目录顺序没有语义 —— 取第一项实测挑到 seedance 而不是万相 3.0。

    单段更长 = 接缝更少, 而换脸/道具漂移/环境音断裂大多发生在接缝处, 是短剧
    一致性的头号来源。所以剧组这条线要按偏好挑, 不是碰运气。
    """
    pick = products._pick_media_model
    catalog = [{"id": "doubao-seedance-2-0-260128"}, {"id": "wan3.0-video"}, {"id": "wan3.0-video-prime"}]
    got = pick(catalog, "K", ("wan3.0-video", "seedance-2-5", "seedance-2-0"))
    assert got == {"K": "wan3.0-video"}, "万相 3.0 在目录里却没被选中"

    # Prime 贵一半, 不能因为名字更长就被优先匹配到
    assert pick([{"id": "wan3.0-video-prime"}, {"id": "wan3.0-video"}], "K", ("wan3.0-video",))["K"] in {
        "wan3.0-video-prime",
        "wan3.0-video",
    }

    # 偏好全不中 → 回落第一项, 不是什么都不设
    assert pick([{"id": "unknown-model"}], "K", ("wan3.0-video",)) == {"K": "unknown-model"}
    # 目录空 → 什么都不设 (调用方据此判定工具不可用)
    assert pick([], "K", ("x",)) == {}
    assert pick(None, "K", ("x",)) == {}


def test_media_catalog_failure_does_not_break_workspace_creation():
    """媒体目录读不出来只该让出图/出片不可用, 不该拖垮整个工作台创建。"""
    seg = (ROOT / "server" / "app" / "products.py").read_text(encoding="utf-8")
    i = seg.index('if product_id == "agents-team"')
    block = seg[i : i + 2600]
    assert "try:" in block and "except Exception" in block
    # 兜底后仍要返回基础 env (对话能力不受媒体影响)
    env = products.env_for("agents-team", "tok")
    assert env["DSH_CLOUD_TOKEN"] == "tok"
    assert env["DSH_GATEWAY_BASE"].endswith("/llm/v1")


def test_media_endpoints_share_the_chat_gateway_prefix():
    """出图/出片端点与对话在同一个 /llm 前缀上 —— 工具因此不需要第二套配置。

    这条钉的是一个**跨文件的事实**: 一旦哪天 media 的 router 前缀改了, 容器里
    拼出来的 URL 会静默 404, 而症状只是"阿画不出图"。
    """
    gw = (ROOT / "server" / "app" / "gateway.py").read_text(encoding="utf-8")
    md = (ROOT / "server" / "app" / "media.py").read_text(encoding="utf-8")
    assert 'APIRouter(prefix="/llm"' in gw
    assert 'APIRouter(prefix="/llm"' in md
    # ⚠️ 这条断言原先写成 `"/v1/videos/generations" in MEDIA`, 而那**正是 bug 本身**:
    # GATEWAY_BASE 已含 /llm/v1, 再拼 /v1/ 就是 /llm/v1/v1/... → 405。
    # 断言写成"存在某个字符串"而不是"URL 拼对了", 于是它把 bug 钉成了规范。
    # 现在钉真语义: 容器侧一律 f"{GATEWAY_BASE}/<路径>", 不许再出现 /v1/。
    assert 'f"{GATEWAY_BASE}/v1/' not in MEDIA, "又拼出了双 /v1"
    for path in (
        "images/generations",
        "videos/generations",
        "videos/result/",
        "media/uploads",
        "media/models",
    ):
        assert 'f"{GATEWAY_BASE}/' + path in MEDIA, f"{path} 的 URL 拼法不对"
    # 与对话端同一种拼法 —— 同一个 base, 不该有两套规矩
    agent_src = (TEAM / "app" / "agent.py").read_text(encoding="utf-8")
    assert 'f"{GATEWAY_BASE}/chat/completions"' in agent_src


def test_crew_roles_exist_with_distinct_jobs():
    for bot_id in ("director", "artist", "storyboard", "videographer", "editor"):
        assert f'"{bot_id}"' in ROOMS, f"缺少剧组工位 {bot_id}"


def test_storyboard_stops_for_human_review_before_expensive_generation():
    """② 人审插在生成之前 —— 这是整条流水线最重要的一条。"""
    i = ROOMS.index('"storyboard"')
    seg = ROOMS[i : i + 900]
    assert "镜头表.json" in seg
    assert "停下来" in seg or "请用户过目" in seg
    # 视频师那边也要有对应的"没确认就别开跑"
    j = ROOMS.index('"videographer"')
    vseg = ROOMS[j : j + 900]
    assert "确认" in vseg


def test_video_tool_description_warns_about_cost_and_gate():
    """模型选工具几乎只看描述 —— 贵和要先审这两件事必须写在描述里。"""
    i = MEDIA.index('"name": "generate_video"')
    seg = MEDIA[i : i + 1200]
    assert "最慢最贵" in seg or "最贵" in seg
    assert "确认" in seg


def test_artifacts_are_files_not_chat_messages():
    """① 交付物落文件 —— 下游 read_file 能读、用户在文件树能看见、单环可重做。"""
    for role, artifact in (
        ("director", "讲戏本.md"),
        ("artist", "资产清单.md"),
        ("storyboard", "镜头表.json"),
    ):
        i = ROOMS.index(f'"{role}"')
        assert artifact in ROOMS[i : i + 900], f"{role} 没有约定文件产物 {artifact}"
    # 生成类工具一律写进工作区, 不把二进制塞回对话
    assert "存进工作区" in MEDIA or "存到工作区" in MEDIA


def test_character_assets_anchor_shot_consistency():
    """③ 角色资产先于镜头 —— 一致性锚点。"""
    i = ROOMS.index('"artist"')
    assert "一致性" in ROOMS[i : i + 900]
    i2 = MEDIA.index('"name": "generate_video"')
    assert "一致性" in MEDIA[i2 : i2 + 1200]


def test_single_shot_redo_does_not_touch_others():
    """成片不是终点: 退回某一镜 @对应 agent 单独重做, 别的片段不动。"""
    i = ROOMS.index('"videographer"')
    assert "只重跑那一条" in ROOMS[i : i + 900]


def test_ffmpeg_is_in_the_image():
    """剪辑师靠它拼片 —— 镜像里没有的话, 报错只会出现在容器日志里。"""
    assert "ffmpeg" in DOCKERFILE


def test_media_tools_registered():
    for name in ("generate_image", "generate_video", "concat_videos", "media_models"):
        assert f'"{name}"' in MEDIA
    assert "media.SCHEMAS" in TOOLS and "media.HANDLERS" in TOOLS


def test_media_module_does_not_import_tools():
    """media 与 tools 互相引用会成环 (tools 合并工具表时 import media)。"""
    assert "from . import tools" not in MEDIA
    assert "AGENTS_TEAM_WORKDIR" in MEDIA, "WORKDIR 要自己从同一个 env 派生"


def test_generated_paths_are_confined_to_workspace():
    """模型偶尔会写 ../ 或绝对路径 —— 生成产物不许落到工作区外面。"""
    assert "_safe_rel" in MEDIA
    i = MEDIA.index("def _safe_rel")
    seg = MEDIA[i : i + 400]
    assert "resolve()" in seg and "路径越界" in seg
    # 三个写文件的工具都要过这道关
    for fn in ("generate_image", "generate_video", "concat_videos"):
        j = MEDIA.index(f"async def {fn}")
        assert "_safe_rel" in MEDIA[j : j + 1500], f"{fn} 没有过路径收敛"


def test_resolution_is_normalised_to_lowercase():
    """自测抓到的真 bug: 目录里分辨率键是**小写**, 服务端不归一化。

    传 "720P" 会让 price_of 查不到价 → 判成"未定价=不售卖"当场被拒, 而症状只是
    "阿摄一出片就失败"。模型很容易照人话写成大写, 所以必须在容器侧兜住。
    """
    assert "_norm_resolution" in MEDIA
    i = MEDIA.index("def _norm_resolution")
    seg = MEDIA[i : i + 300]
    assert ".lower()" in seg
    # 代码里不许再有大写档位 (注释里出现是在解释这个 bug, 不算)
    code = "\n".join(ln for ln in MEDIA.splitlines() if not ln.lstrip().startswith("#"))
    for bad in ('= "720P"', '= "480P"', '= "1080P"', 'or "720P"', "480P/720P/1080P"):
        assert bad not in code, f"代码里还有大写档位: {bad}"
    # 提交体里用的是归一化后的值, 不是原值
    j = MEDIA.index('"resolution":')
    assert "_norm_resolution(resolution)" in MEDIA[j : j + 120]


def test_norm_resolution_behaviour():
    """行为本身也钉一下 —— 光有函数名不代表它做对了。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("film_media", TEAM / "app" / "media.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._norm_resolution("720P") == "720p"
    assert mod._norm_resolution("1080P") == "1080p"
    assert mod._norm_resolution("  480p ") == "480p"
    # 认不出来的一律回落到 720p, 不许把垃圾透传给上游
    assert mod._norm_resolution("4K") == "720p"
    assert mod._norm_resolution("") == "720p"


# ── 一句话出片: 自动组队 + 接力 + 真闸门 (2026-08-31 老板: "我就想一句话,
# 让他们协同完成一个 3 分钟的视频短剧") ──────────────────────────────────
#
# 原先只有并行模式 —— 五个工位对着同一句"做个短剧"各说各话, 谁也看不见谁这一轮
# 的产出: 美术读不到导演刚写的讲戏本, 分镜读不到美术刚出的资产清单。流水线在
# 并行模式下根本不成立。而人审闸只是写在人格里的一句话, 模型想跳过就跳过。
MAIN = (TEAM / "app" / "main.py").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    """取一个顶层函数的完整正文。

    别用固定字符数切片: 函数一长断言就假红 (2026-08-31 加续跑时四条集体红,
    全是 _run_relay 从 2200 长到 2553 字符, 不是真回归)。
    """
    i = src.index(name)
    j = src.find("\nasync def ", i + len(name))
    k = src.find("\ndef ", i + len(name))
    ends = [x for x in (j, k) if x > 0]
    return src[i : min(ends)] if ends else src[i:]


def test_crew_room_is_relay_not_parallel():
    """顺序即依赖: 讲戏本 → 角色资产 → 镜头表 →(人审)→ 片段 → 成片。"""
    assert 'CREW = ("director", "artist", "storyboard", "videographer", "editor")' in ROOMS
    assert "def create_crew_room" in ROOMS
    i = ROOMS.index("def create_crew_room")
    assert 'mode="relay"' in ROOMS[i : i + 500]


def test_relay_writes_each_turn_before_passing_the_baton():
    """接力的**全部要害**: 先落记录再传棒, 下一位 render_for 才读得到。"""
    seg = _fn(MAIN, "async def _run_relay")
    add_at = seg.index("store.add(room.id, ev[")
    render_at = seg.index("store.render_for(bot, room.id)")
    # render 在循环体开头, add 在同一轮的 end 事件里 —— 两者都在, 且循环是逐棒的
    assert add_at > render_at
    # 逐棒 = 按成员顺序取下标跑 (续跑要从中间起, 所以是带下标的形式)
    assert "for idx in range(start, len(members))" in seg, "必须按成员顺序逐棒, 不是并发"
    assert "members[idx]" in seg
    assert "asyncio.create_task" not in seg, "接力里不许再起并发任务"


def test_halt_is_a_tool_not_a_prompt_hope():
    """闸门靠工具调用判定 —— 文本标记会漏、会被改写、会混进正文。"""
    assert 'HALT_TOOL = "wait_for_user"' in TOOLS
    assert "async def wait_for_user" in TOOLS
    seg = _fn(MAIN, "async def _run_relay")
    assert "tools.HALT_TOOL" in seg, "调度器没有按工具判定叫停"
    assert "return" in seg, "叫停后必须真的不再往下传棒"


def test_halted_stops_downstream_stages():
    seg = _fn(MAIN, "async def _run_relay")
    assert '"type": "halted"' in seg, "叫停要有事件, 否则前端不知道在等人"


def test_one_stage_failure_does_not_feed_downstream():
    """一棒炸了就停 —— 半截产物传下去只会让后面基于错的东西接着做。"""
    seg = _fn(MAIN, "async def _run_relay")
    j = seg.index("except Exception")
    # break 还是 return 是实现细节, 要钉的是"不再往下传棒"
    assert "return" in seg[j : j + 400], "一棒炸了还继续跑下游"


def test_parallel_mode_survives():
    """头脑风暴仍要并行 —— 改成一律串行就变回"排队发言"。"""
    assert "async def _run_room" in MAIN
    assert "asyncio.create_task" in MAIN
    assert 'runner = _run_relay if room.mode == "relay" else _run_room' in MAIN
    assert 'mode: str = "parallel"' in ROOMS, "老房间/老 rooms.json 必须仍是并行"


def test_gate_users_are_wired_to_the_tool():
    """三个该停的工位都要用工具, 而不是只在人格里说"请过目"。"""
    for role in ("director", "storyboard", "videographer"):
        i = ROOMS.index(f'"{role}"')
        assert "wait_for_user" in ROOMS[i : i + 1000], f"{role} 没接叫停工具"


def test_ui_has_a_one_click_new_film_entry():
    """一句话出片 = 不用手工拉五个人, 也不用自己记谁先谁后。"""
    web = (TEAM / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="newFilm"' in web
    assert "/api/rooms/crew" in web


# ── 流式 Markdown 渲染 (2026-08-31 老板截图: 阿导整段发言是原始 markdown,
# 引用/加粗/编号全是字面量) ─────────────────────────────────────────────
WEB = (TEAM / "web" / "index.html").read_text(encoding="utf-8")


def test_bot_output_is_markdown_rendered():
    """照 AgentsDance 的 MarkdownText 搬 (零依赖自研, 可对流式增量反复渲染)。"""
    assert "function mdRender" in WEB
    for block in ("blockquote", "<hr>", "<pre><code>", "<table>"):
        assert block in WEB, f"渲染器没覆盖 {block}"
    # 列表的标签是拼出来的 (ordered ? "ol" : "ul"), 搜字面量搜不到 —— 钉语义
    assert "ordered ? 'ol' : 'ul'" in WEB, "渲染器没覆盖有序/无序列表"
    # 样式也得跟上, 否则渲染出来了却没排版
    for css in (".txt blockquote", ".txt table", ".txt pre", ".txt ul,.txt ol"):
        assert css in WEB, f"缺样式 {css}"


def test_streaming_rerenders_from_accumulated_source():
    """增量必须累加**原文**再整份重渲。

    直接往 innerHTML 上拼渲染结果, 会在标记跨块断开时渲出半截标签
    (`**加粗` 的后半截还没到)。整份重渲是纯函数, 便宜且永远自洽。
    """
    assert "dataset.raw" in WEB
    i = WEB.index("setText(el, (el.dataset.raw")
    assert i > 0, "流式没有走累加原文 + 重渲"
    assert ".txt').textContent += ev.text" not in WEB, "还留着旧的纯文本累加"


def test_bot_html_is_escaped_before_innerHTML():
    """React 那边自动转义, 原生这边必须自己转 —— 机器人吐 <script> 就是 XSS。

    实测过: <img onerror>/<script>/javascript: 四道全挡 (浏览器断言)。
    """
    assert "function esc(t)" in WEB
    i = WEB.index("function mdInline")
    seg = WEB[i : i + 400]
    assert "esc(t)" in seg, "行内渲染没有先转义"
    # 链接只放行 http(s) —— javascript: 伪协议是另一条 XSS 路
    assert "https?:\\/\\/" in WEB or "https?:\\/" in WEB or "(https?:" in WEB


def test_user_text_stays_literal():
    """用户自己打的字不走 markdown —— 他打什么就该看到什么。"""
    i = WEB.index("function setText")
    seg = WEB[i : i + 300]
    assert "isUser" in seg and "textContent" in seg


# ── 工具行折叠 + 谁在工作 (2026-08-31 老板: "屏幕有限, 用户很难捕捉哪个专家
# 正在工作") ────────────────────────────────────────────────────────────
def test_tool_rows_collapse_into_one_summary_line():
    """一个工位随手十几步, 平铺能占满整屏 —— 谁在干活反而看不出来了。

    实测: 11 步从 470px 收成 85px, 省 385px。
    """
    assert "function toolBox" in WEB
    assert ".toolbox .tl { display:none" in WEB, "默认必须是收起的"
    assert ".toolbox.open .tl { display:block" in WEB


def test_summary_shows_the_latest_step_not_a_count_only():
    """摘要要回答"此刻在干嘛", 不是"一共干了多少"。"""
    i = WEB.index("function syncToolSummary")
    seg = WEB[i : i + 500]
    assert "rows[rows.length - 1]" in seg, "摘要没有取最后一行"


def test_only_one_bot_is_marked_running():
    """接力模式下"工作中"就等于"当前这一棒是谁" —— 屏幕有限, 这比堆工具行有用。"""
    assert ".msg.bot.running" in WEB
    i = WEB.index("const bubbleFor = id =>")
    seg = WEB[i : i + 600]
    assert "querySelectorAll('.msg.bot.running')" in seg, "没有摘掉上一位的标记"
    assert "classList.add('running')" in seg


def test_finished_stage_stops_occupying_screen():
    """交棒后摘掉工作中标记并收起工具组 —— 跑完了就不该再占屏。"""
    i = WEB.index("} else if (ev.type === 'end') {")
    seg = WEB[i : i + 500]
    assert "classList.remove('running')" in seg
    assert "toolbox.open" in seg


# ── 续跑 (2026-08-31 老板: "中间还会停顿吗") ────────────────────────────
# 查出来两处**非设计**的停顿, 都比"停一下"更糟:
#   · 用户回话后接力从头重来 —— 美术会照字面再出一遍图, 那是真花钱
#   · 三十步通用上限把逐镜头出片的工位掐在半路 (三分钟片 ≈ 三四十个镜头)
AGENT = (TEAM / "app" / "agent.py").read_text(encoding="utf-8")


def test_relay_resumes_instead_of_restarting():
    """人审后从**下一棒**续跑 —— 重跑美术 = 再花一遍出图钱。"""
    assert "resume_at" in ROOMS, "Room 没有记住停在哪一棒"
    seg = _fn(MAIN, "async def _run_relay")
    assert "room.resume_at" in seg
    assert "for idx in range(start, len(members))" in seg, "还是从头遍历"
    assert '"type": "resume"' in seg, "续跑要有事件, 否则用户不知道跳过了几棒"


def test_capped_stage_resumes_itself_not_the_next():
    """撞上限 = 这一棒没干完 → 重跑**本棒**; 人审 = 交了活 → 跑**下一棒**。"""
    seg = _fn(MAIN, "async def _run_relay")
    assert "nxt = idx if capped_here else idx + 1" in seg
    assert 'ev.get("capped")' in seg, "没有识别撞上限"


def test_failed_stage_resumes_itself():
    seg = _fn(MAIN, "async def _run_relay")
    j = seg.index("except Exception")
    assert "room.resume_at = idx" in seg[j : j + 300], "炸了要停在本棒"


def test_shot_by_shot_roles_get_a_higher_step_cap():
    """三分钟片 ≈ 三四十个镜头, 三十步必然掐在半路。"""
    assert "LONG_RUN_BOTS" in AGENT
    assert "videographer" in AGENT and "artist" in AGENT
    assert "max_steps = LONG_RUN_STEPS if bot_id in LONG_RUN_BOTS else MAX_STEPS" in AGENT
    # 通用上限不许为了一个特例整体放开 —— 它是防跑飞的
    assert 'os.environ.get("AGENTS_TEAM_MAX_STEPS", "30")' in AGENT


def test_capped_turn_tells_the_user_it_is_unfinished():
    """被掐时正文往往看着像正常收尾 —— 必须说清楚"还没干完"。"""
    assert "回一句「继续」可以接着干" in AGENT
    assert '"capped": True' in AGENT


# ── 网关瞬断重试 (2026-09-01 老板跑片时被打断两次) ──────────────────────
# 一次 502 让阿摄整棒作废: 它读镜头表读到一半撞上 dhc-server 重建窗口 (容器换
# IP, Caddy DNS 缓存约一秒不同步), 而后台其实什么都没坏, 重发一次就好。
def test_gateway_retries_only_transient_failures():
    """该重试的与不该重试的, 判据要写死在源码里 (行为测试见 image 内实测)。"""
    assert "_RETRY_STATUS = (429, 500, 502, 503, 504)" in AGENT
    assert "GATEWAY_TRIES" in AGENT
    # 400/401/403 不在表里 —— 请求本身有问题, 重试一百次也一样
    for code in ("400", "401", "403"):
        i = AGENT.index("_RETRY_STATUS = (")
        assert code not in AGENT[i : i + 60], f"{code} 不该进重试表"


def test_retry_lives_below_the_step_loop():
    """重试**不算一步**。

    放在调用方那个 for 里 continue 会把网络抖动记成"干了一步活" —— 三次抖动
    吃掉三格步数上限, 而三分钟短剧的出片工位本来就在跟上限赛跑。
    """
    assert "async def _stream_once(" in AGENT, "重试层没有独立出来"
    i = AGENT.index("async def _stream(")
    seg = AGENT[i : i + 1200]
    assert "for attempt in range(GATEWAY_TRIES)" in seg
    assert "_stream_once(" in seg


def test_no_retry_after_content_already_streamed():
    """已经吐出去的 token 用户看到了, 重来会让他看到重复的字。"""
    i = AGENT.index("async def _stream(")
    seg = AGENT[i : i + 1200]
    assert "yielded" in seg
    assert "if yielded or not transient" in seg, "吐过内容还重试 = 正文重复"


def test_asyncio_is_imported_for_the_backoff():
    """退避要 await asyncio.sleep —— 而 agent.py 原先根本没 import asyncio。

    这条不是形式主义: 少了它, 重试**一触发就 NameError**, 比原来的 502 更糟,
    而且只在真抖动时才炸 (平时全绿)。是镜像内跑行为测试当场撞出来的。
    """
    assert "import asyncio" in AGENT
    i = AGENT.index("async def _stream(")
    assert "asyncio.sleep" in AGENT[i : i + 1200]


# ── 字段名必须与服务端实际返回一致 (2026-09-01: 我今晚第三次栽在"想当然") ──
# 上传返回体的字段是 download_url, 我写成了 url —— 于是上传**全部成功**却取到
# 空串, 报"参考图上传失败"且原因为空。阿摄被这个假错误折腾了十几步 (换绝对
# 路径、换 jpg、验 PNG 魔数), 全是白费。
#
# 这条测试跨文件比对: 从服务端 media.py 里读出真实字段, 再断言容器侧在用它。
# 光看容器侧的源码永远发现不了 —— 那边写什么都"看起来对"。
SERVER_MEDIA = (ROOT / "server" / "app" / "media.py").read_text(encoding="utf-8")


def test_upload_response_fields_match_the_server():
    i = SERVER_MEDIA.index('"upload_url": f"{base}')
    seg = SERVER_MEDIA[i - 200 : i + 400]
    assert '"download_url"' in seg, "服务端改了字段名, 这条测试的前提没了"
    # 容器侧必须用同一对字段
    assert 'd.get("upload_url")' in MEDIA
    assert 'd.get("download_url")' in MEDIA
    assert 'd.get("url")' not in MEDIA, "又在用不存在的 url 字段"


def test_video_job_fields_match_the_server():
    assert '"id": job_id, "model": model, "task_status": "PROCESSING"' in SERVER_MEDIA
    assert '"video_result": [{"url": job["url"]' in SERVER_MEDIA
    # 容器侧: 提交读 id, 轮询读 task_status + video_result[0].url
    assert '.get("id")' in MEDIA
    assert 'd.get("task_status")' in MEDIA
    assert '"video_result"' in MEDIA


def test_upload_failure_always_says_why():
    """空原因比报错更糟 —— 模型拿到"失败但没说为什么"只能瞎试。"""
    i = MEDIA.index("async def _upload_blob")
    seg = MEDIA[i : i + 1600]
    # 每一条失败出口都要带上原因
    assert 'return "", f"文件不存在' in seg
    assert "申请上传位 HTTP" in seg
    assert "实际字段" in seg, "字段对不上时要报出实际字段名, 而不是回空"
    assert "上传 HTTP" in seg
    # 调用方兜底: 万一还是空原因也要说人话
    assert "没有返回失败原因" in MEDIA


def test_upload_content_type_follows_the_suffix():
    """一律报 image/png 会让 jpg 存成 png —— 没必要赌上游按魔数纠错。"""
    assert "_CTYPE" in MEDIA
    assert '".jpg": "image/jpeg"' in MEDIA


def test_reference_images_reach_both_input_styles():
    """**参考图必须两个字段都发。**

    服务端按模型的 video_input 分流: img_url 系 (seedance) 只看 image_url;
    media 系 (**万相 3.0 —— 我们的默认视频模型**) 只看 media 数组, image_url
    被完全忽略。只发 image_url 的话默认模型下参考图被**静默丢弃** —— 十几个
    镜头的人物一致性全没了, 而且不报任何错。

    服务端注释原话: "把它压成一张首帧, 等于把用户的参考素材悄悄丢掉, 他付了钱
    却拿到一条无视素材的视频, 比直接报错更糟"。我读过那段注释, 然后还是只发了
    image_url —— 所以钉一条测试。
    """
    # 服务端确实按 input_style 分流 (前提成立才谈得上这条契约)
    assert 'video_input_style(model) == "media"' in SERVER_MEDIA
    assert 'payload["input"]["media"] = media' in SERVER_MEDIA
    assert 'payload["input"]["img_url"] = image_url' in SERVER_MEDIA
    # 容器侧两个都发
    assert 'body["image_url"] = urls[0]' in MEDIA
    assert 'body["media"] = [{"url": u} for u in urls]' in MEDIA


def test_media_item_shape_and_cap_match_the_server():
    """media 每项要 {"url": ...}, 且不超过服务端的上限 (超了整个请求 400)。"""
    assert "_MEDIA_MAX_ITEMS = 8" in SERVER_MEDIA
    assert "MEDIA_MAX_ITEMS = 8" in MEDIA, "上限没跟服务端对齐"
    assert "each media item needs a url" in SERVER_MEDIA
    assert '{"url": u}' in MEDIA


def test_multiple_reference_images_are_supported():
    """一个镜头里角色+场景+道具本该一起当参考 —— 只开一张是浪费。"""
    i = MEDIA.index("async def generate_video")
    seg = MEDIA[i : i + 900]
    assert "image: str | list | None" in seg, "image 还是只收单张"
    assert "isinstance(image, str)" in seg, "得兼容单张写法"
    # 工具描述与人格都要说得出来, 否则模型不会用
    assert "最多 8 张" in MEDIA
    j = ROOMS.index('"videographer"')
    assert "数组" in ROOMS[j : j + 1000], "阿摄的人格没提可以给多张"


def test_upload_sends_only_fields_the_server_reads():
    """服务端只读 content_type / file_name —— 发 size 是噪音。"""
    i = SERVER_MEDIA.index('@router.post("/v1/media/uploads")')
    seg = SERVER_MEDIA[i : i + 1400]
    reads = {m for m in ("content_type", "file_name", "size") if f'body.get("{m}")' in seg}
    assert reads == {"content_type", "file_name"}, f"服务端读的字段变了: {reads}"
    j = MEDIA.index("async def _upload_blob")
    body = MEDIA[j : j + 1200]
    assert '"content_type": ctype' in body and '"file_name": p.name' in body
    assert '"size"' not in body, "还在发服务端不读的 size"


# ── 等待可见性 + 房间可辨识 (2026-09-01 老板: "切出去再回来消息就没了？以及
# 发消息半天不回有没有个计数器以及阶段说明") ─────────────────────────────
def test_waiting_shows_who_and_how_long():
    """一棒能跑好几分钟, 而屏幕上此前只有一片静止 —— 分不出在干活还是死了。"""
    assert "function statusStart" in WEB and "function statusSet" in WEB
    assert ".statusbar" in WEB, "缺样式, 渲染出来也没形"
    # 发出去就起, 收尾必摘 (否则残留一个永远转的圈)
    assert "statusStart('已发出" in WEB
    i = WEB.index("} finally {")
    assert "statusStop()" in WEB[i : i + 200], "收尾没摘状态条"


def test_elapsed_resets_only_when_the_baton_changes():
    """一棒里要跑十几个工具。每次工具调用都重置的话秒数被反复清零,
    永远看不出"这一棒已经跑了多久" —— 而那正是用户要的。(浏览器实测过:
    同一人换工具 3→4 秒不清零, 换人才回到 1 秒。)"""
    i = WEB.index("function statusSet")
    seg = WEB[i : i + 700]
    assert "botId !== statusWho" in seg, "换工具也会清零"
    assert "statusFrom = Date.now()" in seg


def test_relay_step_number_is_shown():
    """第几棒只在剧组房间有意义 —— 别的房间成员不是流水线。"""
    i = WEB.index("function statusSet")
    seg = WEB[i : i + 700]
    assert "CREW_ORDER" in seg
    assert "cur.mode === 'relay'" in seg, "非流水线房间也标棒次会误导"
    # 顺序必须与服务端 Store.CREW 一致, 否则棒次是错的
    assert "['director', 'artist', 'storyboard', 'videographer', 'editor']" in WEB
    assert 'CREW = ("director", "artist", "storyboard", "videographer", "editor")' in ROOMS


def test_rooms_are_distinguishable():
    """开新片不再弹 prompt 问名字 —— 几个房间在侧栏长得一模一样, 切出去再回来
    会以为"消息没了", 其实是切到了另一个同名房间。"""
    assert "prompt('这部片叫什么" not in WEB, "还在弹 prompt"
    assert "新片 ${n}" in WEB, "名字没有序号"
    # 侧栏第二行带消息数 —— 重名时唯一分得出"哪个是我刚才那个"的线索
    assert "r.count" in WEB
    assert '"count": len(store.transcript(r.id))' in MAIN, "服务端没返回 count"
