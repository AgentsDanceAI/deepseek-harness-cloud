# DSH Cloud — Third-Party Notices / 第三方声明

版本 1.0，生效日期 2026-08-21 / Version 1.0, effective 2026-08-21

DSH Cloud 桌面应用由 **AgentsDance AI（北京灵舞人工智能科技有限公司 / Beijing AgentsDance AI Technology Co., Ltd.）** 基于下列 MIT 许可的开源项目构建并再分发。我们按许可要求完整保留其版权与许可声明。

The DSH Cloud desktop application is built upon and redistributes the following MIT-licensed open-source projects. Their copyright and permission notices are reproduced in full below, as required by the license.

## 1. Open Source Notices / 开源软件声明

### 1.1 deepseek-harness

> MIT License
>
> Copyright (c) 2026 DeepSeek
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

### 1.2 deepseek-harness-desktop

> MIT License
>
> Copyright (c) 2026 Anywhere Labs
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## 2. Trademark Notice / 商标声明

"DeepSeek"及相关标识为其权利人的商标，上述 MIT 开源许可**不包含任何商标授权**。DSH Cloud 由 AgentsDance AI 独立开发与运营，**与 DeepSeek 不存在隶属、合作、赞助或背书关系**；本产品名称与品牌中对开源项目名的提及仅用于说明来源（nominative use）。

"DeepSeek" and related marks are trademarks of their respective owner. The MIT licenses above grant **no trademark rights**. DSH Cloud is independently developed and operated by AgentsDance AI / Beijing AgentsDance AI Technology Co., Ltd. and is **not affiliated with, sponsored by, or endorsed by DeepSeek**. References to the upstream project names are made solely to describe the software's origin (nominative use).

## 3. Excluded Component / 再分发剔除组件说明

本产品再分发的桌面构建**不包含** `@deepseek-ai/dsh-subagent-claude-code` 包。该包内嵌 `@anthropic-ai/claude-agent-sdk`，其分发授权以主体身份为限、仅授予 DeepSeek 自身，不延伸至本产品的再分发；因此我们在装配与打包流程中将其剔除，并在打包校验中兜底确认。

The redistributed desktop build of this product **excludes** the `@deepseek-ai/dsh-subagent-claude-code` package. That package embeds `@anthropic-ai/claude-agent-sdk`, whose distribution authorization is identity-scoped to DeepSeek and does not extend to our redistribution; it is therefore removed during assembly and verified absent at packaging time.

---

## English Summary

DSH Cloud is a commercial build derived from two MIT-licensed projects — deepseek-harness (Copyright (c) 2026 DeepSeek) and deepseek-harness-desktop (Copyright (c) 2026 Anywhere Labs) — whose full MIT notices are retained above. "DeepSeek" is a third-party trademark not licensed to us; DSH Cloud is independent and not affiliated with or endorsed by DeepSeek. The redistributed build excludes `@deepseek-ai/dsh-subagent-claude-code` because its embedded `@anthropic-ai/claude-agent-sdk` distribution authorization is identity-scoped to DeepSeek and does not extend to us.
