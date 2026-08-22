# DSH Cloud editions

This document separates a software license from a deployment and support model.
The repository is currently **AGPL-3.0-only**. DSH Cloud Hosted is an operated
service, not a different retroactive license for the public code. Any future
source-available license would be prospective and remains blocked on counsel and
copyright-chain approval; see [LICENSING.md](../LICENSING.md).

## Comparison

| Area | Community Edition | DSH Cloud Hosted | Enterprise discussions |
|---|---|---|---|
| What it is | The useful, self-hosted public repository | The managed service at `dshcloud.online` | Potential net-new extensions, commercial rights, and support scoped by a signed agreement |
| Start path | Docker/Compose, npm, or uv installation | [Sign in and open a managed workspace](https://dshcloud.online/login?next=%2Fwork) | Contact support with requirements |
| License | AGPL-3.0-only for the current repository | Service use is governed by Hosted terms; this does not alter the repository license | No public-code right is removed; separate rights exist only in a signed agreement |
| Models | Operator selects and pays an OpenAI-compatible upstream | Managed model access and plan allowances | Private-provider and governance integration may be scoped |
| Operations | Operator owns TLS, identity, email, data, backups, monitoring, and upgrades | Managed capacity, upgrades, backups, monitoring, billing operations, and incident response | Deployment and support scope defined per agreement |
| Workspaces | Optional self-hosted path, off by default; operator must review its trust model | Managed workspace capacity and storage | Net-new governance, private networking, HA, or compliance options may be considered |
| Support | Community Issues; no response-time commitment | Account and billing support for the managed service | Response and service commitments only when written into an agreement |
| Billing | Optional generic provider integrations controlled by the operator | Individual and team plans; one-time prepaid term with no automatic renewal | Contract-specific if offered |

- [View individual Hosted plans](https://dshcloud.online/pricing#plans)
- [View team plans](https://dshcloud.online/pricing#team)
- [Read support routes](../SUPPORT.md)

## Community baseline rule

[legal/licensing/COMMUNITY_BASELINE.manifest](../legal/licensing/COMMUNITY_BASELINE.manifest)
records the adoption baseline at commit
`945439f346a291722f8f0883e3c3c789d3c0463c`. Product capabilities represented by
that baseline cannot be reclassified solely to monetize them. Environment-specific
credentials, topology, backup media, cutover procedures, capacity/cost records,
customer identifiers, and incident records are private operations, not product
features.

Use this decision table for later changes:

| Question | Community | Hosted operations | Enterprise |
|---|---|---|---|
| Present in the baseline as product code or capability? | Remains Community | Not applicable | Cannot be reclassified solely for monetization |
| Environment-specific credentials, topology, backup/cutover, quotas, customer identifiers, or incident records? | Excluded from the public repository | Private | Not a product feature |
| Net-new capability after the baseline? | Community by default unless an approved boundary says otherwise | Private only when environment-specific operation | May be a separate extension only through an explicit interface and review |
| Required to build, test, self-host, secure, migrate/export data, back up, restore, or satisfy AGPL? | Must remain available | Private values may be omitted | Cannot be withheld |
| Shared by public and private implementations? | Neutral interface and useful Community implementation remain public | Environment adapter may remain private | Separate implementation may use the public interface |

“Enterprise” in this document is a product-planning category, not evidence that a
particular capability, license, SLA, certification, or package is currently
available.

## Pull request boundary checklist

For a change that affects edition placement, the pull request must:

- identify related paths in the Community baseline manifest;
- explain why the Community path remains complete, secure, buildable, testable,
  self-hostable, upgradeable, and able to export its data;
- describe the stable extension interface instead of scattering private-edition
  checks through public code;
- identify shared schema, migration, API, configuration, and downgrade effects;
- keep environment-specific Hosted operations outside the public tree; and
- attach qualified legal review when copyright, contribution terms, notices, or
  licensing are affected.
