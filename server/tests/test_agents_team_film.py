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
    seg = _tool_schema("generate_video")
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
    assert "只重跑那一条" in ROOMS[i : i + 2200]


def test_ffmpeg_is_in_the_image():
    """剪辑师靠它拼片 —— 镜像里没有的话, 报错只会出现在容器日志里。"""
    assert "ffmpeg" in DOCKERFILE


def test_media_tools_registered():
    for name in ("generate_image", "generate_video", "concat_videos", "media_models"):
        assert f'"{name}"' in MEDIA
    assert "media.SCHEMAS" in TOOLS and "media.HANDLERS" in TOOLS


def test_media_module_does_not_import_tools():
    """media 与 tools 互相引用会成环 (tools 合并工具表时 import media)。

    落盘位置改由 filmdir 说了算之后, 这里不再各读一次 env: 两边各算各的常量时,
    "当前是哪部片"这种**会变的**状态同步不了 —— 读写落在片目录、出片落在根上,
    症状是"图明明生成了却读不到"。filmdir 谁也不 import, 所以两边都能用它。
    """
    assert "from . import tools" not in MEDIA
    assert "filmdir" in MEDIA, "落盘位置要走 filmdir, 不许自己再派生一份"
    assert "AGENTS_TEAM_WORKDIR" not in MEDIA, "不该再各读一次 env 算自己的根"


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
    # 必须按包加载: media.py 里有 `from . import filmdir`, 单文件 exec 会
    # ImportError (attempted relative import with no known parent package)。
    mod = _crew("media")
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
    # relay 现在来自剧组模板 (teams.py), create_crew_room 只是 create_team_room("film")
    assert 'create_team_room("film"' in ROOMS[i : i + 500]
    assert _crew("teams").BY_ID["film"].mode == "relay", "剧组模板不是接力 —— 流水线就散了"


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
    """一句话组队 = 不用手工拉五个人, 也不用自己记谁先谁后。

    2026-09-02 起剧组只是十个模板之一: 入口从「开新片」变成「组个团队」选模板,
    建群走 /api/rooms/team; /api/rooms/crew 保留给老调用方。
    """
    web = (TEAM / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="newTeam"' in web and 'id="teamDlg"' in web, "没有组队入口/选单"
    assert "/api/rooms/team" in web and "/api/teams" in web
    assert '@app.post("/api/rooms/crew")' in MAIN, "老入口 /api/rooms/crew 不能拆"


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
    # 长跑名单由团队模板推 (teams.Team.long_run), 出片/出图的工位必须在里面
    lr = _crew("agent").LONG_RUN_BOTS
    assert "videographer" in lr and "artist" in lr, f"出片/出图工位不在长跑名单: {sorted(lr)}"
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


def _tool_schema(name: str) -> str:
    """取某个工具 schema 的完整片段。

    固定字符窗口 (MEDIA[i:i+2000]) 每次往描述里加一句注释就会失效 —— 我为此
    连修了三轮断言。按**下一个工具的 name** 切, 结构上永远是准的。
    """
    i = MEDIA.index(f'"name": "{name}"')
    nxt = MEDIA.find('"name": "', i + 20)
    return MEDIA[i : nxt if nxt > 0 else len(MEDIA)]


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
    i = MEDIA.index("async def _upload_once")  # 干活的那段在这里 (_upload_blob 是重试外壳)
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
    assert 'body["media"] = (' in MEDIA and '"url": u' in MEDIA


def test_media_item_shape_and_cap_match_the_server():
    """media 每项要 {"url": ...}, 且不超过服务端的上限 (超了整个请求 400)。"""
    assert "_MEDIA_MAX_ITEMS = 8" in SERVER_MEDIA
    assert "MEDIA_MAX_ITEMS = 8" in MEDIA, "上限没跟服务端对齐"
    assert "each media item needs a url" in SERVER_MEDIA
    assert '"url": u' in MEDIA


def test_multiple_reference_images_are_supported():
    """一个镜头里角色+场景+道具本该一起当参考 —— 只开一张是浪费。"""
    i = MEDIA.index("async def generate_video")
    seg = MEDIA[i : i + 2000]
    assert "image: str | list | None" in seg, "image 还是只收单张"
    assert "isinstance(image, str)" in seg, "得兼容单张写法"
    # 工具描述与人格都要说得出来, 否则模型不会用
    assert "最多 8 张" in MEDIA
    j = ROOMS.index('"videographer"')
    assert "多张" in ROOMS[j : j + 1200], "阿摄的人格没提可以给多张"


def test_upload_sends_only_fields_the_server_reads():
    """服务端只读 content_type / file_name —— 发 size 是噪音。"""
    i = SERVER_MEDIA.index('@router.post("/v1/media/uploads")')
    seg = SERVER_MEDIA[i : i + 1400]
    reads = {m for m in ("content_type", "file_name", "size") if f'body.get("{m}")' in seg}
    assert reads == {"content_type", "file_name"}, f"服务端读的字段变了: {reads}"
    j = MEDIA.index("async def _upload_once")  # 干活的那段在这里 (_upload_blob 是重试外壳)
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
    """第几棒只在接力房间有意义 —— 并行房间成员不是流水线。

    棒次从**房间成员表**推 (cur.members), 不再写死剧组那五个 id: 有十个团队之后,
    写死等于只有剧组能显示棒次, 而且移出一个成员后棒次就错位。
    """
    i = WEB.index("function statusSet")
    seg = WEB[i : i + 800]
    assert "cur.members" in seg, "棒次没从房间成员表推"
    assert "cur.mode === 'relay'" in seg, "非流水线房间也标棒次会误导"
    assert "CREW_ORDER" not in WEB, "还在写死剧组顺序"
    # 服务端: 剧组模板的顺序与老的 Store.CREW 一致 (老调用方还认它)
    assert 'CREW = ("director", "artist", "storyboard", "videographer", "editor")' in ROOMS
    teams = _crew("teams")
    assert teams.BY_ID["film"].members == ("director", "artist", "storyboard", "videographer", "editor")


def test_rooms_are_distinguishable():
    """开新片不再弹 prompt 问名字 —— 几个房间在侧栏长得一模一样, 切出去再回来
    会以为"消息没了", 其实是切到了另一个同名房间。"""
    assert "prompt('这部片叫什么" not in WEB, "还在弹 prompt"
    assert "新${t.name} ${n}" in WEB, "名字没有序号 (按团队计数)"
    # 侧栏第二行带消息数 —— 重名时唯一分得出"哪个是我刚才那个"的线索
    assert "r.count" in WEB
    assert '"count": len(store.transcript(r.id))' in MAIN, "服务端没返回 count"


# ── media 每项必须带 type + 长工具要报进度 (2026-09-01 老板: 出片一直失败;
# "生成过程中能计数吗, 各个阶段都计数, 别让用户干等以为卡住了") ───────────
AGENT_SRC = (TEAM / "app" / "agent.py").read_text(encoding="utf-8")


def test_media_items_carry_a_type():
    """服务端只校验 url 所以放行, 上游 (百炼) 退 `Field required: input.media.0.type`。

    而那个错**只出现在 video_jobs 表里** —— 界面上只有一句"出片失败", 于是
    阿导以为是"image 参数格式变了", 又开始瞎试。取值来自同仓的 ComfyUI 垫片
    (workspace-comfyui/api_shim.py), 不是我猜的。
    """
    shim = (ROOT / "deploy" / "workspace-comfyui" / "api_shim.py").read_text(encoding="utf-8")
    assert 'item["type"] == "first_frame"' in shim, "垫片改了取值, 这条的前提没了"
    assert '"type": "first_frame"' in MEDIA
    assert '"reference_image"' in MEDIA
    # 第一张当首帧 —— 它决定第一帧长什么样; 其余当参考图
    i = MEDIA.index('body["media"]')
    seg = MEDIA[i : i + 300]
    assert "if len(urls) == 1" in seg, "没有按张数分形态 (首帧 / 全能参考互斥)"


def test_long_tools_report_progress():
    """出片一条几分钟, 而工具是"一次调用走完"的同步语义 —— 中间没有任何东西
    告诉用户"还活着"。心跳必须真能泵出来 (镜像内实测过 3 条)。"""
    assert "def set_progress" in MEDIA
    i = MEDIA.index("deadline = started + VIDEO_POLL_TIMEOUT_S")
    seg = MEDIA[i : i + 500]
    assert "_progress(" in seg and "出片中" in seg
    # agent 侧: 队列桥接 (异步生成器里 await 同步工具, 没法直接 yield)
    assert "asyncio.Queue" in AGENT_SRC
    import re as _re

    assert _re.search(r'yield \{\s*"type": "progress"', AGENT_SRC), "没有 progress 事件"
    assert "call_soon_threadsafe" in AGENT_SRC
    # 用完要置回, 否则下一轮的心跳会打到上一轮的队列上
    assert "set_progress(None)" in AGENT_SRC


def test_progress_updates_in_place_not_as_new_rows():
    """每 6 秒一条, 新起一行会把工具组刷成一屏噪音。"""
    i = WEB.index("ev.type === 'progress'")
    seg = WEB[i : i + 600]
    assert "tools.get(" in seg, "没有定位到原来那行"
    assert "syncToolSummary" in seg, "摘要没跟着走"
    assert "statusSet(" in seg, "状态条没跟着走"


# ── 上游真实传参 (2026-09-01 老板给了千面官方示例 + 百炼两个 skill 包) ──────
# 我此前梳理的多图规则是**推**出来的, 全错。坏图探测法实打对照:
#     images=[坏图]      -> 400 InvalidParameter.TaskTypeConstraint  ← 真读了
#     image_urls=[坏图]  -> 200 照收                                  ← 被静默忽略
#     image_url=坏图     -> 400 (seedance 认, 但只吃单张)
SERVER_MEDIA2 = (ROOT / "server" / "app" / "media.py").read_text(encoding="utf-8")


def test_qianmian_uses_the_images_array():
    """千面的参考素材字段是 `images` 数组 (供应商官方示例 + 坏图探测双重确认)。"""
    i = SERVER_MEDIA2.index('payload = {"model": model, "prompt": prompt, "resolution"')
    seg = SERVER_MEDIA2[i : i + 900]
    assert 'payload["images"] = imgs' in seg, "还在用单数 image_url 发多图"
    assert '"image_urls"' not in seg, "image_urls 是被上游忽略的字段"
    # media 摊平成 images —— 容器侧只发一套, 服务端负责翻译到各上游
    assert "media or []" in seg


def test_ratio_is_passed_explicitly():
    """上游默认自适应, 提示词里写"横屏"**不吃** (万相 3.0 官方协议)。"""
    i = SERVER_MEDIA2.index('payload = {"model": model, "prompt": prompt, "resolution"')
    assert 'payload["ratio"] = ratio' in SERVER_MEDIA2[i : i + 900]
    assert 'body["ratio"] = ratio or "16:9"' in MEDIA, "容器侧没给默认画幅"


def test_reference_and_frame_modes_are_mutually_exclusive():
    """`reference_*` 与 `first_frame`/`last_frame` 同传直接报错 (官方协议)。

    我原先写的"第一张当首帧, 其余当参考图" —— 传两张以上必炸, 而错误只落进
    作业表, 界面上还是那句"出片失败"。
    """
    # 服务端兜一道 (容器侧改坏了也不至于打到上游)
    i = SERVER_MEDIA2.index('if video_input_style(model) == "media"')
    seg = SERVER_MEDIA2[i : i + 1200]
    assert "reference_image" in seg and "first_frame" in seg
    assert "last_frame 必须与 first_frame 同时给" in seg
    # 容器侧: 一张走首帧, 多张全走参考, 不混
    j = MEDIA.index('body["media"] = (')
    cseg = MEDIA[j : j + 400]
    assert "if len(urls) == 1" in cseg, "还在混着发"
    assert '"reference_image"' in cseg


def test_wan3_skill_packs_are_shipped_and_referenced():
    """整包进镜像而不是把要点抄进人格 —— 抄会失真, 也没法随上游更新。"""
    skills = TEAM / "skills"
    assert (skills / "wan3-drama-prompt" / "SKILL.md").is_file()
    assert (skills / "wan3-ecommerce-prompt" / "SKILL.md").is_file()
    assert "COPY skills/ /opt/agents-team/skills/" in DOCKERFILE
    # 分镜师被明确要求动笔前先读 —— 不引用等于白带
    i = ROOMS.index('"storyboard"')
    assert "wan3-drama-prompt" in ROOMS[i : i + 1200]


# ── 出片参数与重复出片 (2026-09-01 老板截图: 一小时二十多条同一镜头) ────────
def test_duration_and_resolution_are_required():
    """描述里写"省略 5"等于告诉模型可以不写, 它就真不写。

    实测: 十几条镜头全出成 5 秒, 阿摄自己都发现"每次发的 JSON 里只有
    prompt/path/ratio/model 四个键"。镜头表写 10 秒的镜头出成 5 秒是废片,
    而且照样扣钱。
    """
    seg = _tool_schema("generate_video")
    assert '"required": ["prompt", "path", "duration", "resolution"]' in seg
    # ⚠️ 别用"省略 5"这种子串判 —— 新描述里那句"**省略**会变成 **5** 秒"是在
    # 警告后果, 会被误判成"还在教模型省略"(我为此多修了一轮)。钉真语义:
    # 描述必须说"必填", 且不能出现"省略 <默认值>"这种许可式写法。
    dur = seg[seg.index('"duration"') : seg.index('"resolution"')]
    assert "必填" in dur, "duration 描述没说必填"
    assert "省略 5" not in dur.replace("省略会变成 5 秒", ""), "描述里还在教模型省略"
    res = seg[seg.index('"resolution"') : seg.index('"ratio"')]
    assert "必填" in res, "resolution 描述没说必填"


def test_model_choice_is_not_exposed_to_the_agent():
    """模型看不见账单。实测阿摄自己挑了 wan3.0-video-prime (15 积分/秒),
    比默认的 wan3.0-video (10) 贵一半。型号由工作台注入, 换档是用户的决定。"""
    seg = _tool_schema("generate_video")
    assert '"model": {"type": "string"' not in seg, "又把型号开放给模型选了"


def test_regenerating_an_existing_clip_is_blocked():
    """**这条是省钱闸。** 阿摄在同一个镜头上反复调用二十多次, 一小时烧掉一千多
    积分 —— 它以为在"修 duration 参数", 而每一次都真下单真扣钱。
    模型看不见账单, 闸必须在工具里。"""
    i = MEDIA.index("async def generate_video")
    seg = MEDIA[i : i + 1400]
    assert "out.exists()" in seg and "没有重新出片" in seg
    # 要给出**可执行的**重做路径, 否则模型会卡住或绕路
    assert "rm " in seg
    # 人格里也要说清楚, 免得它把这个返回当成错误反复重试
    j = ROOMS.index('"videographer"')
    vseg = ROOMS[j : j + 1600]
    assert "不要反复跑" in vseg or "不要靠反复重跑" in vseg
    assert "duration" in vseg, "人格没提 duration 必填"


# ── 轮次锁卡死 + 发送前探活 (2026-09-01 老板: "怎么回事, 没了呢") ──────────
# 连发四条「继续」零回应。查下来是两件事叠在一起。
MAIN2 = (TEAM / "app" / "main.py").read_text(encoding="utf-8")


def test_turn_lock_recovers_from_a_dead_turn():
    """**锁必须能自己恢复。**

    `async with _turn_lock` 包着一个流式生成器 —— 浏览器一断 (关标签页/切走/
    网络抖动/容器被换), 生成器不保证被正常关闭, 锁就永远不释放。之后**所有
    房间**的每一条消息都被"上一轮还在跑"挡死。实测: 直接向工作台发一条, 回的
    就是那句话, 事件总数 1。
    """
    assert "_turn_started" in MAIN2, "没有记录这一轮的开始时间, 无从判断锁死没死"
    assert "TURN_STALE_S" in MAIN2
    i = MAIN2.index("async def send")
    seg = MAIN2[i : i + 2600]
    assert "stale" in seg and "_turn_lock.release()" in seg, "陈旧锁不会被接管"
    # finally 里无条件释放 —— 不依赖 async with 的正常退出路径
    j = seg.index("finally:")
    assert "_turn_lock.release()" in seg[j : j + 300]
    # 陈旧阈值要大于最慢的一轮 (出片一条几分钟, 一棒十几镜可能半小时)
    assert '"2400"' in MAIN2 or "2400" in MAIN2


def test_lock_busy_message_says_how_long():
    """ "上一轮还在跑"要带秒数 —— 不然用户分不清"真在跑"和"卡死了"。"""
    i = MAIN2.index("async def send")
    seg = MAIN2[i : i + 2600]
    assert "上一轮还在跑" in seg and "秒" in seg


def test_send_checks_the_backend_is_alive_first():
    """容器被回收/换版后浏览器手里那条连接是死的, fetch 在 SSE 循环**之前**就
    失败 —— 屏幕上只留一条自己的消息, 什么都不发生。探活一个 GET 很便宜,
    换来的是"说得出为什么"。"""
    i = WEB.index("async function submit")
    seg = WEB[i : i + 1800]
    assert "/api/rooms'" in seg or '/api/rooms"' in seg, "发送前没有探活"
    assert "waitForBackend" in seg, "探活失败后没有重连"
    assert "云电脑连接断了" in seg


def test_time_is_imported_for_the_stale_check():
    """陈旧判断要 time.time() —— 而 main.py 原先没 import time。

    同款坑今晚栽过一次 (agent.py 加了 asyncio.sleep 却没 import asyncio,
    重试一触发就 NameError, 平时全绿)。缺 import 不影响语法, 只在真触发时炸。
    """
    assert "\nimport time\n" in MAIN2
    assert "time.time()" in MAIN2


# ── 空流 / 空棒 (2026-09-01 端到端首跑炸出来的) ────────────────────────────────
# 这一组和本文件其余部分不同: **不读源码文本, 跑真行为**。源码断言对 bug 和正解
# 同时成立 —— 首跑那天 60+ 条全绿, 而炸出来的十个问题一条都没接住。


def _crew(mod: str):
    """把容器里的 app 包挂成 crew_app 再 import。

    不能直接 `from app import agent` —— 服务端自己也有个 app 包, 名字撞车; 而
    容器侧 agent.py 写的是 `from . import tools`, 必须以包的形式加载才解析得了。
    """
    import importlib
    import sys
    import types

    if "crew_app" not in sys.modules:
        pkg = types.ModuleType("crew_app")
        pkg.__path__ = [str(TEAM / "app")]
        sys.modules["crew_app"] = pkg
    return importlib.import_module(f"crew_app.{mod}")


class _NoBackoff:
    """只把 sleep 换成立即返回, 其余属性照转给真 asyncio。"""

    def __init__(self, real_sleep):
        import asyncio as _a

        self._a = _a
        self._real = real_sleep

    def __getattr__(self, k):
        return getattr(self._a, k)

    async def sleep(self, *_a, **_k):
        await self._real(0)


def test_empty_stream_is_a_failure_not_an_empty_answer():
    """网关 200 + 零 chunk 必须重发, 重发还空就往上抛。

    干净 EOF 不抛异常, 所以它躲过了所有 except 分支: `_stream` 只看到循环正常
    结束就 return, 调用方于是发一个 text=""、tools=[]、usage={} 的 end, 接力照常
    传棒 —— 这正是 2026-09-01 首跑导演那一棒的形状。
    """
    import asyncio

    agent = _crew("agent")
    tries = []

    async def _empty(client, model, messages):
        tries.append(model)
        return
        yield {}  # 这行到不了 — 只是让它成为 async generator

    async def _go():
        agent._stream_once = _empty
        # ⚠️ agent.asyncio **就是** asyncio 本尊 (同一个模块对象), 直接赋 lambda
        # 会把全局 sleep 换成调自己的函数 → RecursionError。要先接住原件。
        real_sleep = asyncio.sleep
        agent.asyncio = _NoBackoff(real_sleep)
        got = []
        with_err = None
        try:
            async for c in agent._stream(None, "m", []):
                got.append(c)
        except agent.GatewayError as e:
            with_err = e
        return got, with_err

    got, err = asyncio.run(_go())
    assert got == [], "空流不该吐出任何 chunk"
    assert err is not None, "空流被当成了'他没话说' —— 必须当故障抛出来"
    assert isinstance(err, agent.EmptyStreamError), f"抛的是 {type(err).__name__}"
    assert len(tries) == agent.GATEWAY_TRIES, (
        f"空流只试了 {len(tries)} 次 —— 它是瞬时故障, 该按 GATEWAY_TRIES 重发"
    )


def test_a_bot_that_produces_nothing_does_not_pass_the_baton():
    """空棒必须停住, 且续跑重跑**本棒**。

    传下去的代价不是"少一段话": 后面的人会对着从没被写出来的产物开工。首跑那天
    导演空棒之后, 美术和分镜读了十几次不存在的讲戏本, 全程无一处报错。
    """
    src = (TEAM / "app" / "main.py").read_text(encoding="utf-8")
    body = src[src.index("async def _run_relay") : src.index("async def _run_room")]
    # 空棒判定必须在 store.add 之后、传棒之前, 且走"重跑本棒"那条路
    assert "not text and not used" in body, "接力层没有空棒判定"
    seg = body[body.index("not text and not used") :]
    seg = seg[: seg.index("yield ev")]
    assert "capped_here = True" in seg, "空棒续跑没停在本棒上 —— 它的活没干完"
    assert '"type": "error"' in seg, "空棒没发 error 事件, 用户看不见为什么停了"


# ── 每部片一个目录 (2026-09-01 验收跑废在这里) ────────────────────────────────


def test_each_film_gets_its_own_directory():
    """两部片不许落进同一个目录, 老房间不许被搬走。

    共用一个目录的代价不是"文件乱": 第二部片开机就看见第一部的 角色/ 和 S01..S20,
    于是美术照"同一角色只做一张权威图"**拒绝重做** (它判断没错, 它只是不知道那是
    上一部的人), 出片去重闸把旧成片当成"这镜出过了"整片跳过 —— 新片跑完等于把旧片
    重剪一遍, **全程不报错**。
    """
    filmdir = _crew("filmdir")

    # 老房间: dir 为空 -> 还用扁平的根, 不迁移 (用户手上跑到 S20 的片不能消失)
    assert filmdir.resolve("") == filmdir.ROOT

    # 两部同名的片必须分开 —— 片名可以重复, 所以目录名要带 room id
    a = filmdir.slug_for("aaa11111", "新片")
    b = filmdir.slug_for("bbb22222", "新片")
    assert a != b, f"两部都叫'新片'却落进同一个目录: {a}"
    assert a.startswith(filmdir.FILMS + "/") and b.startswith(filmdir.FILMS + "/")

    # 片名是用户随手起的: 空格、引号、斜杠都正常, 不能直接当目录名
    dirty = filmdir.slug_for("cc33", '外卖骑手/暴雨夜 "最后一单"')
    assert "/" not in dirty[len(filmdir.FILMS) + 1 :], f"片名里的斜杠穿出去了: {dirty}"
    assert '"' not in dirty


def test_tools_and_media_both_follow_the_current_film():
    """工具落盘要跟着片走, 而且穿越照样得挡住。

    两个模块各有一套路径解析 (media 故意不 import tools, 否则成环) —— 只改一边
    的话, 读写在片目录里而出片落在根上, 症状是"图明明生成了却读不到"。
    """
    import tempfile

    os.environ["AGENTS_TEAM_WORKDIR"] = tempfile.mkdtemp(prefix="film-test-")
    filmdir = _crew("filmdir")
    tools_m = _crew("tools")
    media_m = _crew("media")

    rel = filmdir.slug_for("dd44", "验收片")
    tok = filmdir.use(filmdir.resolve(rel))
    here = filmdir.ROOT / rel

    got = tools_m._resolve("讲戏本.md")
    assert str(got).startswith(str(here)), f"read/write 没跟着片走: {got}"
    got = media_m._safe_rel("片段/S01.mp4")
    assert str(got).startswith(str(here.resolve())), f"出片落盘没跟着片走: {got}"

    # 片目录换了, 穿越闸不能跟着松掉
    try:
        media_m._safe_rel("../../etc/passwd")
    except ValueError:
        pass
    else:
        raise AssertionError("路径穿越没挡住")
    finally:
        filmdir.reset(tok)  # 别把"当前是哪部片"泄漏给后面的测试


def test_old_rooms_json_still_loads():
    """老 rooms.json 没有 dir 键 —— 读不进来的话用户的房间全没了。"""
    rooms_m = _crew("rooms")
    r = rooms_m.Room(
        **{
            "id": "old1",
            "name": "旧片",
            "members": ["director"],
            "created": 1.0,
            "mode": "relay",
            "resume_at": -1,
        }
    )
    assert r.dir == "", "老房间该落在根上 (空 dir), 不该被搬进新目录"


def test_only_one_root_so_overrides_cannot_be_silently_ignored():
    """工作区的根只许有一个定义 —— 两个根 = 覆盖掉一个, 另一个照旧。

    2026-09-01 构建自检抓到: tools 自留了一份 WORKDIR 常量, 于是 verify.py 里
    `tools.WORKDIR = 临时目录` 的覆盖对路径解析**完全无效** —— 每个工具都报成功,
    文件却写在 /workspace。这类 bug 在单测里看不见 (单测不覆盖根), 只有真去磁盘上
    找文件才发现。
    """
    tools_src = (TEAM / "app" / "tools.py").read_text(encoding="utf-8")
    rooms_src = (TEAM / "app" / "rooms.py").read_text(encoding="utf-8")
    media_src = (TEAM / "app" / "media.py").read_text(encoding="utf-8")
    for name, src in (("tools", tools_src), ("rooms", rooms_src), ("media", media_src)):
        assert "AGENTS_TEAM_WORKDIR" not in src, (
            f"{name}.py 又自己从 env 派生了一个根 —— 根只许 filmdir 定义一处"
        )

    # 而且 ROOT 必须是**可改写的**: ContextVar 的 default 在创建时定死, 存 ROOT
    # 进去的话, 自检改 filmdir.ROOT 依然不生效 (同一个 bug 换个地方)。
    filmdir = _crew("filmdir")
    import contextvars
    import pathlib

    old = filmdir.ROOT
    try:
        filmdir.ROOT = pathlib.Path("/tmp/__root_override_probe__")
        # 在**干净的 Context** 里探: 显式 use() 过的片目录本就该压过 ROOT, 而这里
        # 要验的是"没设过片目录时回落到当前 ROOT"。直接调会读到上一条测试留下的值。
        assert contextvars.Context().run(filmdir.current) == filmdir.ROOT, (
            "改写 ROOT 后 current() 没跟上 —— ContextVar 的 default 又被定死了"
        )
    finally:
        filmdir.ROOT = old


# ── 花钱的位置不许把瞬时故障当终局 (2026-09-01 验收跑读账读出来的) ─────────────


def test_a_flaky_poll_does_not_abandon_a_paid_video():
    """查询抖两下不许放弃 —— 片子在上游出着, 钱已经扣了。

    验收跑实测: 作业 e92c5262 上游 succeeded、扣了 100 积分, 而剧组收到的是
    "出片查询失败"。模型看到失败的本能是重试, 而出片提交就是**下单** —— 重试
    等于同一个镜头付两次钱。工具返回里看不出这件事, 只有查 video_jobs 才知道。

    这条**真跑 generate_video**, 不只验话术: 只验话术的话, 把轮询里的容错删掉
    测试照样绿 (helper 还在)。
    """
    import asyncio
    import pathlib as _pl
    import tempfile

    filmdir = _crew("filmdir")
    media = _crew("media")
    # ⚠️ filmdir.ROOT 是 import 期从 env 派生的 —— 这时候再设 AGENTS_TEAM_WORKDIR
    # 已经晚了 (模块早被别的测试 import 过)。要像镜像自检那样直接改 ROOT。
    _saved = (filmdir.ROOT, media._client, media.VIDEO_POLL_INTERVAL_S)
    filmdir.ROOT = _pl.Path(tempfile.mkdtemp(prefix="poll-test-"))
    media.GATEWAY_BASE = "http://gw/v1"
    media.GATEWAY_TOKEN = "t"
    media.VIDEO_MODEL = "wan3.0-video"
    media.VIDEO_POLL_INTERVAL_S = 0

    polls = {"n": 0}

    class _R:
        def __init__(self, code, payload=None):
            self.status_code = code
            self.text = "boom"
            self._p = payload or {}

        def json(self):
            return self._p

    class _Stream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            yield b"MP4DATA"

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            return _R(200, {"id": "job-abc"})  # 下单成功一次

        async def get(self, url, **k):
            polls["n"] += 1
            if polls["n"] <= 2:
                return _R(502)  # 抖两次
            return _R(200, {"task_status": "SUCCESS", "video_result": [{"url": "http://x/v.mp4"}]})

        def stream(self, *a, **k):
            return _Stream()

    media._client = lambda *a, **k: _C()

    async def _go():
        return await media.generate_video("一个镜头", "片段/T1.mp4", 10, "720p")

    try:
        body, _summary = asyncio.run(_go())
        assert polls["n"] >= 3, f"抖了两次就不查了 (只查了 {polls['n']} 次) — 已付费的片被丢掉"
        assert "已出片" in body, f"熬过抖动后该拿到片子, 实际: {body[:120]}"
        assert (filmdir.ROOT / "片段/T1.mp4").exists(), "片子没落盘"
    finally:
        # 猴补丁必须还原 —— 模块是缓存在 sys.modules 里的, 漏了就污染同轮其它测试
        filmdir.ROOT, media._client, media.VIDEO_POLL_INTERVAL_S = _saved


def test_giving_up_on_polling_must_not_invite_a_second_order():
    """真放弃时话术要拦住重新下单 —— 那是第二次扣费。"""
    media = _crew("media")
    body, summary = media._paid_but_unpolled("job-123", "HTTP 502")
    assert "job-123" in body, "没说是哪个作业 — 模型没法自己去取"
    assert "不要重新下单" in body, "没拦住重新下单 = 第二次扣费"
    assert "已付费" in summary, f"摘要没标已付费: {summary}"
    assert media.POLL_GIVEUP > 1, "一次失败就放弃 = 丢掉已付费的片"


def test_upload_retries_instead_of_killing_the_whole_shot():
    """上传没有副作用, 抖一下就放弃等于让整条镜头白跑; 异常也不许炸穿工具。"""
    import asyncio

    media = _crew("media")
    calls = {"n": 0}

    async def _flaky(p):
        calls["n"] += 1
        if calls["n"] < 3:
            raise __import__("httpx").ConnectError("boom")
        return "http://asset/x.png", ""

    _saved_once = media._upload_once
    media._upload_once = _flaky

    async def _go():
        return await media._upload_blob(__import__("pathlib").Path("/tmp/x.png"))

    url, why = asyncio.run(_go())
    assert url == "http://asset/x.png", f"重发之后该成功, 却是 {url!r} / {why!r}"
    assert calls["n"] == 3, f"没重发够: {calls['n']} 次"

    # 一直失败时: 要有原因, 不能是空串 (空原因让模型只能瞎试)
    async def _always(p):
        raise __import__("httpx").ConnectError("still boom")

    media._upload_once = _always
    try:
        url, why = asyncio.run(_go())
        assert url == ""
        assert "ConnectError" in why and "重发" in why, f"失败原因不成话: {why!r}"
    finally:
        media._upload_once = _saved_once


def test_a_submit_is_never_sent_twice():
    """下单**绝不重发**, 异常也不许炸穿工具。

    这是比幂等键更靠前的一道闸: 幂等键要求下游真的实现了幂等 (这个假设栽过),
    "根本不重发"不依赖任何人。而异常炸穿工具等于变相重发 —— 模型看到"工具出错"
    的本能就是对同一个镜头再调一次, 效果和自动重试一模一样。
    """
    import asyncio
    import pathlib as _pl
    import tempfile

    import httpx as _httpx

    filmdir = _crew("filmdir")
    media = _crew("media")
    saved = (filmdir.ROOT, media._client, media.VIDEO_POLL_INTERVAL_S)
    filmdir.ROOT = _pl.Path(tempfile.mkdtemp(prefix="order-test-"))
    media.GATEWAY_BASE = "http://gw/v1"
    media.GATEWAY_TOKEN = "t"
    media.VIDEO_MODEL = "wan3.0-video"
    media.VIDEO_POLL_INTERVAL_S = 0

    posts = {"n": 0}

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            posts["n"] += 1
            # 下单途中连接断掉 —— 上游很可能**已经收下**
            raise _httpx.ReadError("connection reset mid-flight")

    media._client = lambda *a, **k: _C()

    try:
        body, summary = asyncio.run(media.generate_video("镜头", "片段/X.mp4", 10, "720p"))
        assert posts["n"] == 1, f"下单发了 {posts['n']} 次 —— 每多一次就是多扣一次钱"
        assert "不要直接重下单" in body, "没拦住重下单, 模型下一步就是再付一次"
        assert "videos/jobs" in body, "没给自查的路 = 只剩重下单这一条路"
        assert "可能已计费" in summary, f"摘要没提示可能已计费: {summary}"
    finally:
        filmdir.ROOT, media._client, media.VIDEO_POLL_INTERVAL_S = saved


def test_lost_submits_are_recoverable_at_all():
    """服务端必须能列出"我最近的作业" —— 否则丢了 id 的那笔钱是死账。

    2026-09-01 那条被丢掉的成片, 是人去翻数据库捞回来的; 智能体没有任何途径。
    只警告"别重下单"却不给取回的路, 等于把死胡同留给它。
    """
    src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "media.py").read_text(encoding="utf-8")
    i = src.index('@router.get("/v1/videos/jobs")')
    seg = src[i : src.index("@router.", i + 10)]
    assert "user_id = ?" in seg, "列表端点没按调用方过滤 —— 会泄漏别人的作业"
    assert "_settle" not in seg, "查询端点不该有结算副作用"


