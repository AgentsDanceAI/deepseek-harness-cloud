"""模型中心 —— 自口袋专家迁入的第一格 (2026-09-17)。

那边的控制台有三页直接在浏览器里打推理节点的 `/v1/*`；这里不这么做, 原因在
`app/models_hub.py` 的模块注释里。本文件守的是那条**唯一的安全边界**:

⛔ 页面只能通过 `/api/models-hub/proxy` 打**白名单里的看板与装卸路径**。推理节点上
还有 `/v1/chat/completions`、`/v1/embeddings` 这些真花钱的推理面 —— 一旦能透传任意
路径, 这一格就成了绕开积分计费的免费推理入口。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import apps_catalog, models_hub


class TestOnlyTheBoardPathsGetThrough:
    """白名单是这一页唯一挡得住事的东西。"""

    @pytest.mark.parametrize("method,path", [
        ("GET", "/v1/engines"),
        ("GET", "/v1/gpu"),
        ("GET", "/v1/models"),
        ("GET", "/v1/models/base"),
        ("POST", "/v1/models/register"),
        ("POST", "/v1/models/unregister"),
        ("POST", "/v1/models/vllm-a/load"),
        ("POST", "/v1/models/vllm-a/unload"),
    ])
    def test_board_and_load_paths_pass(self, method, path):
        assert models_hub.allowed_target(method, path) == path

    @pytest.mark.parametrize("path", [
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/rerank",
        "/v1/responses",
    ])
    def test_inference_surfaces_are_refused(self, path):
        """⛔ 这几条是花钱的推理面。放过去 = 绕开积分计费的免费入口。"""
        with pytest.raises(HTTPException) as got:
            models_hub.allowed_target("POST", path)
        assert got.value.status_code == 404

    @pytest.mark.parametrize("path", [
        "/v1/models/../chat/completions",
        "/v1/models/..%2F..%2Fadmin",
        "//v1/chat/completions",
        "/v1/models/x/load/../../chat/completions",
    ])
    def test_traversal_cannot_escape_the_whitelist(self, path):
        """⚠️ 这几条**本来就被白名单挡住**(段数/表里没有), `..` 那两行只是冗余加固。
        所以注红删掉那两行时这条不会变红 —— 那是对的, 真正的防线是上面那组。"""
        with pytest.raises(HTTPException):
            models_hub.allowed_target("POST", path)

    def test_engine_action_must_be_load_or_unload(self):
        """引擎名是用户给的, 不能用前缀匹配放行整个 /v1/models/<任意>/<任意>。"""
        with pytest.raises(HTTPException):
            models_hub.allowed_target("POST", "/v1/models/x/delete_everything")

    def test_method_matters(self):
        """看板那几条只读; 用 POST 打过去不该被当成合法目标。"""
        with pytest.raises(HTTPException):
            models_hub.allowed_target("POST", "/v1/engines")

    def test_unknown_paths_are_refused(self):
        for bad in ("", "/", "/v1", "/admin", "/v1/models/base/extra"):
            with pytest.raises(HTTPException):
                models_hub.allowed_target("GET", bad)


class TestCredentialsStayServerSide:
    """节点凭证一个字节都不下发 —— 与 gateway.py 的上游 key 同一条纪律。"""

    def test_key_is_only_in_request_headers(self, monkeypatch):
        monkeypatch.setattr(models_hub.config, "INFERENCE_KEY", "secret-key", raising=False)
        assert models_hub._headers()["X-API-Key"] == "secret-key"

    def test_no_key_configured_sends_no_header(self, monkeypatch):
        monkeypatch.setattr(models_hub.config, "INFERENCE_KEY", "", raising=False)
        assert "X-API-Key" not in models_hub._headers()

    def test_page_never_embeds_the_key(self):
        """模板里不许出现凭证相关的变量 —— 页面打的是我们自己的 /api/models-hub/*。"""
        from pathlib import Path

        html = (Path(models_hub.__file__).parent / "templates" / "models_hub.html").read_text(
            encoding="utf-8")
        for leak in ("INFERENCE_KEY", "X-API-Key", "inference_key"):
            assert leak not in html, f"模板里出现了 {leak}"
        assert "/api/models-hub/" in html, "页面该打我们自己的代转口"
        assert "/v1/" in html and "http" not in html.split("fetch(")[1][:60], \
            "页面不该直连推理节点"


class TestNotConfiguredIsQuiet:
    """没接推理节点时: 卡片仍在, 点进去说人话, 不是 500 也不是空白。"""

    def test_configured_follows_the_url(self, monkeypatch):
        monkeypatch.setattr(models_hub.config, "INFERENCE_URL", "", raising=False)
        assert models_hub.configured() is False
        monkeypatch.setattr(models_hub.config, "INFERENCE_URL", "http://node:50001",
                            raising=False)
        assert models_hub.configured() is True

    def test_trailing_slash_is_normalised(self, monkeypatch):
        monkeypatch.setattr(models_hub.config, "INFERENCE_URL", "http://node:50001/",
                            raising=False)
        assert models_hub._base_url() == "http://node:50001"


class TestItIsOnTheShelf:
    def test_card_exists_and_lives_on_this_site(self):
        entry = next(a for a in apps_catalog.CATALOG if a.id == "models-hub")
        assert entry.href == "/models-hub", "它住在主站上, 不是云工作台"
        assert entry.name_key, "我们自己那几格的名字要跟着语言走"

    def test_card_is_categorised(self):
        placed = {i for _key, ids in apps_catalog.CATEGORIES for i in ids}
        assert "models-hub" in placed, "没归类会掉进无标题的兜底组"
