# Terms of Service

**Version 1.2 · Effective date: August 19, 2026**

These Terms are the agreement between you and the Operator regarding deepseek-harness-cloud (the "Service", at https://dshcloud.online). By registering an account, downloading a client, or using the Service, you confirm that you have read and agree to these Terms. If you do not agree, please do not use the Service.

---

## 1. Operator and Contact Information

| Item | Details |
|---|---|
| Legal entity | AgentsDance AI |
| Service and billing support | support@agentsdance.ai |
| Legal and privacy | legal@agentsdance.ai |
| Security vulnerability reports | security@agentsdance.ai |

All of the mailboxes above are handled by real people, with a first reply within 24 hours on business days.

## 2. Service Description

Built on the open-source project DeepSeek Harness (MIT License), the Service provides a hosted account, quota, and model gateway, and consists of three parts:

1. **Cloud Harness**: a cloud agent workspace usable directly in the browser, with each user running in a dedicated container;
2. **Desktop Harness**: macOS and Windows clients, where tasks run on your own machine;
3. **Unified model gateway**: we purchase from and call upstream model providers on your behalf, so you do not need to obtain your own API key.

**The Service is operated independently by us. We have no affiliation, agency, or partnership relationship with DeepSeek (杭州深度求索人工智能基础技术研究有限公司), and the Service is not its official product.** "DeepSeek" and "DeepSeek Harness" belong to their respective rights holders.

## 3. Accounts

3.1 You must be at least 18 years old to register. If you register on behalf of a company, you represent that you have obtained the appropriate authorization from that company.

3.2 You are responsible for all activity under your account and must safeguard your login credentials and device authorization tokens. If you discover unauthorized use, notify security@agentsdance.ai immediately.

3.3 We may suspend or terminate your account if you violate these Terms or the Acceptable Use Policy in Section 8; see Section 6 for details.

## 4. Credits, Machine Hours, and Billing

4.1 **Two separate quotas.** Credits measure model calls (counted by token), and machine hours measure the **running time** of the cloud workspace — it runs continuously from when you open it until you close it, and every minute counts, whether or not the agent is executing a task. It is reclaimed only when **nobody is using it and no task is running**: about 10 minutes without interaction while the page is open, or about 3 minutes after the page is closed, provided the agent is not executing a task. Your files are kept; the workspace restarts on your next visit. The two are not convertible into each other and do not draw on each other.

4.2 **How credits are deducted.** Each model has a published multiplier, with Claude Sonnet as the 1.00x baseline; 1.00x means a deduction of 1,000 credits per 1 million tokens. The full multiplier table is published in real time at https://dshcloud.online/pricing . When a multiplier changes because of an upstream price adjustment, we will update that page before the change takes effect.

4.3 **Free quota.** Registration includes a one-time grant of credits (currently 500), plus 3 cloud machine hours per month, reset on the first of each calendar month. Free quota cannot be cashed out or transferred.

## 5. Payment, Auto-Renewal, and Cancellation

### 5.1 No Auto-Renewal

**The Service does not auto-renew, and it does not create any recurring charges.** Every payment is a **single, one-time** transaction:

- When you purchase a plan period (monthly or annual), we add the corresponding number of days of entitlements to your account;
- When the period ends, the account **automatically reverts to the Free plan**, and we will not charge you again;
- **Unless you actively place another order, we will never initiate a second charge against your payment method.**

There is therefore no "subscription cancellation" process for the Service—there is nothing to cancel. You may stop purchasing at any time.

### 5.2 Plans and Prices

The prices below are current (in US dollars). RMB prices and the latest price list are as shown at https://dshcloud.online/pricing :

| Plan | Monthly | Annual | Credits per month | Machine hours per month | Concurrency |
|---|---|---|---|---|---|
| Free | $0 | — | 0 (one-time grant of 500 at registration) | 3 hours | 1 |
| Plus | $10 | $84 ($7/month) | 1,000 | 100 hours | 2 |
| Pro | $50 | $420 ($35/month) | 5,000 | 360 hours | 5 |
| Max | $100 | $840 ($70/month) | 10,000 | 540 hours | 10 |

**Annual discount**: the annual list price is twelve months at the monthly rate, less 30% on every tier.

**First-month price**: the first monthly period of a tier you have not subscribed to before costs $8 for Plus, $38 for Pro, and $75 for Max. The standard monthly price applies from the second monthly period. The offer is available once per account per tier.

**Credit packs** (can be purchased without a plan; valid for 365 days): 1,000 credits for $10; 5,000 credits for $50; 10,000 credits for $100. Packs cost the same per credit as a plan: $1 buys 100 credits.

**Team seats**: $25 per seat per month, minimum 3 seats; each seat includes 2,500 credits and 20 machine hours, shared within the organization.

An annual plan is a single payment for the full year that grants the full year of entitlements at once, and it likewise does not renew automatically.

### 5.3 Payment Processor

All payments for the Service are processed by **Waffo Pancake**, which acts as the **Merchant of Record** and handles collection, invoicing, and taxes. Your card details are collected directly by Waffo Pancake and handled in accordance with PCI-DSS; they **are not transmitted to or stored on our servers**. Your statement may show wording related to Waffo or Waffo Pancake.

### 5.4 Prices and Taxes

**Listed prices are tax-inclusive.** Applicable VAT, sales tax, and similar taxes are already contained in the amount shown on the page and are collected and remitted by Waffo Pancake as merchant of record; nothing is added on top at checkout. A price change does not affect a period you have already purchased that has not yet expired.

## 6. Refunds

The complete refund policy is available at https://dshcloud.online/legal/refund . It is an integral part of these Terms and is also displayed at checkout. The key points are:

6.1 **No-questions-asked refund within 14 calendar days of your first purchase.** Within 14 calendar days of your first payment, you may request a full refund if no more than 20% of the credits included in that purchase have been used.

6.2 **Duplicate or erroneous charges**: refunded in full, whether or not the Service was used.

6.3 **Major service interruption**: where the Service is continuously unavailable for more than 72 hours in a single incident, we refund pro rata for the number of days affected.

6.4 **Termination because you violated these Terms**: we will refund the fees for any full period you have paid for that **has not yet started** (this service does not auto-renew, so such a period exists only if you bought a further period before the current one ended); the period currently in progress, and credits already granted but not used, are not refundable. If the termination is because you engaged in the fraud, attacks, or unlawful conduct listed in Section 8, no amount will be refunded.

6.5 Apart from the situations above, credits and machine hours that have been granted are non-refundable once used. Unused quota does not carry over to the next period and cannot be cashed out or transferred.

Send refund requests to support@agentsdance.ai; we will reply within 5 business days. Approved refunds are returned by Waffo Pancake through the original payment method, and the time to arrive depends on your card issuer.

## 7. Your Content and Limits on Our Use

7.1 All input you submit through the Service and all output generated (together, "Your Content") belongs to you.

7.2 To provide the Service, we need to transmit Your Content to upstream model providers for processing. Apart from that:

- **We will not use Your Content to train our own models**;
- **We will not sell Your Content**, nor will we use it for advertising.

7.3 You grant us the limited license (to store, transmit, and display) that is necessary to operate the Service, troubleshoot, and maintain security. That license ends when you delete the content or close your account.

7.4 Output from generative models may be inaccurate. **You are responsible for judging whether to rely on the output and for the consequences of doing so**, especially in high-risk settings such as medical, legal, and financial matters.

## 8. Acceptable Use

You agree to comply with the Acceptable Use Policy set out at https://dshcloud.online/legal/aup , which is an integral part of these Terms. Core prohibitions include: generating child sexual exploitation material; planning violence or terrorist activity; developing malware or carrying out cyberattacks; generating disinformation or spam at scale; processing other people's personal information without authorization; circumventing the Service's metering, quota, or security mechanisms; and reselling or redistributing the Service's model access.

## 9. Service Availability

9.1 We make reasonable efforts to keep the Service available, but **we do not commit to any specific availability rate**. Planned maintenance will be announced in advance.

9.2 A cloud workspace is reclaimed after about 10 minutes of idle time so that it stops consuming your machine hours. Your files and sessions live on separate storage and are not lost when this happens; your next visit creates a new workspace, which usually takes 20–40 seconds.

9.3 We may change, restrict, or discontinue a feature of the Service. For a materially adverse change, we will give at least 30 days' advance notice; if you do not accept it, you may request a pro rata refund for any period that has not yet started.

## 10. Open Source and Self-Hosting

The server-side code of the Service is released under an open-source license. You may deploy it yourself, but a self-hosted instance is your own responsibility, and these Terms apply only to https://dshcloud.online as operated by us. The open-source license does not grant you the right to use our brand, account system, or model quota.

## 11. Limitation of Liability

To the maximum extent permitted by applicable law:

11.1 The Service is provided "as is", and we make no warranties of any kind, express or implied, including merchantability, fitness for a particular purpose, and non-infringement.

11.2 We are not liable for indirect, incidental, or punitive damages, or for loss of profits, data, or goodwill.

11.3 Our aggregate liability under these Terms is limited to **the total fees you actually paid in the 12 months before the event giving rise to the claim**.

11.4 The limitations above do not exclude liability that cannot be excluded by law (including personal injury caused by intent or gross negligence).

## 12. Changes to These Terms

We may revise these Terms. **We will give notice of material changes at least 15 days before they take effect, by in-product announcement and to your registered email address.** Continued use after a change takes effect is treated as acceptance; if you do not accept it, you may stop using the Service and request a refund for any period that has not yet started.

## 13. Termination

13.1 You may close your account at any time from the console. Closing your account deletes your account data, and **any unused portion of a paid period is not compensated** (except as otherwise provided in Section 6.4).

13.2 We may terminate the Service if you seriously violate these Terms, and will handle refunds under Section 6.4. Except in urgent situations (such as an attack or unlawful conduct in progress), we will notify you first and give you a reasonable opportunity to correct the issue.

## 14. Governing Law and Dispute Resolution

These Terms are governed by the laws of the People's Republic of China (excluding its conflict-of-laws rules). For disputes arising from these Terms, the parties shall first seek an amicable resolution; if that fails, the dispute shall be submitted to the competent People's Court at the Operator's place of registration. The foregoing does not affect the rights you enjoy as a consumer under the mandatory laws of your place of residence.

## 15. Miscellaneous

15.1 These Terms, together with the Privacy Policy, the Refund Policy, and the Acceptable Use Policy, constitute the entire agreement.

15.2 If any provision is invalid, the remaining provisions remain in effect.

15.3 Failure to exercise a right does not constitute a waiver of it.

15.4 You may not assign your rights or obligations under these Terms without our written consent.

---

*Last updated: August 19, 2026 · Version 1.2 (this revision changes only the price table in section 5.2. Pro moves from $20 to $50 per month and its monthly credits from 2,000 to 5,000; the other tiers are unchanged. Per section 5.2, a price change does not affect a period you have already purchased that has not yet expired.)*
