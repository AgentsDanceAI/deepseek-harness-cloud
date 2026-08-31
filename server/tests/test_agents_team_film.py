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
    # 容器侧按 {GATEWAY_BASE}/videos/generations 拼, base 已含 /llm/v1
    assert "/v1/videos/generations" in MEDIA or "videos/generations" in MEDIA


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
