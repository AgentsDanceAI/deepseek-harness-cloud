/** DSH Cloud client constants. Assembled into dsh-plugin-desktop/src/cloud/. */

/**
 * Cloud service base URL. DSH_CLOUD_BASE (real process environment only — the
 * DSH_ prefix is bootstrap-only, .env files cannot set it) overrides for
 * staging/self-hosted deployments.
 */
export const CLOUD_BASE: string = (process.env.DSH_CLOUD_BASE ?? 'https://dshcloud.agentsdance.ai')
  .replace(/\/+$/, '')

/**
 * Environment variable that carries the user's gateway token inside the main
 * process. Deliberate properties of this name, both verified against upstream:
 *  - `DSH_` prefix is on the bootstrap-only list, so a `.env` file cannot
 *    inject or override it — only this launcher sets it;
 *  - it matches dsh's SENSITIVE_ENV_PATTERN (/KEY|PASSWORD|SECRET|TOKEN/i), so
 *    dsh scrubs it from every spawned subprocess (bash tool, MCP servers).
 */
export const CLOUD_TOKEN_ENV = 'DSH_CLOUD_TOKEN'

/** Filename (under Electron userData) holding the encrypted device token. */
export const AUTH_STORE_FILENAME = 'cloud-auth.json'
