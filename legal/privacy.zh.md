<!-- 本文为草案，正式发布前请经法律专业人士审阅 -->

# DSH Cloud 隐私政策

版本 1.0，生效日期【上线日期】

> 草案说明：本文为草案，正式发布前请经法律专业人士审阅。文中【】内为占位符，发布前须逐项填写或确认。
>
> 个人信息处理者：**【运营主体】**，当前为北京跃迁效应人工智能科技有限公司（Beijing AgentsDance AI Technology Co., Ltd.）。本政策依据《中华人民共和国个人信息保护法》等法律法规制定，适用于 DSH Cloud 桌面应用、云端账号服务、LLM 网关及配套网站（合称"本服务"）。

## 1. 我们收集与处理的个人信息

我们遵循最小必要原则，仅处理下列信息：

| 类别 | 具体内容 | 处理目的 | 是否必需 |
|---|---|---|---|
| 账号信息 | 邮箱地址；口令的 scrypt 哈希值（**不存储明文密码**） | 注册、登录、身份核验、重要通知 | 必需 |
| 设备记录 | 设备名称、平台类型、最近使用时间 | 桌面端设备授权与管理、账号安全 | 使用桌面端必需 |
| 积分与用量记录 | 模型名称、token 数量（输入/缓存/输出）、积分扣减额、时间戳。**不含消息内容** | 计量计费、余额展示、对账 | 必需 |
| 订单与支付记录 | 订单号、商品项、金额、支付渠道、渠道交易号、支付时间 | 履行交易、开具凭证、退款处理 | 购买时必需 |
| 服务器日志 | IP 地址、请求元数据（接口、时间、状态码等） | 安全防护、故障排查、防滥用 | 必需 |

支付由第三方支付机构（Stripe、支付宝、微信支付）完成，**我们不收集、不接触您的银行卡号等支付账户敏感信息**。

本服务**不集成任何第三方统计分析 SDK**。

## 2. 关于消息内容的特别说明（仅传输、不存储）

2.1 您在桌面应用中发起 AI 请求时，提示词与模型回复（"消息内容"）经我们的网关**在传输过程中转发**至上游模型提供方以生成回复。

2.2 **我们承诺：不在服务端存储消息内容。**服务器日志不记录消息正文；用量记录仅包含模型名与 token 计数等元数据。

2.3 您的会话记录保存在您本人设备本地（`~/.dsh` 目录），由您自行控制与删除。

## 3. 对外提供个人信息清单

| 接收方类别 | 提供的信息 | 目的 | 说明 |
|---|---|---|---|
| 上游模型提供方（DeepSeek 官方 API 或其他 OpenAI 兼容提供方，当前名单：【上游提供方名单】） | 消息内容、必要请求元数据 | 生成模型回复 | 仅传输所必需；可能涉及跨境（见第 4 条） |
| 支付机构（Stripe / 支付宝 / 微信支付） | 订单号、金额、商品信息 | 完成支付、退款 | 支付机构作为独立个人信息处理者适用其自身隐私政策 |
| 邮件发送服务（SMTP 服务商） | 邮箱地址、验证码/通知内容 | 发送验证码与账号通知 | 仅发送所必需 |

除上述情形及法律法规规定的情形外，我们不向任何第三方提供您的个人信息。

## 4. 跨境传输披露

4.1 当您选用的模型对应的上游提供方位于**中华人民共和国境外**（例如境外 OpenAI 兼容提供方；Stripe 的处理也可能位于境外）时，您的消息内容及必要请求元数据将被传输至境外接收方，用于生成模型回复或完成支付。

4.2 境外接收方的名称/类别、联系方式、处理目的与方式、个人信息种类，以产品内模型说明页与本条为准：【境外接收方清单及联系方式】。您可通过【联系邮箱】向我们提出向境外接收方行使权利的请求。

4.3 我们将依据《个人信息保护法》第三章履行跨境提供个人信息的法定义务（包括单独告知并取得您的**单独同意**，以及法律要求的其他条件）。若您不同意跨境传输，可仅使用境内上游对应的模型。

## 5. 保存期限

| 信息 | 期限 |
|---|---|
| 账号信息、设备记录 | 账号存续期间；注销后删除或匿名化 |
| 积分与用量记录 | 账号存续期间；注销后保留法定所需的最短期限 |
| 订单与支付记录 | 自交易完成之日起不少于三年（《电子商务法》第三十一条），期满后删除或匿名化 |
| 服务器日志 | 【90】天（建议值，正式发布前确认），期满自动清除 |
| 邮箱验证码、设备激活码 | 短期有效（数分钟级），过期即失效 |
| 消息内容 | **不存储**（见第 2 条） |

## 6. 您的权利

6.1 您有权**查阅、复制**您的个人信息，**更正**不准确的信息，在法定情形下**删除**信息，以及**撤回同意**（撤回不影响撤回前基于同意已进行的处理）。

6.2 **注销**：您可在账号设置中**自助注销**。注销即时生效——账号立即停用，全部设备授权即刻吊销；您的个人信息将按第 5 条删除或匿名化。

6.3 行权路径：产品内账号设置，或发邮件至【联系邮箱】。我们将在核验身份后【15】个工作日内响应。

6.4 我们记录您同意本政策时的**政策版本号**；政策更新需重新取得同意的情形，将再次征得您的同意。

## 7. 未成年人保护

本服务仅面向年满 18 周岁的用户，不面向未成年人提供。若发现未成年人注册使用，我们将注销相关账号并删除其个人信息。

## 8. 安全措施

- 全链路 **TLS** 传输加密；
- 口令仅以 **scrypt** 哈希形式存储，不存明文；设备令牌哈希落库；
- 上游 API 密钥仅存于服务端环境，不出现在客户端、响应或日志中；
- **日志最小化**：不记录消息正文，日志限期保存；
- 访问控制与登录防爆破（登录失败次数限制、验证码发送限额）。

发生个人信息安全事件时，我们将依法履行告知与报告义务。

## 9. 政策更新

本政策更新时，我们将通过网站公告或注册邮箱通知，并更新版本号与生效日期；涉及处理目的、方式或种类的重大变更，将重新征得您的同意。

## 10. 联系我们

个人信息保护相关问题、投诉与行权请求，请联系：【联系邮箱】。
【运营主体】注册地址：【注册地址】。

---

## English Summary

This is a non-binding summary; the Chinese text above is the operative version.

DSH Cloud, operated by 【运营主体】 (currently Beijing AgentsDance AI Technology Co., Ltd.), processes: account email and an scrypt password hash (no plaintext passwords), device records (name/platform/last-seen), credit and usage records (model name, token counts, credits, timestamps — never message content), order/payment records (via Stripe/Alipay/WeChat Pay; we never touch card numbers), and retention-limited server logs (IP, request metadata). Prompt/completion content is proxied in transit to upstream model providers to generate responses and is **not persisted by us**; chat sessions live locally in `~/.dsh` on the user's device. Third-party recipients: upstream model providers (message content; cross-border transfer possible when the provider is overseas — separate consent obtained per PIPL), payment providers (order info), and an SMTP provider (verification codes). No analytics SDKs. Users must be 18+. Users can access, correct, delete their data, withdraw consent, and self-service delete their account with immediate deactivation and device revocation. Consent is recorded together with the policy version. Contact: 【联系邮箱】.
