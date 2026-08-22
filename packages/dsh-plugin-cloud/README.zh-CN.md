# dsh-plugin-cloud

[English](./README.md)

把**原版 DeepSeek Harness** 接入 [DSH Cloud](https://dshcloud.online)：
设备授权登录 + 一个网关 provider，**20 个模型**走同一入口——任何客户端都不需要
你自己的上游 API Key。

> **坦白说明：** DSH Cloud 是 AgentsDance AI 的商业托管服务。整套平台可免费自部署——
> [deepseek-harness-cloud](https://github.com/AgentsDanceAI/deepseek-harness-cloud)。
> 托管版**注册即送 500 积分**。本插件也可通过 `DSH_CLOUD_BASE` 指向你的自部署实例。

## 一条命令接入

```bash
npx --yes dsh-plugin-cloud setup
```

它会打开浏览器做设备授权（RFC 8628 风格，与 DSH Cloud 桌面端同一条流程），拉取
实时模型目录，并向 `$DSH_HOME/cordis.patch.yml`（上游的用户自有配置层）写入两行：

- `dsh-plugin-cloud` —— 运行时插件（把你的 token 导出为 `DSH_CLOUD_TOKEN`）
- `dsh-cloud-models` —— 一个独立的 `@deepseek-ai/dsh-llm-pi-ai` 实例，挂载网关
  provider。**绝不触碰你自己的 `llm-pi-ai` 行。**

重启 DeepSeek Harness，在模型列表的 **DSH Cloud** 组里选模型即可（例如带 1M
上下文窗口的 `deepseek-v4-pro`）。随时重跑 `setup` 刷新目录；文件已存在时会先备份。

也可以走 bundle 机制注册插件、单独登录：

```bash
dsh add dsh-plugin-cloud
npx --yes dsh-plugin-cloud login
```

## 命令

| 命令 | 作用 |
|---|---|
| `setup` | 按需登录 + 拉目录 + 写配置行 |
| `login` | 仅设备登录 |
| `status` | 显示会话、服务地址与目标路径 |
| `logout` | 删除本地存储的 token |

## 安全说明

- Token 存于 `$DSH_HOME/dsh-cloud-auth.json`（权限 600），以 `DSH_CLOUD_TOKEN`
  导出——这个名字匹配上游的敏感环境变量模式，dsh 会把它从所有子进程
  （bash 工具、MCP 服务器）中剥除。
- 可随时在 DSH Cloud 控制台吊销设备，吊销即杀死 token。
- 本包只改写自己拥有的两行（`dsh-plugin-cloud`、`dsh-cloud-models`），写入前
  必备份你的 patch 文件。

## 卸载

```bash
npx --yes dsh-plugin-cloud logout
```

然后从 `$DSH_HOME/cordis.patch.yml` 删除上述两行（或恢复 `.bak-*` 备份）。

## 许可

本连接器采用 Apache-2.0。DSH Cloud 平台本身的许可见
[仓库](https://github.com/AgentsDanceAI/deepseek-harness-cloud)。
