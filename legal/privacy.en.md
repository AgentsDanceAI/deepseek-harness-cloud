# Privacy Policy

**Version 1.0 · Effective date: August 17, 2026**

This Policy explains how deepseek-harness-cloud (the site at https://dshcloud.online , hereinafter "this service") collects, uses, shares, and protects your personal information.

---

## 1. Data Controller

| Item | Details |
|---|---|
| Legal entity | 北京跃迁效应人工智能科技有限公司 (Beijing AgentsDance AI Technology Co., Ltd.) |
| Unified Social Credit Code | 91110108MAKL3PHR6X |
| Registered address | Room 01-1922, 1F, Building 15, East Zone, Yard 10 Xibeiwang East Road, Haidian District, Beijing, China<br>中国北京市海淀区西北旺东路 10 号院东区 15 号楼 1 层 01-1922 |
| Privacy contact email | legal@agentsdance.ai |
| General support | support@agentsdance.ai |
| Security incident reports | security@agentsdance.ai |

## 2. What Information We Collect

### 2.1 What You Provide to Us

| Category | Details |
|---|---|
| Account identifiers | Email address; if you use third-party sign-in, the account identifier and email address returned by Google or GitHub |
| Authentication credentials | Password (stored as a salted scrypt hash — **we cannot recover the plaintext**), email verification codes (stored hashed) |
| Profile | Display name |
| Your content | The instructions you submit to the agent, the files you upload or generate, and session records |
| Communications | The contents of emails you send to the addresses above |

### 2.2 What Is Generated Automatically When You Use the Service

| Category | Details |
|---|---|
| Access logs | IP address, request path, HTTP status code, timestamp, User-Agent |
| Approximate geographic location | Country/region inferred from the IP address (provided by the CDN), **not precise location** |
| Usage records | Token counts for model calls, credits deducted, and active minutes on the cloud workspace |
| Device records | Device authorization records for the desktop client (device name, platform, last active time) |
| Rate-limit counters | Counters used to prevent abuse |

### 2.3 What We Expressly Do Not Collect

Precise geographic location, contact lists, biometric information, advertising identifiers, and cross-site tracking data. **We do not serve advertising, and the site does not use third-party advertising or social tracking scripts.**

### 2.4 Payment Information

**Your bank card information is collected and processed directly by Waffo Pancake, in accordance with the PCI-DSS standard, and is not transmitted to or stored on our servers.** We receive from them only the order number, amount, currency, payment status, and the last four digits of the card number, for reconciliation and invoicing.

## 3. How We Use This Information

| Purpose | Data involved | Legal basis (GDPR terms) |
|---|---|---|
| Providing and maintaining the service | Account, your content, usage | Performance of a contract |
| Metering and billing | Usage, payment status | Performance of a contract |
| Abuse prevention and security | Access logs, rate-limit counters, device records | Legitimate interests |
| Troubleshooting | Access logs, error stack traces | Legitimate interests |
| Service notifications (verification codes, invoices, material changes) | Email address | Performance of a contract |
| Complying with legal obligations (such as tax retention) | Payment records | Legal obligation |

**We do not use your content to train our own models**, unless you separately give explicit consent for specific content.

## 4. Who We Share With

**We do not sell your personal information, and we do not share your personal information for advertising purposes.**

To operate the service, the following categories of service providers process the corresponding data:

| Service provider | Data processed | Location |
|---|---|---|
| Waffo Pancake (Merchant of Record and payment processor) | Payment and tax information | See its privacy policy |
| Qianmian AI gateway (api.qianmian.ai) | The input you submit to models and the model output | Singapore |
| Upstream model providers (via the gateway above): Anthropic, OpenAI, Google, DeepSeek, Alibaba, Moonshot AI, Zhipu AI, MiniMax, ByteDance, Xiaomi, xAI | The input you submit to models and the model output | Their respective locations |
| Zhipu AI (web search) | Your search queries | China |
| Resend (email delivery) | Recipient email address, message body | United States |
| Cloudflare (CDN, R2 object storage, DNS) | Access logs, installer distribution | Global edge nodes |

We have data processing terms in place with each of the parties above, and we share data only to the extent necessary to provide the service.

In addition, we may disclose information in the following circumstances: pursuant to binding legal process; to protect the rights and safety of this service, our users, or the public; and in the event of a merger, acquisition, or transfer of assets (we will give advance notice and ensure that the transferee assumes equivalent obligations).

## 5. Cross-Border Transfers

Our servers are located in **Singapore**, our operating entity is located in **China**, and the service providers listed above are spread across multiple jurisdictions. Your data is therefore transferred across borders.

Safeguards: we sign contractual terms containing data protection obligations with each recipient; transmission uses TLS encryption throughout; and the scope of data visible to each service provider is limited according to the principle of minimum necessity. If you are located in the European Economic Area, the United Kingdom, or Switzerland, you may request a description of the applicable transfer mechanism from legal@agentsdance.ai.

## 6. Retention Periods

| Data type | Retention period |
|---|---|
| Account identifiers and profile (email address, display name, password hash) | Erased **immediately** upon account closure |
| Device authorization records, sign-in verification codes, organization memberships | Deleted **immediately** upon account closure |
| Email verification codes (where the account has not been closed) | Expire and are cleared after 10 minutes |
| Rate-limit counters | Up to 24 hours |
| Access logs | Rolling retention, capped at 100 MB (roughly one quarter at current traffic); once the cap is exceeded, the oldest portion is automatically overwritten |
| Usage and deduction records | 5 years (needed for reconciliation and tax). After account closure, these records retain only the user ID and contain none of your contact details |
| Payment records | At least 5 years under tax regulations (retained separately by Waffo Pancake and by us) |

Once these periods expire, the data is deleted or irreversibly anonymized.

**What account closure actually does**: when you close your account in the console, we immediately erase your email address, display name, and password hash, delete all device authorizations, sign-in verification codes, and organization memberships, and invalidate all sessions immediately. Deduction and payment records that we are legally required to retain are kept, but they no longer contain contact details that identify you.

## 7. Your Rights

Wherever you are located, you may exercise the following rights: **access, correction, deletion, export (portability), restriction of processing, objection to processing, and withdrawal of consent**.

- Most of these can be done directly in the console (editing your profile, revoking device authorizations, closing your account);
- For anything else, please send an email to legal@agentsdance.ai;
- **We respond within 30 calendar days of receiving a request**; if a case is complex and requires an extension, we will explain the reason within that period.

If you are located in the European Economic Area or the United Kingdom, you have the right to lodge a complaint with your local supervisory authority. If you are a California resident, you have the rights to know, delete, correct, and opt out of sale/sharing under the CCPA/CPRA — as stated above, **we do not sell personal information and do not share it for targeted advertising**. Exercising your rights will not result in discriminatory treatment.

## 8. Security

TLS throughout the transport layer; passwords stored as salted scrypt hashes; session tokens signed and revocable at any time; the cloud workspace gives each user an isolated container with memory, CPU, and process-count limits and network isolation; model providers' keys are held only on the server side and **are never sent down to the client under any circumstances**.

Even so, no system can guarantee absolute security. **If a data breach occurs that may endanger your rights and interests, we will notify affected users and the applicable supervisory authorities within 72 hours of becoming aware of it.**

## 9. Minors

This service is intended for users **aged 18 and over**. We do not knowingly collect personal information from anyone under 18. If you believe we have collected such information in error, please contact legal@agentsdance.ai and we will delete it.

## 10. Cookies and Similar Technologies

We use only **necessary** cookies: session cookies (to keep you signed in) and security cookies (to prevent cross-site request forgery). **We do not use advertising cookies, do not carry out cross-site tracking, and do not integrate third-party analytics scripts.** The site therefore has no cookie consent banner — there are no non-essential cookies for which consent would be required.

## 11. Marketing Emails

By default we send only **service emails** (verification codes, invoices, security alerts, material changes to terms). These emails relate to account functionality and cannot be unsubscribed from. If we send product marketing emails in the future, we will seek consent for them separately and provide a one-click unsubscribe link in every message; unsubscribing does not affect the delivery of service emails.

## 12. Changes to This Policy

If this Policy changes materially, we will notify you **at least 15 days before the change takes effect**, through an in-site announcement and to your registered email address. Non-material wording revisions will be made directly on this page, with the date below updated.

## 13. Contact Us

Privacy matters: legal@agentsdance.ai
General support: support@agentsdance.ai
Security vulnerabilities: security@agentsdance.ai

All of the above addresses are handled by real people, with a first reply within 24 hours on business days.

---

*Last updated: August 17, 2026 · Version 1.0*