def test_a_gateway_error_carries_what_was_already_said():
    """断线前说过的话必须带出来 —— 那些字已经流到浏览器了。

    `store.add` 只在 end 事件时发生。错误路径不把 said 带出去的话, 就是
    **屏幕上有、记录里没有**: 用户看着阿剪说了一段, 续跑时这一棒等于什么都没说过。
    同一族的教训见"端到端必须包含 UI" —— 发出去 ≠ 留下来。
    """
    import asyncio

    import httpx as _httpx

    agent = _crew("agent")
    # run_turn 先查凭据再开跑, 不设的话根本走不到网关那一步
    saved = (agent._stream, agent.GATEWAY_TOKEN, agent.GATEWAY_BASE, agent.DEFAULT_MODEL)
    agent.GATEWAY_TOKEN = "t"
    agent.GATEWAY_BASE = "http://gw/v1"
    agent.DEFAULT_MODEL = "m"

    async def _half_then_die(client, model, messages):
        yield {"choices": [{"delta": {"content": "先说半句"}}]}
        raise _httpx.ReadError("peer closed connection")

    agent._stream = _half_then_die

    async def _go():
        out = []
        async for ev in agent.run_turn("editor", [{"role": "user", "content": "剪"}]):
            out.append(ev)
        return out

    try:
        evs = asyncio.run(_go())
    finally:
        agent._stream, agent.GATEWAY_TOKEN, agent.GATEWAY_BASE, agent.DEFAULT_MODEL = saved

    errs = [e for e in evs if e["type"] == "error"]
    assert errs, "网关炸了却没发 error 事件"
    assert "先说半句" in (errs[0].get("said") or ""), (
        f"断线前说过的话没带出来: {errs[0]!r} —— 屏幕上有、记录里没有"
    )


