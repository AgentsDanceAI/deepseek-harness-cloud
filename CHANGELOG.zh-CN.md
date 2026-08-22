# 变更日志

[English](CHANGELOG.md) | 简体中文

本项目的重要变更记录在此。格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，正式发行记录只来自
已验证的 Git tag 和发行元数据。

## [未发布]

### 新增

### 变更

### 修复

### 安全

## [0.2.0] - 2026-08-21

### 新增

- 为标准自部署栈提供版本锁定的 npm/npx 与 Python/uv 生命周期安装器。
- 增加源码构建、PostgreSQL、工作区、存活/就绪和发行元数据契约。
- 为维护中的文档提供英文与简体中文入口。

### 变更

- 采用 DSH Cloud Community License 1.0，并统一 npm、Python 和 OCI 元数据。
- 加固账号、webhook、请求体、流式计费、预览、容器和部署边界。
- 在服务端、桌面装配、CLI 和镜像中统一版本 `0.2.0`。

### 修复

- 修复自部署版本漂移、旧数据卷升级、PostgreSQL 18 存储、首次建号和 CLI 状态管理。
- 使 Python wheel 和源码归档可复现并满足发布要求。

[0.2.0]: https://github.com/AgentsDanceAI/deepseek-harness-cloud/releases/tag/v0.2.0
