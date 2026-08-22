# DSH Cloud 版本说明

[English](editions.md) | 简体中文

Community Edition 以 [DSH Cloud Community License 1.0](../LICENSE) 提供源码。
DSH Cloud Hosted 是在 `dshcloud.online` 运营的托管订阅服务。超出 Community
License 的使用方式可申请商业授权。

## 对比

| 范围 | Community Edition | DSH Cloud Hosted | 商业授权 |
|---|---|---|---|
| 使用方式 | 单一组织内部使用、自部署、开发、评估和 API 集成 | 面向个人及团队订阅者的托管服务 | 托管多租户服务、官方前端去品牌或协商的其他权利 |
| 开始使用 | Docker/Compose、npm/npx 或 uv/uvx | [打开托管工作台](https://dshcloud.online/login?next=%2Fwork) | 联系 `support@agentsdance.ai` |
| 许可 | DSH Cloud Community License 1.0 | 服务由 Hosted 条款约束 | 已签署的商业协议 |
| 模型 | 运营方选择并承担上游成本 | 托管模型访问和套餐额度 | 可约定提供方及治理集成 |
| 运维 | 运营方负责 TLS、身份、邮件、数据、备份、监控和升级 | 托管容量、升级、备份、监控、计费运维和事件响应 | 服务范围与承诺以合同为准 |
| 工作台 | 可选且默认关闭，运营方需审查信任边界 | 托管工作台容量与存储 | 可约定私有网络、高可用或合规能力 |
| 计费 | 由运营方控制的可选支付集成 | 月度或年度一次性预付，到期不自动续费 | 以合同为准 |

- [个人托管套餐](https://dshcloud.online/pricing#plans)
- [团队托管套餐](https://dshcloud.online/pricing#team)
- [支持渠道](../SUPPORT.zh-CN.md)
- [许可说明](../LICENSING.zh-CN.md)

“商业授权”表示可以通过书面协议取得的权利，不代表已承诺特定功能、SLA、
认证或软件包。

公开 Community 基线记录于提交
`945439f346a291722f8f0883e3c3c789d3c0463c` 和
[`legal/licensing/COMMUNITY_BASELINE.manifest`](../legal/licensing/COMMUNITY_BASELINE.manifest)。
该记录用于来源证明；当前权利和限制以现行许可证为准。

## 贡献边界

变更应保证 Community Edition 完整、安全、可构建、可测试、可自部署、可升级，
并可导出数据。修改许可边界、声明、贡献条款、数据库结构、迁移或公开扩展接口的
Pull Request 需要维护者明确审查。
