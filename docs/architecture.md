# Architecture

DSH Cloud adds accounts, policy, metering, a model gateway, and an optional
browser workspace around the DeepSeek Harness agent runtime. The same public
service can be operated by a self-hoster or used through DSH Cloud Hosted.

This document describes repository interfaces, not the topology, capacity,
credentials, or provider configuration of any live environment.

## System context

```text
Browser / desktop / mobile client
              |
              | HTTPS, session or device credential
              v
       reverse proxy / TLS edge
              |
              v
      FastAPI application (:8100)
       |        |          |
       |        |          +---- account, team, plan and admin APIs
       |        +--------------- OpenAI/Anthropic-compatible model gateway
       +------------------------ server-rendered console and static assets
              |                         |
              |                         +---- operator-selected model/search upstream
              |
              +---- SQLite or PostgreSQL
              |
              +---- optional workspace backend ---- agent runtime container
```

The reverse proxy is the only intended public listener in the canonical Compose
stack. The application listens on port `8100` inside its network. The model
upstream credential remains server-side; clients receive revocable account,
device, or API credentials scoped to this service.

## Components

### Application service

`server/app/main.py` assembles the FastAPI service. Its routers cover accounts,
OAuth, device authorization, the model gateway, payments, teams, administration,
desktop updates, workspaces, and server-rendered pages. Static assets and local
release files are served from bounded application paths.

The service exposes these operational endpoints:

- `GET /livez`: process liveness;
- `GET /readyz`: dependency and configuration readiness;
- `GET /version`: release version and revision only; and
- `GET /api/health`: compatibility liveness endpoint for existing tooling.

Liveness and the compatibility health endpoint answer only whether the process
can serve. Traffic automation should use readiness, which checks required
application state and returns `503` when the instance should not receive traffic.

### Identity and authorization

Browser sessions use an HTTP-only cookie. Desktop clients use a device
authorization flow and store the resulting device credential in the platform's
secure storage. API keys and device/session credentials are revocable. A session
epoch invalidates older credentials after account-sensitive changes.

Authentication and authorization are separate: a valid identity must still pass
account status, team role, plan, quota, rate, and route-specific checks.

### Model gateway

The gateway exposes OpenAI-compatible chat/model routes and an
Anthropic-compatible messages route used by supported search integrations. It:

1. authenticates the caller and resolves account/team policy;
2. validates the requested public model identifier;
3. enforces rate, concurrency, and entitlement gates;
4. replaces the caller credential with the operator's upstream credential;
5. streams the upstream response without logging prompt content; and
6. records normalized usage against the request and credit ledger.

Self-hosters choose the model catalog, upstream URL, upstream key, and pricing
policy. A model identifier absent from the configured catalog is rejected rather
than forwarded arbitrarily.

### Persistence

SQLite is the default single-instance database and is stored in the persistent
data volume. PostgreSQL is supported for deployments that require external
database operation. Account state, device authorization, credits, plans, orders,
teams, API keys, usage, and operational key/value state live behind the database
layer.

Schema changes must be backward-compatible and migration-aware. Backups are an
operator responsibility for self-hosted deployments; see [deploy.md](deploy.md).

### Payments

Payment adapters are optional. Product and amount are resolved from server-side
configuration, not accepted from the browser as authority. A provider is enabled
only when its required verification material is configured. Webhook processing
must authenticate the event and remain idempotent before changing an entitlement.

The public repository supplies integration mechanisms; it does not contain a live
merchant's credentials, product identifiers, settlement records, or operational
runbooks.

### Optional workspaces

The Community Compose path can start a restricted Docker API proxy only when the
workspace profile is enabled. The application creates and manages agent runtime
containers through that proxy, while the reverse proxy routes an authenticated
browser to the selected workspace.

Workspaces execute user-controlled code. The current Community path is disabled
by default and must not be assumed safe for mutually hostile public tenants merely
because it uses containers or a socket proxy. Operators must review image trust,
Docker authority, network isolation, mounts, runtime privileges, resource limits,
preview-origin isolation, outbound access, and credential exposure before
enabling it. See [security.md](security.md).

Hosted- or Enterprise-specific backends integrate through a semantic workspace
backend boundary. Private credentials and environment topology do not belong in
the public implementation or documentation.

## Primary flows

### Browser sign-in

```text
browser -> login or OAuth route -> account verification -> session cookie
        -> authenticated console/API -> authorization and policy checks
```

### Desktop device authorization

```text
desktop -> request device code -> browser approves while signed in
        -> desktop polls once-authorized code -> revocable device credential
        -> model requests carry device credential to the gateway
```

### Streaming model request

```text
client -> authenticate -> authorize/limit -> map model -> upstream stream
       <- normalized stream <------------------------------+
                              usage -> ledger after/final stream accounting
```

### Workspace request

```text
browser -> workspace route authorization -> ensure assigned runtime
        -> trusted reverse-proxy route -> agent UI/runtime
        -> model calls return through the DSH Cloud gateway
```

## Trust boundaries

1. **Public edge:** terminate TLS, bound request sizes, and forward only expected
   headers from trusted proxies.
2. **Application:** treat every browser, client, webhook, and upstream response as
   untrusted input; enforce identity and authorization server-side.
3. **Secrets:** keep signing, upstream, OAuth, SMTP, database, and payment secrets
   outside images, Git, logs, and client responses.
4. **Database:** protect confidentiality and integrity; make backups and restores
   explicit and tested.
5. **Workspace runtime:** treat agent code and preview content as hostile to the
   control plane and to other tenants.
6. **External providers:** use timeouts, response limits, signature verification,
   idempotency, and least-privilege credentials.

## Edition and extension boundary

Community Edition remains useful without a private API or license server. Shared
interfaces must have a public implementation. Environment-specific Hosted
operations and net-new Enterprise implementations may remain separate only behind
documented interfaces; public core files should not accumulate scattered edition
conditionals. See [editions.md](editions.md).

## Versioning and compatibility

Release identity is a stable semantic version plus source revision. Published
containers and packages should be pinned by immutable version (and digest where
available), never inferred from a floating branch. API, configuration, database,
volume, and client compatibility changes require release notes and a rollback or
migration path in [CHANGELOG.md](../CHANGELOG.md).