def test_the_relay_stops_on_an_error_instead_of_passing_the_baton():
    """炸了就停, 别传棒。

    `except Exception` 只接得住**抛出来**的异常, 而网关失败是 run_turn
    **yield 一个 error 事件**再正常结束 —— 于是它被原样转发, 循环若无其事地跑
    下一位。接力"有人炸了就停"的本意, 对**最常见的那种失败**一直没生效, 而
    下游会基于半截产物接着做, 全程不报错。
    """
    src = (TEAM / "app" / "main.py").read_text(encoding="utf-8")
    body = src[src.index("async def _run_relay") : src.index("async def _run_room")]
    i = body.index('elif ev["type"] == "error"')
    seg = body[i : body.index("yield ev", i)]
    assert "halted = True" in seg, "接力遇到 error 事件没停 —— 半截产物会传给下一位"
    assert "capped_here = True" in seg, "续跑没停在本棒 (它的活没干完)"
    assert "store.add" in seg, "断线前说过的话没落进记录"


# ── 文件面板 (2026-09-02 老板验收第一句: "生成的内容在哪呢") ────────────────────


def test_file_panel_is_confined_to_the_room_and_hides_internals(tmp_path):
    """一个房间只能看自己那部片; 老房间看根但看不见房间存档; 越界必须挡。

    "产物落成文件"是这个产品的立足点, 而页面上以前没有任何地方能看到文件 ——
    文件在盘上, 不在用户眼前。补面板的同时边界只有一条: 路径由用户/模型给,
    resolve 之后必须仍在房间根之下, 否则一个房间能读到别部片, 甚至读到根之外。
    """
    import pytest

    files = _crew("files")
    rooms_m = _crew("rooms")
    filmdir = _crew("filmdir")
    saved = filmdir.ROOT
    try:
        filmdir.ROOT = tmp_path
        (tmp_path / ".agents-team").mkdir()
        (tmp_path / ".agents-team" / "rooms.json").write_text("{}")
        (tmp_path / "片" / "验收片-x" / "片段").mkdir(parents=True)
        (tmp_path / "片" / "验收片-x" / "成片.mp4").write_bytes(b"x" * 10)
        (tmp_path / "片" / "验收片-x" / "片段" / "01.mp4").write_bytes(b"y")
        (tmp_path / "片" / "别的片-y").mkdir()
        (tmp_path / "片" / "别的片-y" / "秘密.txt").write_text("不该被看到")

        new = rooms_m.Room("x", "验收片", ["director"], 1.0, "relay", "片/验收片-x")
        old = rooms_m.Room("o", "旧", ["director"], 1.0, "relay", "")

        # 新房间只看自己的目录, 且视频被认出来 (前端靠 kind 决定用 <video> 放)
        d = files.listing(files.root_for(new))
        assert {e["name"] for e in d["entries"]} == {"片段", "成片.mp4"}
        assert next(e for e in d["entries"] if e["name"] == "成片.mp4")["kind"] == "video"
        sub = files.listing(files.root_for(new), "片段")
        assert sub["dir"] == "片段" and [e["name"] for e in sub["entries"]] == ["01.mp4"]

        # 老房间看根, 但 .agents-team (房间存档) 不许露出来
        d = files.listing(files.root_for(old))
        names = {e["name"] for e in d["entries"]}
        assert ".agents-team" not in names and "片" in names

        # 越界: 别部片、根之外、绝对路径, 一个都不许
        root = files.root_for(new)
        for bad in ("../别的片-y/秘密.txt", "../../.agents-team/rooms.json", "/etc/passwd"):
            with pytest.raises(ValueError):
                files.safe(root, bad)
    finally:
        filmdir.ROOT = saved


