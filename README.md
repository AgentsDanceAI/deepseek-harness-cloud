<div align="center">

# DSH Cloud

**Managed cloud agents and a self-hostable platform around DeepSeek Harness.**

Accounts, a server-side model gateway, usage policy, teams, and an optional
browser workspace—without distributing an upstream model key to every client.

[![CI](https://github.com/AgentsDanceAI/deepseek-harness-cloud/actions/workflows/ci.yml/badge.svg)](https://github.com/AgentsDanceAI/deepseek-harness-cloud/actions/workflows/ci.yml)
[![License: AGPL-3.0-only](https://img.shields.io/badge/license-AGPL--3.0--only-4c6ef5.svg)](LICENSE)
[![Security policy](https://img.shields.io/badge/security-private%20reporting-2f9e44.svg)](SECURITY.md)

Source candidate: [`0.2.0`](release/release.json) · Registry artifacts not yet published

[中文](README.zh-CN.md) · [Architecture](docs/architecture.md) ·
[Self-host](docs/deploy.md) · [Editions](docs/editions.md) ·
[Security](SECURITY.md) · [Support](SUPPORT.md)

</div>

> [!IMPORTANT]
> The repository is currently licensed **AGPL-3.0-only**. A future Open Core /
> source-available model is only a non-operative plan subject to counsel and a
> complete rights audit. It does not change this code's current license or revoke
> rights already granted for AGPL versions. See [Licensing](LICENSING.md).

## Choose your path

<!-- path:hosted -->

### Use DSH Cloud Hosted

[DSH Cloud Hosted](https://dshcloud.online/login?next=%2Fwork) is the managed
subscription service: no server installation, managed model and workspace
capacity, upgrades, monitoring, backups, and account support. It is not a token
resale service.

Current public offers are paid once for the selected monthly or annual term and
**do not renew automatically**.

[**Start on DSH Cloud Hosted**](https://dshcloud.online/login?next=%2Fwork) ·
[Individual plans](https://dshcloud.online/pricing#plans) ·
[Team plans](https://dshcloud.online/pricing#team)

<!-- path:selfhost -->

### Self-host Community Edition

Run the AGPL-3.0 Community Edition with your own domain, database, identity
providers, model upstream, storage, and operational controls. Docker Compose is
the canonical persistent path; Docker, npm/npx, and uv/uvx use the same versioned
stack contract.

[**Self-hosting guide**](docs/deploy.md) ·
[Configuration template](deploy/selfhost/.env.example) ·
[Security checklist](docs/security.md)

<!-- path:develop -->

### Develop and contribute

The public repository includes the FastAPI service, web console, gateway,
deployment definitions, desktop overlay, mobile shells, tests, and release
contracts.

[**Development setup**](#development) · [Contributing](CONTRIBUTING.md) ·
[Architecture](docs/architecture.md) · [Changelog](CHANGELOG.md)

## What is included

| Capability | Community behavior |
|---|---|
| Account access | Email/password and email-code paths, optional Google/GitHub OAuth, revocable browser/device/API credentials |
| Model gateway | OpenAI-compatible chat/models and an Anthropic-compatible messages surface; operator upstream key remains server-side |
| Usage and plans | Model catalog, server-side pricing, credit ledger, rate/concurrency gates, usage records, and optional entitlements |
| Teams | Membership, roles, seats, pooled allowances, and per-member attribution |
| Payments | Optional provider adapters with server-authoritative products and authenticated, idempotent webhook handling |
| Web console | Account, plan, order, team, administration, legal, and download surfaces |
| Desktop and mobile | Device authorization plus desktop overlay and mobile shell integration contracts |
| Workspaces | Optional browser-accessible agent runtime; disabled by default and subject to the isolation warning below |
| Operations | Docker/Compose definitions, readiness/liveness/version endpoints, persistent data, release metadata, and upgrade guidance |

Self-hosters remain responsible for infrastructure, provider agreements, model
costs, TLS, identity/email delivery, payment configuration, data protection,
backups, monitoring, and applicable law.

## Quick start

The source checkout is available now. Registry artifacts named `0.2.0` are
candidate release coordinates and are usable only after the first `v0.2.0`
publication is visible in npm, PyPI, or GHCR. A source version is not evidence of
a published package.

<!-- distribution-install:start -->

### Docker Compose from source (available now)

```bash
git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud.git
cd deepseek-harness-cloud
bash scripts/quickstart.sh --domain localhost --admin-email you@example.com
```

The script creates `deploy/selfhost/.env`, generates `AUTH_SECRET`, prompts for
the model upstream, starts the canonical Compose stack, and checks readiness.
Open <http://localhost:8787>. Development mode prints sign-in codes to server logs;
never use it on a public network.

Manual Compose validation and start:

```bash
cp deploy/selfhost/.env.example deploy/selfhost/.env
chmod 600 deploy/selfhost/.env
# For local trial: DOMAIN=localhost, SITE_SCHEME=http, DHC_DEV=1,
# PUBLIC_BASE=http://localhost:8787, BIND_ADDRESS=127.0.0.1, HTTP_PORT=8787.
# Also set AUTH_SECRET, upstream, and admin.
docker compose --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml config --quiet
docker compose --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml up -d --build
curl --fail --show-error http://localhost:8787/readyz
```

### Docker single container from source (available now)

```bash
docker build --tag dsh-cloud-server:local --file server/Dockerfile .
docker volume create dsh-cloud-data
mkdir -p .dsh-cloud
umask 077
printf 'AUTH_SECRET=%s\nDHC_DEV=1\nPUBLIC_BASE=http://127.0.0.1:8081\nPRICING_FILE=pricing.cny.json\n' \
  "$(openssl rand -hex 32)" > .dsh-cloud/docker.env
docker run --rm --name dsh-cloud \
  --env-file .dsh-cloud/docker.env \
  --publish 127.0.0.1:8081:8100 \
  --mount type=volume,src=dsh-cloud-data,dst=/app/data \
  dsh-cloud-server:local
```

In another terminal, check `http://127.0.0.1:8081/readyz`. Add your upstream key
to `.dsh-cloud/docker.env` before model calls. The single container does not
terminate TLS; keep the loopback bind and use a reviewed reverse proxy for
network access.

### npm and npx (after `0.2.0` is published)

One-shot:

```bash
npx --yes @agentsdanceai/dsh-cloud@0.2.0 start --mode trial --wait
```

Installed:

```bash
npm install --global @agentsdanceai/dsh-cloud@0.2.0
dsh-cloud start --mode trial --wait
```

### uv and uvx (after `0.2.0` is published)

One-shot:

```bash
uvx dsh-cloud==0.2.0 start --mode trial --wait
```

Installed:

```bash
uv tool install dsh-cloud==0.2.0
dsh-cloud start --mode trial --wait
```

For explicit lifecycle control, both CLIs also provide `init`, `doctor`, and
`up`. Pin immutable versions in automation and review generated configuration
before starting it.

<!-- distribution-install:end -->

The full deployment guide covers configuration, the versioned GHCR path,
backups, upgrades, rollback, scaling, and troubleshooting:
[docs/deploy.md](docs/deploy.md).

## Architecture at a glance

```text
browser / desktop / mobile
          |
          | HTTPS + session/device credential
          v
  TLS edge / reverse proxy
          |
          v
  DSH Cloud FastAPI service --------> operator-selected model/search upstream
   | accounts, teams, plans            server-side provider credential
   | model gateway and metering
   | web console and payments
   |
   +---- SQLite or PostgreSQL
   |
   +---- optional workspace backend -> agent runtime container
```

The gateway authenticates and authorizes the caller, validates a configured
model ID, replaces the client credential with the operator's upstream credential,
streams the response, and records normalized usage. See the
[architecture document](docs/architecture.md) for flows and trust boundaries.

## Security posture

Workspaces execute user-controlled code. They are off by default. A container and
Docker socket proxy are not automatically a hostile multi-tenant security
boundary; public operators must review image trust, Docker authority, networks,
mounts, privileges, egress, previews, credentials, and resource isolation before
enabling them. See [docs/security.md](docs/security.md).

Report vulnerabilities privately through the
[GitHub security advisory form](https://github.com/AgentsDanceAI/deepseek-harness-cloud/security/advisories/new)
or `security@agentsdance.ai`. Do not open a public vulnerability issue or include
live credentials and user data. See [SECURITY.md](SECURITY.md).

## Development

Python 3.11+, `uv`, and Node.js 22 are the tested contributor toolchain:

```bash
uv sync --project server --all-extras --locked
uv run --project server pytest server/tests -q
uv run --project server ruff check server/app server/tests server/scripts
node --test server/tests/js/*.test.mjs
```

Run the server locally:

```bash
DHC_DEV=1 AUTH_SECRET=local-development-secret \
  uv run --project server uvicorn app.main:app --app-dir server --reload
```

CLI source checks, once the package directories are present in the candidate
tree:

```bash
node packages/cli-npm/bin/dsh-cloud.mjs --help
node packages/cli-npm/bin/dsh-cloud.mjs start --dry-run --json
uv run --project packages/cli-python dsh-cloud --help
uv run --project packages/cli-python dsh-cloud start --dry-run --json
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) for DCO sign-off, provenance disclosure,
tests, security routing, and edition-boundary review.

## Repository map

| Path | Purpose |
|---|---|
| `server/` | FastAPI application, gateway, data layer, templates, configuration, tests |
| `deploy/selfhost/` | Canonical public Compose stack, Caddy config, and environment template |
| `packages/` | Version-matched npm and Python lifecycle CLIs |
| `release/` | Canonical release identity and schemas |
| `desktop/` | Pinned upstream desktop assembly, minimal patches, and cloud integration plugin |
| `mobile/`, `miniprogram/` | Mobile integration shells |
| `docs/` | Architecture, deployment, security, edition, compatibility, and maintainer docs |
| `legal/` | Hosted legal documents and third-party/licensing records |

## Versions, licensing, and marks

- Current software license: [GNU AGPL v3 only](LICENSE).
- Human-readable license status and future gates: [LICENSING.md](LICENSING.md).
- Community, Hosted, and Enterprise boundary: [docs/editions.md](docs/editions.md).
- Trademark use: [TRADEMARKS.md](TRADEMARKS.md).
- Third-party notices: [legal/THIRD_PARTY_NOTICES.md](legal/THIRD_PARTY_NOTICES.md).
- Release changes: [CHANGELOG.md](CHANGELOG.md).

Current AGPL rights remain in force for versions received under AGPL-3.0. Any
future Dify-style Open Core/source-available terms would be prospective only,
after qualified legal approval and proof of relicensing authority.

DSH Cloud is independently developed and operated. It is not affiliated with or
endorsed by DeepSeek. “DeepSeek” and related marks belong to their respective
owners.
