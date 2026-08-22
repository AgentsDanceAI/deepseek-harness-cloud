# 与上游的兼容策略

[English](compatibility.md) | 简体中文

deepseek-harness（dsh）处于 developer preview，官方明言会有 breaking change；
deepseek-harness-desktop 也会跟着动。本项目的立场：**不 fork 上游一行代码**，
全部定制收敛为「4 个小补丁 + 1 个自包含目录」，升级是机械操作。

## 1. Pin 与产物

`desktop/upstream.json` 是唯一的版本事实源：

| 字段 | 含义 |
|---|---|
| `desktopCommit` | deepseek-harness-desktop 的 git pin（补丁基线） |
| `harnessCommit` | dsh 源码 pin（仅用于子模块一致性，参考） |
| `runtimePackageVersion` | `@deepseek-ai/dsh*` npm 包版本族（真正跑的代码） |

服务端工作区与桌面端分别锁版本：`release/release.json.harnessRuntime` 是
工作区镜像使用的版本，`desktopRuntime` 是 pinned 桌面上游实际支持的版本。
两者不得假装一致；升级桌面 pin 并通过装配、类型检查和测试后，才同步提升
`desktopRuntime`。

装配（`desktop/scripts/assemble.mjs`）= clone pin → `git apply` 补丁 → 拷入
`dsh-plugin-cloud` → 各种守卫检查。产出的是一棵普通的上游工作树，用上游自己
的命令构建。

## 2. 我们依赖的上游契约点（全部清单）

改动面越窄，升级越稳。我们依赖且仅依赖：

| 契约点 | 用途 | 断言位置 |
|---|---|---|
| base bundle 里的 row id `llm-deepseek` | 注入网关 baseURL + token env | verify-contract.mjs |
| `dsh-llm-deepseek` config 字段 `baseURL` / `apiKeyEnv` | 同上 | verify-contract.mjs |
| base bundle 里的 row id `web-search-deepseek` 及同名字段 | web_search 走网关（Anthropic Messages 面） | verify-contract.mjs |
| base bundle 里的 row id `session-telemetry-otel` | 钉死遥测关闭 | verify-contract.mjs |
| base bundle 里的 row id `llm-pi-ai` | 网关整份目录以 hand-declared 路由暴露 | verify-contract.mjs |
| `dsh-llm-pi-ai` config 字段 `providers` / `apiKeyEnv` / `baseURL` / `models` | 同上（hand-declared 路由要自己给全端点/协议/模型） | verify-contract.mjs |
| dsh 凭据 seam：env 源最高优先级、只读、按请求解析 | token 注入通道 | 行为契约（升级时人工确认 release note） |
| `main.ts` 的 `try { loadLayeredEnv` 与 `prepareDesktopProfile(...)` 调用点 | 0003 补丁的两个锚 | git apply --check |
| `prepareDesktopProfile` 返回的 `patches` 数组语义（后推入者胜） | 我们的 row 覆盖一切层 | 行为契约 |
| electron-builder 配置在 `dsh-plugin-desktop/package.json` `build` 字段 | 0001 品牌补丁 | git apply --check |
| `update-checker.ts` / `update-download.ts` 的 URL 常量 | 0002 更新源补丁 | git apply --check |

**明确不依赖**：desktopRuntime 内部接口、Electron 窗口/托盘细节、dsh 内部包
路径、任何未导出的符号——上游文档声明这些会无预警变更。

## 3. 升级流程（例行，建议每次上游 release 后执行）

```bash
# 1. 试装新 pin，补丁冲突会逐个报告
node desktop/scripts/bump-upstream.mjs --desktop-commit <新sha> --runtime <新版本族>

# 2. （如有冲突）在新树上重放同样的编辑并重新 git diff 生成对应补丁，重跑第 1 步

# 3. 实装 + 安装依赖 + 契约断言
node desktop/scripts/assemble.mjs
cd desktop/build/upstream && corepack enable && yarn install
node ../../scripts/verify-contract.mjs "$PWD"

# 4. 上游自己的检查 + 我们的打包
yarn check
cd dsh-plugin-desktop && yarn build && yarn package:dir

# 5. 全绿后提交 upstream.json（+ 重新生成的补丁）
```

`verify-contract.mjs` 挂掉 = 上游动了某个契约点。届时改的是
`desktop/dsh-plugin-cloud/`（自包含，无冲突可言）或重生成对应补丁，改动范围
天然被第 2 节的清单圈死。

## 4. 已知漂移风险与预案

| 风险 | 信号 | 预案 |
|---|---|---|
| patch 层是"整 row config 替换"，上游给 `llm-deepseek` row 新增默认 config 字段会被我们盖掉 | verify-contract 不报错但行为变化；升级时 diff base bundle 的该 row | 把上游新增字段并入 `cloudProfilePatches()` |
| `llm-pi-ai` 注入失效而 `llm-deepseek` 仍被禁用 | 无可用模型时输入框会被禁用 | verify-contract 断言 pi-ai 契约；运行时在目录不可用时保留 deepseek 行作为兜底 |
| 上游把 web_search 换协议/换 row | verify-contract 失败 | 网关加对应面（服务端改动，客户端不动） |
| 上游给 desktop 加自己的账号体系 | 装配后功能重叠 | 用 0003 同款手法禁掉上游 row，保留我们的 |
| `main.ts` 重构导致 0003 锚点消失 | git apply 冲突 | 重找 boot 前/prepare 后两个语义锚，重生成补丁（逻辑都在 cloud/ 里，补丁只有 8 行） |
| dsh 收紧 `apiKeyEnv` 或凭据 seam | release note / verify-contract 字段断言 | 改用 llm-pi-ai 路由（支持自定义 headers，配置驱动，已确认三协议） |

## 5. 服务端的兼容缓冲

网关是标准 OpenAI/Anthropic 兼容面 + Bearer token，对 dsh 版本零感知。
就算客户端一段时间不升级，服务端先行升级也不破坏旧客户端；反之亦然。
计费只依赖响应里的 `usage` 字段——这是 OpenAI 兼容生态最稳的字段之一。
