"""AI 智慧搜索 / AI 智慧推荐 —— 自口袋专家迁入的第二、三格 (2026-09-17)。

两格与模型中心同构: 页面住在主站上, 数据面在我们自己那台后端上, 页面通过
`/api/<格>/proxy` 代转, 凭证只在服务端。守的还是那**一道**边界:

⛔ 上游那台后端是**口袋专家的完整后端** —— 上面有写索引、删数据、以及
`/v1/chat/completions` 这类花钱的推理面。代转口一旦能透传任意路径, 这两格就成了
绕开计费与权限的后门。白名单是唯一挡得住的东西。

⚠️ 还有一条写在模块注释里的前提: 现在是**单租户**的 (一把服务端密钥, 所有访客共用
同一个上游租户的索引与账单), 所以两格都 unlisted。这个文件钉住"没上架"这件事 ——
它是上架前那道限制还在的凭据。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import apps_catalog, smart_recommend, smart_search


class TestSearchWhitelist:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/datasets"),
            ("GET", "/api/datasets/my-index/detail"),
            ("GET", "/api/fields/my-index"),
            ("GET", "/api/ai-search"),
            ("GET", "/api/suggest"),
            ("POST", "/api/search-advanced"),
        ],
    )
    def test_read_paths_pass(self, method, path):
        assert smart_search._UP.target(method, path) == path

    @pytest.mark.parametrize(
        "method,path",
        [
            ("POST", "/v1/chat/completions"),  # 花钱的推理面
            ("POST", "/api/create-dataset"),  # 建索引
            ("DELETE", "/api/datasets/my-index"),  # 删数据
            ("POST", "/api/agent/run"),  # 跑智能体 (更花钱)
            ("GET", "/api/admin/search-engines"),  # 运维面
            ("POST", "/api/datasets"),  # 同路径但方法不同
        ],
    )
    def test_everything_else_is_refused(self, method, path):
        with pytest.raises(HTTPException) as got:
            smart_search._UP.target(method, path)
        assert got.value.status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/api/datasets/../create-dataset/detail",
            "/api/datasets//detail",
            "/api/datasets/x/y/detail",  # 段数对不上
            "/api/datasets//x/detail",
        ],
    )
    def test_prefix_rule_cannot_be_widened(self, path):
        """带变量的那两条用**段数钉死**的前缀匹配 —— 松成"前缀对上就行"就等于放开整棵子树。"""
        with pytest.raises(HTTPException):
            smart_search._UP.target("GET", path)

    def test_query_string_is_carried_but_not_matched(self):
        """上游那几条自带查询串 (?query=&index=), 要原样带过去, 但判白名单只看路径。"""
        got = smart_search._UP.target("GET", "/api/ai-search?query=x&index=y")
        assert got == "/api/ai-search?query=x&index=y"

    def test_query_string_cannot_smuggle_a_path(self):
        with pytest.raises(HTTPException):
            smart_search._UP.target("GET", "/api/admin/x?path=/api/datasets")


class TestRecommendWhitelist:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/datasets"),
            ("GET", "/api/datasets/my-index/detail"),
            ("GET", "/api/recommend/random-user"),
            ("GET", "/api/recommend/random-item"),
            ("POST", "/api/recommend/feed"),
            ("POST", "/api/recommend/related"),
        ],
    )
    def test_its_own_paths_pass(self, method, path):
        assert smart_recommend._UP.target(method, path) == path

    def test_it_cannot_reach_the_search_surface(self):
        """两格各有各的白名单 —— 推荐这一格没有理由能打 ai-search。"""
        with pytest.raises(HTTPException):
            smart_recommend._UP.target("GET", "/api/ai-search")

    def test_inference_is_refused(self):
        with pytest.raises(HTTPException):
            smart_recommend._UP.target("POST", "/v1/chat/completions")


class TestCredentialsStayServerSide:
    @pytest.mark.parametrize("mod", [smart_search, smart_recommend])
    def test_key_only_in_headers(self, mod, monkeypatch):
        from app import config

        monkeypatch.setattr(config, "PE_API_KEY", "secret", raising=False)
        assert mod._UP.headers()["X-API-Key"] == "secret"
        monkeypatch.setattr(config, "PE_API_KEY", "", raising=False)
        assert "X-API-Key" not in mod._UP.headers()

    @pytest.mark.parametrize("name", ["smart_search", "smart_recommend"])
    def test_template_never_embeds_the_key(self, name):
        from pathlib import Path

        html = (Path(apps_catalog.__file__).parent / "templates" / f"{name}.html").read_text(encoding="utf-8")
        for leak in ("PE_API_KEY", "X-API-Key", "pe_api_key"):
            assert leak not in html, f"{name}.html 里出现了 {leak}"
        assert f"/api/{name.replace('_', '-')}/proxy" in html, "页面该打我们自己的代转口"


class TestNotConfiguredIsQuiet:
    @pytest.mark.parametrize("mod", [smart_search, smart_recommend])
    def test_configured_follows_the_url(self, mod, monkeypatch):
        from app import config

        monkeypatch.setattr(config, "PE_API_URL", "", raising=False)
        assert mod.configured() is False
        monkeypatch.setattr(config, "PE_API_URL", "http://pe:50001", raising=False)
        assert mod.configured() is True


class TestBothAreStillUnlisted:
    """⚠️ 上架前那条限制 (单租户) 还在, 所以这两格必须还藏着。

    这不是风格问题: 一旦摆上货架, 所有访客会共用同一个上游租户的索引与账单。
    """

    @pytest.mark.parametrize("app_id", ["smart-search", "smart-recommend"])
    def test_unlisted(self, app_id):
        entry = next(a for a in apps_catalog.CATALOG if a.id == app_id)
        assert entry.unlisted is True, "单租户那条限制还没解, 不能上架"
        assert entry.href == f"/{app_id}", "它住在主站上, 不是云工作台"

    @pytest.mark.parametrize("app_id", ["smart-search", "smart-recommend"])
    def test_not_on_the_shelf_but_routable(self, app_id):
        from fastapi.testclient import TestClient

        from app.main import app

        assert app_id not in {a.id for a in apps_catalog.listed()}
        with TestClient(app) as client:
            assert client.get(f"/{app_id}").status_code == 200, "藏的是卡片不是页面"
            assert f"/{app_id}" not in client.get("/apps").text

    @pytest.mark.parametrize("app_id", ["smart-search", "smart-recommend"])
    def test_readme_does_not_advertise_them(self, app_id):
        from pathlib import Path

        root = Path(apps_catalog.__file__).resolve().parents[2]
        for name in ("README.md", "README.zh-CN.md"):
            assert app_id not in (root / name).read_text(encoding="utf-8")


class TestStreamingIsPipedNotBuffered:
    """⚠️ 智搜的总结与智推的理由都是 SSE 逐字出的。整段读完再回 = 把流式产品做成
    转圈产品, 而且体验上分不出"在想"和"挂了"。"""

    def test_forward_returns_a_stream_not_a_buffered_body(self, monkeypatch):
        """⛔ 真调一次, 不看源码。

        上一版这条断言的是"`forward` 的源码里有没有 text/event-stream" —— 注红把那个
        分支改成 `if False:` 时它照样绿, 因为字符串还在源码里。读源码型断言必须能
        被注红打红, 打不红就换成真调用 (见 [[agentsdance-guard-tests]])。
        """
        import asyncio

        from fastapi.responses import StreamingResponse

        from app import config, upstream

        chunks = [b"data: {}\n\n", b"data: {}\n\n"]

        class _Resp:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def aiter_raw(self):
                for c in chunks:
                    yield c

            async def aclose(self):
                pass

            async def aread(self):
                return b""

        class _Client:
            def __init__(self, *a, **k):
                pass

            def build_request(self, *a, **k):
                return object()

            async def send(self, *a, **k):
                return _Resp()

            async def aclose(self):
                pass

        monkeypatch.setattr(config, "PE_API_URL", "http://pe:50001", raising=False)
        monkeypatch.setattr(upstream.httpx, "AsyncClient", _Client)

        class _Req:
            method = "GET"

            async def body(self):
                return None

        got = asyncio.run(smart_search._UP.forward(_Req(), "/api/ai-search"))
        assert isinstance(got, StreamingResponse), "SSE 被整段缓冲了, 流式产品做成了转圈"
        assert got.media_type == "text/event-stream"

        async def _drain():
            return [c async for c in got.body_iterator]

        assert asyncio.run(_drain()) == chunks, "分片没有原样透传"

    @pytest.mark.parametrize("name", ["smart_search", "smart_recommend"])
    def test_pages_read_the_stream_incrementally(self, name):
        from pathlib import Path

        html = (Path(apps_catalog.__file__).parent / "templates" / f"{name}.html").read_text(encoding="utf-8")
        assert "getReader()" in html, f"{name}.html 没有边收边显"
        assert "TextDecoder" in html
