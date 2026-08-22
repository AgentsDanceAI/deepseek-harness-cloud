# 贡献 DSH Cloud

[English](CONTRIBUTING.md) | 简体中文

感谢改进 DSH Cloud。小而聚焦、带测试且来源清晰的 Pull Request 最容易审查。
参与即表示同意遵守 [行为准则](CODE_OF_CONDUCT.zh-CN.md)。安全漏洞不得提交公开
Issue，请遵循 [安全政策](SECURITY.zh-CN.md)。

## 提交变更前

1. 搜索现有 Issue 和 Pull Request；
2. 需要设计共识时使用缺陷或功能模板；
3. 不要在同一 PR 中混入无关重构；
4. 涉及版本边界时先阅读 [版本说明](docs/editions.zh-CN.md)。

## 本地开发与检查

服务器需要 Python 3.11+。推荐使用 `uv`：

```bash
uv sync --project server --extra dev
DHC_DEV=1 AUTH_SECRET=local-development-secret \
  uv run --project server uvicorn app.main:app --app-dir server --reload
uv run --project server pytest server/tests -q
uv run --project server ruff check server/app server/tests
node --test server/tests/js/*.test.mjs
git diff --check
```

只使用一次性本地数据和占位凭证，禁止把生产环境文件、数据库、支持包或用户记录
复制到测试或 Issue。PR 中应记录实际执行的命令和结果。

## 身份、DCO 与来源

确认 Git 使用你愿意公开的身份，并以 `git commit -s` 添加 DCO
`Signed-off-by`。该声明不覆盖 DSH Cloud Community License，也不转让商标或自行
授权许可证变更。

PR 必须披露复制、改编、生成或第三方材料的来源 URL、准确版本、许可证、生成方式
和必要声明。贡献者对来源、正确性、安全性与许可兼容性负责。

保持 API 和存储数据兼容，行为变更应配测试和文档；说明数据库、配置、部署、安全
及回滚影响。不得在缺少单独明确授权时变更公开许可证、生产服务、Registry 或公开
Git 历史。
