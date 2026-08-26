# Install DSH Cloud Community Edition

English | [简体中文](install.zh-CN.md)

Release `0.2.4` has two equivalent, version-locked installers. Both default to
local trial mode, bind Caddy to `127.0.0.1:8787`, generate a random 256-bit auth
secret in a mode-`0600` file, and invoke Docker without a shell.

Release `0.2.4` uses the exact registry coordinates
`@agentsdanceai/dsh-cloud@0.2.4` and `dsh-cloud==0.2.4`. Keep the explicit version
in scripts and automation.

## One-command trial

With npm/npx:

```bash
npx --yes @agentsdanceai/dsh-cloud@0.2.4 start --mode trial --wait
```

With uv/uvx:

```bash
uvx dsh-cloud==0.2.4 start --mode trial --wait
```

Open <http://localhost:8787>. The explicit package version is the release
contract; do not replace it with `latest`.

## Verify from this source checkout now

The source-tree entry points execute the same code that is staged into the npm
tarball and Python wheel:

```bash
node packages/cli-npm/bin/dsh-cloud.mjs --version
node packages/cli-npm/bin/dsh-cloud.mjs start --dry-run --json
npm --prefix packages/cli-npm test

uv run --project packages/cli-python dsh-cloud --version
uv run --project packages/cli-python dsh-cloud start --dry-run --json
```

To start directly from source-built containers, copy the self-host environment
template, set its required values, and include the build overlay explicitly:

```bash
cp deploy/selfhost/.env.example deploy/selfhost/.env
docker compose --env-file deploy/selfhost/.env \
  -f deploy/selfhost/docker-compose.yml \
  -f deploy/selfhost/compose.build.yml up -d --build --wait
```

## Public self-hosting

Public mode deliberately binds to all interfaces. Initialize it first so that
identity credentials can be supplied without putting secrets in shell history:

```bash
npx --yes @agentsdanceai/dsh-cloud@0.2.4 init --mode selfhost \
  --domain cloud.example.com --admin-email admin@example.com
$EDITOR dsh-cloud/.env  # configure SMTP or Google/GitHub OAuth, plus the upstream key
npx --yes @agentsdanceai/dsh-cloud@0.2.4 doctor dsh-cloud
npx --yes @agentsdanceai/dsh-cloud@0.2.4 up dsh-cloud --wait
```

The generated `.env` is mode `0600`. Public `start`/`up` refuses to launch until
SMTP or a complete Google/GitHub OAuth client is configured, because a fresh
deployment otherwise has no verified first-account path. Trial mode can start
without either identity or an upstream key; sign-in codes go to local logs and
model requests remain unavailable until a key is supplied.

## Docker only

The application Dockerfile uses the immutable base-image digests in
`release/release.json` and never copies the hosted operator's root `legal/`
documents:

```bash
docker build -t dsh-cloud-server:local -f server/Dockerfile .
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

Readiness is available at <http://127.0.0.1:8081/readyz>; liveness remains at
<http://127.0.0.1:8081/api/health>. Keep this loopback binding unless a TLS
reverse proxy is in front of it.

## Package-content verification

`scripts/release/build-packages.mjs` stages both ecosystems from one release
source and embeds `docker-compose.yml`, Caddy, the model/pricing configuration,
and `release-manifest.json` in both artifacts:

```bash
node scripts/release/build-packages.mjs dist/packages
npm pack dist/packages/npm --dry-run --json
uv build --wheel --project dist/packages/python --out-dir dist/packages/python
```

These commands build local artifacts only; they do not publish, deploy, or
contact a production service.