def test_file_routes_are_registered_and_use_range_capable_responses():
    """两条路由都得在, 且取文件走 FileResponse —— 视频拖进度条靠它的 Range 支持。"""
    src = (TEAM / "app" / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/api/rooms/{room_id}/files")' in src, "没有列目录的路由"
    assert '@app.get("/api/rooms/{room_id}/files/raw")' in src, "没有取文件的路由"
    i = src.index('@app.get("/api/rooms/{room_id}/files/raw")')
    seg = src[i : src.index("@app.", i + 10)]
    assert "FileResponse(" in seg, "取文件没走 FileResponse — 视频只能从头放, 拖不动"
    assert "files.safe(" in seg, "取文件没过越界检查"
    web = (TEAM / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="filesBtn"' in web and "files/raw" in web, "前端没有文件面板"


# ── 十个团队模板 (2026-09-02 老板: "不仅仅只是剧组, 咱们是多 Agent 群聊引擎") ──


def test_ten_team_templates_are_well_formed():
    """模板表必须自洽: 10 个、id/名字唯一、每个成员都在花名册、模式合法、目录互不相同。

    成员名字也不许撞 —— 「拉个群」的花名册是全体平铺的, 两个"阿图"用户分不清谁是谁。
    """
    teams = _crew("teams")
    rooms_m = _crew("rooms")
    assert len(teams.TEAMS) == 10, f"应有 10 个团队, 实际 {len(teams.TEAMS)}"
    ids = [t.id for t in teams.TEAMS]
    assert len(set(ids)) == 10
    roster = {b.id: b for b in rooms_m.BUILTIN_BOTS}
    names = [b.name for b in rooms_m.BUILTIN_BOTS]
    assert len(set(names)) == len(names), [n for n in names if names.count(n) > 1]
    for t in teams.TEAMS:
        assert t.mode in ("relay", "parallel"), t.id
        assert len(t.members) >= 3, f"{t.id} 成员太少, 不成群"
        for m in t.members:
            assert m in roster, f"{t.id} 引用了花名册里没有的成员 {m}"
        for m in t.long_run:
            assert m in t.members, f"{t.id}.long_run 里的 {m} 不是本团队成员"
        assert t.tagline and t.dir
    dirs = [t.dir for t in teams.TEAMS]
    assert len(set(dirs)) == len(dirs), "两个团队共用一个产物目录前缀"
    # 剧组还是第一个, 老入口 create_crew_room 要落到它
    assert teams.TEAMS[0].id == "film"


def test_team_rooms_carry_their_template_and_directory():
    """按模板建的群要记住出自哪个模板, 产物目录前缀跟着团队走。"""
    import tempfile

    filmdir = _crew("filmdir")
    rooms_m = _crew("rooms")
    saved = (filmdir.ROOT, rooms_m.STATE_PATH)
    try:
        filmdir.ROOT = pathlib.Path(tempfile.mkdtemp(prefix="team-room-"))
        rooms_m.STATE_PATH = filmdir.ROOT / ".agents-team" / "rooms.json"
        st = rooms_m.Store()
        r = st.create_team_room("research")
        assert r.team == "research" and r.mode == "relay"
        assert r.dir.startswith("报告/"), r.dir
        assert r.members == ["topic", "searcher", "verifier", "writer", "editor_doc"]
        f = st.create_crew_room("验收片")
        assert f.team == "film" and f.dir.startswith("片/")
        c = st.create_team_room("code_review")
        assert c.mode == "parallel" and c.dir.startswith("评审/")
        # 手工拉的群: 没有模板, 目录落在默认前缀下
        h = st.create_room("随手群", ["doer", "checker"])
        assert h.team == "" and h.dir.startswith(filmdir.FILMS + "/")
    finally:
        filmdir.ROOT, rooms_m.STATE_PATH = saved


def test_delete_room_drops_its_messages_but_keeps_files():
    """删群 = 房间 + 记录一起删; 产物文件留在磁盘 (那是用户花钱出的东西)。"""
    import tempfile

    filmdir = _crew("filmdir")
    rooms_m = _crew("rooms")
    saved = (filmdir.ROOT, rooms_m.STATE_PATH)
    try:
        filmdir.ROOT = pathlib.Path(tempfile.mkdtemp(prefix="del-room-"))
        rooms_m.STATE_PATH = filmdir.ROOT / ".agents-team" / "rooms.json"
        st = rooms_m.Store()
        r = st.create_team_room("film", "要删的片")
        keep = st.create_team_room("film", "留着的片")
        st.add(r.id, "user", "开工", [])
        st.add(keep.id, "user", "别动我", [])
        d = filmdir.ROOT / r.dir
        (d / "片段").mkdir(parents=True)
        (d / "片段" / "01.mp4").write_bytes(b"paid")
        assert st.delete_room(r.id) is True
        assert r.id not in st.rooms
        assert not any(m.room == r.id for m in st.messages), "记录没跟着删"
        assert any(m.room == keep.id for m in st.messages), "误删了别的群的记录"
        assert (d / "片段" / "01.mp4").exists(), "删群把用户花钱出的文件也删了"
        assert st.delete_room(r.id) is False, "删不存在的群应返回 False"
        # 落盘了: 重新加载不该复活
        st2 = rooms_m.Store()
        assert r.id not in st2.rooms and keep.id in st2.rooms
    finally:
        filmdir.ROOT, rooms_m.STATE_PATH = saved


def test_remove_member_keeps_relay_resume_pointer_right_and_refuses_empty_room():
    """移出成员: 接力下标要跟着修; 群里至少留一个人。

    停在第 3 棒时移出第 1 棒, 不修下标续跑就会跳过一位 —— 而且不报错。
    """
    import tempfile

    import pytest

    filmdir = _crew("filmdir")
    rooms_m = _crew("rooms")
    saved = (filmdir.ROOT, rooms_m.STATE_PATH)
    try:
        filmdir.ROOT = pathlib.Path(tempfile.mkdtemp(prefix="rm-member-"))
        rooms_m.STATE_PATH = filmdir.ROOT / ".agents-team" / "rooms.json"
        st = rooms_m.Store()
        r = st.create_team_room("research")  # topic searcher verifier writer editor_doc
        r.resume_at = 3  # 停在 writer
        st.remove_member(r.id, "searcher")  # 移出前面的一位
        assert r.members == ["topic", "verifier", "writer", "editor_doc"]
        assert r.resume_at == 2 and r.members[r.resume_at] == "writer", "续跑指针错位"
        r.resume_at = 3  # 停在最后一位 editor_doc
        st.remove_member(r.id, "editor_doc")  # 把停着的那位移出 -> 没有待续的棒
        assert r.resume_at == -1
        one = st.create_room("独角戏", ["doer"])
        with pytest.raises(ValueError):
            st.remove_member(one.id, "doer")
        # 移出不存在的人: 无事发生, 不炸
        st.remove_member(r.id, "nobody")
    finally:
        filmdir.ROOT, rooms_m.STATE_PATH = saved


def test_group_management_endpoints_and_ui_hooks_exist():
    """接口与入口都得在: 列模板 / 按模板建群 / 删群 / 移出成员; 前端有选单、删除键、chip 上的 ×。"""
    for route in (
        '@app.get("/api/teams")',
        '@app.post("/api/rooms/team")',
        '@app.delete("/api/rooms/{room_id}")',
        '@app.delete("/api/rooms/{room_id}/members/{bot_id}")',
    ):
        assert route in MAIN, f"缺端点 {route}"
    web = (TEAM / "web" / "index.html").read_text(encoding="utf-8")
    assert "method: 'DELETE'" in web, "前端没有任何删除调用"
    assert 'class="del"' in web and "删除这个群" in web, "房间项没有删除键"
    assert "x.className = 'x'" in web and "移出" in web, "成员 chip 没有移出入口"
    assert "function hintFor" in web, "底栏提示没按房间生成"
    # 假网关要能给任何成员台词, 否则十个团队里九个在本地预览里一开口就炸
    dev = (TEAM / "dev_preview.py").read_text(encoding="utf-8")
    assert "SCRIPT.get(who)" in dev


def test_every_team_ships_three_concrete_example_prompts():
    """空房间要有开场 (2026-09-02 老板: "否则打开都不知道问什么")。

    每个团队 3 条、互不相同、写得具体 —— 示例的作用是示范"问到什么程度算清楚",
    第一棒拿到就能开工, 不用回头问。太短的一句 (< 20 字) 示范不了这个。
    """
    teams = _crew("teams")
    for t in teams.TEAMS:
        assert len(t.examples) == 3, f"{t.id} 示例数 {len(t.examples)} != 3"
        assert len(set(t.examples)) == 3, f"{t.id} 示例有重复"
        for ex in t.examples:
            assert len(ex) >= 20, f"{t.id} 示例太短, 示范不了怎么问: {ex!r}"
    assert len(teams.GENERIC_EXAMPLES) >= 3, "手工群没有开场示例"
    # 接口要带出来, 前端要渲染出来
    assert '"examples": list(t.examples)' in MAIN, "/api/teams 没带 examples"
    assert '"generic_examples": list(teams.GENERIC_EXAMPLES)' in MAIN
    web = (TEAM / "web" / "index.html").read_text(encoding="utf-8")
    assert "function renderEmpty" in web and "renderEmpty(room)" in web, "空房间没有开场渲染"
    assert "className = 'ex'" in web, "示例没有做成可点的按钮"
    # 点示例是**填进输入框**而不是直接发 —— 示例里常有要改的地方 (产品名/路径/尺寸)
    # 按结构切到下一个顶层函数 (不用固定字符窗口 —— 加一行注释就失效; 也别假设
    # 文件里函数的先后顺序, msgEl 其实定义在它前面)
    import re as _re

    i = web.index("function renderEmpty")
    nxt = _re.search(r"\n(?:async )?function \w+\(", web[i + 10 :])
    seg = web[i : i + 10 + nxt.start()] if nxt else web[i:]
    assert "input.value = ex" in seg and "submit(" not in seg, "点示例直接发送了 — 用户没机会改"
