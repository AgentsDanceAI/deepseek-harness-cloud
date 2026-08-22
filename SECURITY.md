# Security policy

We welcome responsible vulnerability reports and coordinate fixes privately.

## Supported versions

Security fixes are developed for the latest maintained release and the current
default branch. Older releases may no longer receive fixes. Until the project
publishes a version-specific support table, do not assume a fixed maintenance
window; upgrade to the newest maintained release before requesting a backport.

## Report a vulnerability privately

Use one of these channels:

1. [Open a private GitHub security advisory](https://github.com/AgentsDanceAI/deepseek-harness-cloud/security/advisories/new).
2. If private reporting is unavailable, email
   [security@agentsdance.ai](mailto:security@agentsdance.ai).

Do **not** open a public issue, discussion, or pull request for an unpatched
vulnerability. Do not include live credentials, access tokens, personal data,
customer data, or production dumps. Revoke an exposed credential through its
provider before reporting it.

Include, when available:

- affected version or commit and deployment mode;
- impact and the assumptions required to reproduce it;
- minimal, non-destructive reproduction steps using synthetic data;
- relevant logs with secrets and identifiers removed;
- suggested mitigation; and
- whether you plan to publish details and any proposed disclosure date.

## What to expect

Maintainers will acknowledge the report, validate it, coordinate remediation,
and discuss disclosure through the private channel. The project does not promise
an unverified response or fix deadline. Contracted support terms, if any, apply
only to the parties and scope named in that agreement.

Please allow a reasonable remediation period before publication. We may ask you
to test a candidate fix. Credit is offered when requested and appropriate, but
we do not promise a bounty.

## Scope and safe research

Good-faith research should minimize access, modification, and retention of data;
avoid privacy violations, service disruption, denial of service, social
engineering, and physical attacks; and stop once enough evidence exists to
demonstrate the issue. Do not test against accounts or systems you do not own or
have permission to assess.

Self-hosted operators are responsible for their own infrastructure, secrets,
upstream providers, network policy, backups, and incident response. Hardening
guidance is in [docs/security.md](docs/security.md).
