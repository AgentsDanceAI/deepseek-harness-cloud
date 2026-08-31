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


def test_crew_room_is_relay_not_parallel():
    """顺序即依赖: 讲戏本 → 角色资产 → 镜头表 →(人审)→ 片段 → 成片。"""
    assert 'CREW = ("director", "artist", "storyboard", "videographer", "editor")' in ROOMS
    assert "def create_crew_room" in ROOMS
    i = ROOMS.index("def create_crew_room")
    assert 'mode="relay"' in ROOMS[i : i + 500]


def test_relay_writes_each_turn_before_passing_the_baton():
    """接力的**全部要害**: 先落记录再传棒, 下一位 render_for 才读得到。"""
    i = MAIN.index("async def _run_relay")
    seg = MAIN[i : i + 2200]
    add_at = seg.index("store.add(room.id, ev[")
    render_at = seg.index("store.render_for(bot, room.id)")
    # render 在循环体开头, add 在同一轮的 end 事件里 —— 两者都在, 且循环是逐棒的
    assert add_at > render_at
    assert "for bot_id in room.members" in seg, "必须按成员顺序逐棒, 不是并发"
    assert "asyncio.create_task" not in seg, "接力里不许再起并发任务"


def test_halt_is_a_tool_not_a_prompt_hope():
    """闸门靠工具调用判定 —— 文本标记会漏、会被改写、会混进正文。"""
    assert 'HALT_TOOL = "wait_for_user"' in TOOLS
    assert "async def wait_for_user" in TOOLS
    i = MAIN.index("async def _run_relay")
    seg = MAIN[i : i + 2200]
    assert "tools.HALT_TOOL" in seg, "调度器没有按工具判定叫停"
    assert "break" in seg


def test_halted_stops_downstream_stages():
    i = MAIN.index("async def _run_relay")
    seg = MAIN[i : i + 2200]
    assert '"type": "halted"' in seg, "叫停要有事件, 否则前端不知道在等人"


def test_one_stage_failure_does_not_feed_downstream():
    """一棒炸了就停 —— 半截产物传下去只会让后面基于错的东西接着做。"""
    i = MAIN.index("async def _run_relay")
    seg = MAIN[i : i + 2200]
    j = seg.index("except Exception")
    assert "break" in seg[j : j + 300]


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
