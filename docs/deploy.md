# Self-host deployment

English | [简体中文](deploy.zh-CN.md)

This guide covers the public Community Edition. It does not describe any live DSH
Cloud environment. For the managed service, use
[DSH Cloud Hosted](https://dshcloud.online/login?next=%2Fwork); the remaining
steps are intentionally neutral self-host instructions.

## Choose an installation path

| Path | Best for | Network exposure |
|---|---|---|
| Canonical Docker Compose | Persistent self-hosted installation with Caddy/TLS | Caddy publishes configured HTTP/HTTPS ports; app remains internal |
| Docker single container | Local evaluation or integration behind your own proxy | Bind to loopback unless a trusted proxy is in place |
| npm/npx or uv/uvx CLI | Guided configuration and lifecycle commands | Generates and operates the same versioned stack |
| Source development | Contributors changing server behavior | Development mode; never expose publicly |

Release `0.3.0` coordinates packages and images through `release/release.json`.
Use exact package versions or image digests in automation. Source builds remain
available for development and independent verification.

## Prerequisites

- Docker Engine or Docker Desktop with Compose v2;
- a supported Linux host for a public deployment;
- an OpenAI-compatible upstream URL and API key;
- a domain whose DNS points to the host for automatic public TLS; and
- enough persistent storage for the database, logs, releases, and optional
  workspaces.

For public use, also prepare SMTP or an approved identity provider, a backup and
restore process, monitoring, and the security controls in [security.md](security.md).

## Canonical Compose deployment

From a source checkout:

```bash
git clone https://github.com/AgentsDanceAI/deepseek-harness-cloud.git
cd deepseek-harness-cloud
cp deploy/selfhost/.env.example deploy/selfhost/.env
chmod 600 deploy/selfhost/.env
```

Edit `deploy/selfhost/.env`. At minimum, review:

- `DOMAIN`, `SITE_SCHEME`, and `PUBLIC_BASE`;
- `AUTH_SECRET` (generate with `openssl rand -hex 32`);
- `UPSTREAM_BASE_URL` and `UPSTREAM_API_KEY`; and
- `ADMIN_EMAILS`; and
- for public mode, either `MAIL_SMTP_HOST` (plus the credentials required by
  your SMTP provider), a complete Google OAuth client, or a complete GitHub
  OAuth client — without one of these nobody can register the first account and
  `start` refuses to run.

`dsh-cloud init --mode selfhost --domain <domain> --admin-email <email>` collects
those interactively and writes the `.env` for you. It also **enables the cloud
workspace by default**: `WORK_ENABLED=1`, `WORK_DOMAIN=work.<domain>`,
`COOKIE_DOMAIN=.<domain>`, `COMPOSE_PROFILES=work`. The workspace is routed by
hostname, so add a DNS record for `work.<domain>` pointing at that server; the
leading dot in `COOKIE_DOMAIN` is required, or the session never reaches the
subdomain and every visit bounces back to the login page. Clear those four
settings to leave the workspace off.

For a local trial, use `DOMAIN=localhost`, `SITE_SCHEME=http`, `DHC_DEV=1`,
`PUBLIC_BASE=http://localhost:8787`, `BIND_ADDRESS=127.0.0.1`,
`HTTP_PORT=8787`, and `HTTPS_PORT=8443`. For every public deployment, use HTTPS
and `DHC_DEV=0`; choose public bind addresses only after firewall and
reverse-proxy review. Do not run the public `up` command until an identity path
is configured: new accounts require verified email or OAuth ownership.

Validate interpolation before starting:

```bash
docker compose \
  --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml \
  config --quiet
```

Start and verify:

```bash
docker compose \
  --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml \
  up -d --build

docker compose \
  --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml \
  ps

curl --fail --show-error http://localhost:8787/readyz
```

For a real domain, replace the final URL with the configured HTTPS origin. Use
`/livez` for restart probes and `/readyz` for traffic readiness. `/api/health`
remains a liveness-only compatibility endpoint, not a readiness gate.

The assisted source-checkout flow performs the same setup:

```bash
bash scripts/quickstart.sh --domain localhost --admin-email you@example.com
```

It prompts for the upstream key and never overwrites an existing environment file
without applying explicit flags.

## Versioned CLI paths

After version `0.3.0` has been published, the one-shot npm path is:

```bash
npx --yes @agentsdanceai/dsh-cloud@0.3.0 init --mode trial
npx --yes @agentsdanceai/dsh-cloud@0.3.0 doctor
npx --yes @agentsdanceai/dsh-cloud@0.3.0 up --wait
```

The installed npm equivalent is:

```bash
npm install --global @agentsdanceai/dsh-cloud@0.3.0
dsh-cloud init --mode trial
dsh-cloud doctor
dsh-cloud up --wait
```

The isolated Python path is:

```bash
uvx dsh-cloud==0.3.0 init --mode trial
uvx dsh-cloud==0.3.0 doctor
uvx dsh-cloud==0.3.0 up --wait
```

The installed `uv` equivalent is:

```bash
uv tool install dsh-cloud==0.3.0
dsh-cloud init --mode trial
dsh-cloud doctor
dsh-cloud up --wait
```

Do not replace pinned versions with `latest` in unattended automation. Review the
generated environment file before `up`, especially public origin, secret,
provider, storage, and workspace settings.

## Docker single-container path

After the image exists in GHCR, the versioned image name is:

```text
ghcr.io/agentsdanceai/dsh-cloud-server:0.3.0
```

A version tag can be moved by a registry administrator. For immutable
automation, resolve the published manifest digest and use
`ghcr.io/agentsdanceai/dsh-cloud-server@sha256:<digest>`.

Use an environment file with permissions `0600`, a persistent `/app/data` volume,
and a loopback bind when another proxy will terminate TLS:

```bash
docker volume create dsh-cloud-data
mkdir -p .dsh-cloud
umask 077
printf 'AUTH_SECRET=%s\nDHC_DEV=1\nPUBLIC_BASE=http://127.0.0.1:8081\nPRICING_FILE=pricing.cny.json\n' \
  "$(openssl rand -hex 32)" > .dsh-cloud/docker.env
docker run --detach --name dsh-cloud \
  --env-file .dsh-cloud/docker.env \
  --mount source=dsh-cloud-data,target=/app/data \
  --publish 127.0.0.1:8081:8100 \
  ghcr.io/agentsdanceai/dsh-cloud-server:0.3.0
curl --fail --show-error http://127.0.0.1:8081/readyz
```

To build the same server from the current checkout before publication:

```bash
docker build --file server/Dockerfile --tag dsh-cloud-server:local .
docker run --rm \
  --env-file .dsh-cloud/docker.env \
  --mount source=dsh-cloud-data,target=/app/data \
  --publish 127.0.0.1:8081:8100 \
  dsh-cloud-server:local
```

The single container does not terminate TLS. Keep the loopback bind and put a
reviewed reverse proxy in front for network access.

## Configuration boundaries

The self-host example contains placeholders only. Do not use another operator's
legal documents, OAuth applications, payment merchant identifiers, domains, or
provider credentials. Optional integrations remain off or unavailable until their
required values are supplied.

Model IDs come from `server/config/models.json`. Configure identifiers actually
served by your upstream. Keep configuration changes under review: a valid YAML,
JSON, or Compose file is not necessarily a secure or useful deployment.

## Data, backup, and restore

The canonical Compose project stores SQLite data in its named `dhc-data` volume.
Back up with a database-consistent mechanism before every upgrade. SQLite WAL
state means copying only the live `.db` file is not a verified backup. Stop writes
or use the SQLite backup API, then copy the backup to encrypted storage outside
the host.

For PostgreSQL, use the database provider's consistent dump/snapshot and restore
procedure. Test every backup generation by restoring into a disposable instance
and checking `/readyz`, account access, schema version, and representative data.

Record backup location, retention, restore owner, and last successful restore in
a private operations system—not this repository.

## Upgrades and rollback

1. Read [CHANGELOG.md](../CHANGELOG.md) and version-specific release notes.
2. Record the current image/package version, configuration checksum, and database
   schema.
3. Create and test a current backup.
4. Pull or install an immutable target version; do not use a floating branch or
   tag as rollback evidence.
5. Run configuration validation and a disposable upgrade rehearsal.
6. Upgrade, wait for `/readyz`, and run authenticated smoke tests.
7. Roll back application artifacts only when the documented schema compatibility
   allows it; otherwise restore the matching backup.

Never assume `docker compose down` deletes data, and never add `--volumes` to a
routine stop command. Review the resolved Compose project and volume names before
changing directory, project name, or stack layout.

## Scaling

SQLite and process-local rate/concurrency state are suited to a single application
instance. Before multiple workers or hosts, use PostgreSQL, external coordination
for distributed limits and jobs, shared artifact/storage design, and load-tested
readiness/rollback behavior. Do not infer high availability from multiple
containers alone.

## Optional workspaces

Workspaces are disabled by default. They execute user-controlled code and require
Docker authority, separate DNS/origin design, persistent storage, resource limits,
and a reviewed isolation model. Follow [security.md](security.md) before enabling
the `work` profile. The example is not a claim of hostile multi-tenant isolation.

## Troubleshooting

```bash
docker compose \
  --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  logs --tail 200 dhc-server dhc-caddy

curl --fail --show-error http://localhost:8787/livez
curl --fail --show-error http://localhost:8787/readyz
```

Redact secrets, cookies, tokens, email addresses, order references, private
addresses, and user content before opening a public issue. See
[SUPPORT.md](../SUPPORT.md).
