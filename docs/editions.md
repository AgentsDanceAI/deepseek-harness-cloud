# DSH Cloud editions

English | [简体中文](editions.zh-CN.md)

The Community Edition is source-available under the
[DSH Cloud Community License 1.0](../LICENSE). DSH Cloud Hosted is the operated
subscription at `dshcloud.online`. Commercial authorization is available for
uses outside the Community License.

## Comparison

| Area | Community Edition | DSH Cloud Hosted | Commercial authorization |
|---|---|---|---|
| Use | Single-organization internal use, self-hosting, development, evaluation, and API integrations | Managed service for individual and team subscribers | Managed multi-tenant services, rebranding of official frontends, or negotiated rights |
| Start | Docker/Compose, npm/npx, or uv/uvx | [Open a managed workspace](https://dshcloud.online/login?next=%2Fwork) | Contact `support@agentsdance.ai` |
| License | DSH Cloud Community License 1.0 | Hosted terms govern the service | A signed commercial agreement |
| Models | Operator chooses and pays the upstream | Managed model access and plan allowances | Provider and governance integrations may be scoped |
| Operations | Operator owns TLS, identity, email, data, backups, monitoring, and upgrades | Managed capacity, upgrades, backups, monitoring, billing operations, and incident response | Scope and service commitments are contractual |
| Workspaces | Optional and off by default; operator reviews the trust model | Managed workspace capacity and storage | Private networking, HA, or compliance options may be scoped |
| Billing | Optional provider integrations controlled by the operator | Monthly or annual prepaid term; no automatic renewal | Contract-specific |

- [Individual Hosted plans](https://dshcloud.online/pricing#plans)
- [Team Hosted plans](https://dshcloud.online/pricing#team)
- [Support routes](../SUPPORT.md)
- [License guide](../LICENSING.md)

“Commercial authorization” describes rights available by signed agreement; it
does not promise a particular feature, SLA, certification, or package.

The public Community baseline is recorded at commit
`945439f346a291722f8f0883e3c3c789d3c0463c` in
[`legal/licensing/COMMUNITY_BASELINE.manifest`](../legal/licensing/COMMUNITY_BASELINE.manifest).
That record is provenance evidence; the current rights and restrictions are
defined by the active license.

## Contribution boundary

Changes should keep Community Edition complete, secure, buildable, testable,
self-hostable, upgradeable, and able to export its data. Pull requests that alter
license boundaries, notices, contribution terms, schemas, migrations, or public
extension interfaces require explicit maintainer review.
