# Self-host security guide

This guide complements the vulnerability-reporting policy in
[SECURITY.md](../SECURITY.md). It is a deployment checklist, not a certification
or guarantee. Self-hosted operators own their threat model, infrastructure,
providers, backups, monitoring, and incident response.

## Before exposing an instance

- Use a dedicated host or account with supported OS, Docker, and dependency
  security updates.
- Generate a unique high-entropy `AUTH_SECRET`; do not reuse a Hosted or upstream
  provider credential. A protected `AUTH_SECRET_FILE` can be used instead of a
  direct environment value where the deployment supports secret files.
- Store upstream, OAuth, SMTP, database, and payment credentials outside Git and
  container images. Restrict access to the runtime account.
- Put the application behind an HTTPS reverse proxy. Do not expose port `8100`
  directly to an untrusted network.
- Set the exact public origin and OAuth callbacks; do not use wildcard origins.
- Keep development mode disabled on any network-accessible deployment.
- Disable unused sign-in, payment, search, workspace, and provider integrations.
- Use a dedicated database account and encrypted, access-controlled backups.
- Configure log rotation and ensure logs contain no prompts, credentials, or
  personal data beyond what is operationally necessary.

## Operational probes and response limits

Use `/livez` only to determine whether the process should be restarted. Use
`/readyz` to determine whether it should receive traffic; readiness validates the
database and required application configuration. `/api/health` remains a
liveness-only compatibility endpoint and must not be used as a readiness gate.

The application defaults to a 2 MiB normal API body limit and a 256 KiB payment
webhook limit. Preserve equally strict or tighter limits at the reverse proxy and
reject oversized requests before buffering them. Apply connection, header,
upstream, and idle timeouts suitable for streaming responses.

Security headers deny framing, MIME sniffing, and unnecessary browser
capabilities; HTML responses receive a content security policy. HTTPS deployments
add HSTS. If another proxy or CDN changes these headers, verify the final public
response instead of assuming the application values survived.

## Identity and secrets

- Rotate `AUTH_SECRET` only with a planned logout of existing sessions and
  devices.
- Revoke exposed upstream/provider keys at the provider first; removing them from
  Git or logs is not revocation.
- Keep administrator email configuration narrow and protect those mailboxes with
  MFA.
- Treat session cookies, device tokens, API keys, password-reset/login codes, and
  support bundles as credentials.
- Do not pass a general account or provider token into agent-controlled code.
- Set `Secure`, HTTP-only, same-site, domain, and proxy trust settings for the
  actual deployment origin.

## Model gateway and external content

Allow only configured upstream origins and model identifiers. Use TLS validation,
timeouts, bounded response handling, and least-privilege provider credentials.
Do not log prompt or completion bodies by default. Review any feature that fetches
user-supplied URLs for SSRF, redirect, DNS-rebinding, size, and content-type risk.

Agent-created preview content belongs on an origin separated from account and
administration pages. Stream non-HTML responses and keep HTML rewriting bounded;
`PREVIEW_HTML_MAX_BYTES` defaults to 8 MiB for the Community server. Never weaken
cookie, CORS, CSP, or origin checks simply to make a preview work.

## Payments and webhooks

Payment configuration is optional. When enabled:

- configure the provider's webhook verification key before accepting checkout;
- accept entitlement changes only from authenticated, idempotently processed
  provider events;
- resolve product, amount, currency, user, and entitlement server-side;
- retain provider event IDs for deduplication and audit;
- test refund and chargeback behavior with a sandbox; and
- never include payment credentials or raw webhook payloads in public reports.

For Waffo, checkout requires merchant, signing, and webhook-verification material.
Unsigned settlement events are rejected in every environment.

## Workspace warning

Workspaces run user-controlled code and are off by default. A Docker socket proxy
reduces API surface but is not, by itself, a complete hostile-tenant isolation
boundary. Before enabling workspaces, verify:

- images are pinned, reviewed, and run without unnecessary privilege;
- the Docker API cannot create arbitrary privileged containers, mounts, devices,
  namespaces, published ports, or commands;
- workloads cannot reach the host, control plane, metadata services, private
  networks, other tenants, or provider credentials;
- per-workspace storage, process, memory, CPU, and time limits are enforced;
- preview routing binds the authenticated user to the intended workspace and
  allowed port; and
- deletion, idle cleanup, backup, and restore cannot remove another user's data.

For a public multi-tenant service, use a dedicated isolation design and complete
an independent security review. Do not treat the optional Community workspace
profile as a production multi-tenant guarantee.

## Database and backups

- Back up before every upgrade and keep multiple tested generations.
- Encrypt backups, separate them from the application host, restrict access, and
  define retention and deletion.
- Exercise restore into a disposable environment; a backup that has not restored
  successfully is not verified.
- For SQLite, capture a consistent backup rather than copying a live database and
  ignoring WAL state.
- Use PostgreSQL and external coordination before running multiple application
  workers or hosts.

## Incident response

Prepare contacts, credential-revocation steps, a read-only or shutdown option,
backup/restore ownership, and a communication path before an incident. Preserve
only necessary evidence in a restricted location. Do not commit incident notes,
user data, private topology, or live indicators to this repository.

Report a vulnerability in DSH Cloud privately using
[SECURITY.md](../SECURITY.md).
