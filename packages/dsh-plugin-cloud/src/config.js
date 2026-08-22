/** Shared constants. Mirrors desktop/dsh-plugin-cloud/src/config.ts — the two
 * clients speak to the same production service, so the semantics must match. */

import { homedir } from 'node:os'
import { join } from 'node:path'

/** Cloud service base URL. DSH_CLOUD_BASE overrides for staging/self-hosted. */
export const CLOUD_BASE = (process.env.DSH_CLOUD_BASE ?? 'https://dshcloud.online')
  .replace(/\/+$/, '')

/**
 * Env var carrying the gateway token. Two deliberate properties, both verified
 * against upstream: the `DSH_` prefix is bootstrap-only (a `.env` file cannot
 * inject it), and the name matches dsh's SENSITIVE_ENV_PATTERN so it is
 * scrubbed from every spawned subprocess (bash tool, MCP servers).
 */
export const TOKEN_ENV = 'DSH_CLOUD_TOKEN'

/** DeepSeek Harness home (upstream default: ~/.dsh, overridable via DSH_HOME). */
export function dshHome() {
  return process.env.DSH_HOME ?? join(homedir(), '.dsh')
}

/** The user-owned config layer upstream applies after the profile's own. */
export function homePatchPath() {
  return join(dshHome(), 'cordis.patch.yml')
}

/** Token store, kept inside the dsh home so `rm -rf ~/.dsh` removes everything. */
export function authStorePath() {
  return join(dshHome(), 'dsh-cloud-auth.json')
}

/** Row ids this package owns in the home patch layer. Never touch other rows. */
export const PLUGIN_ROW_ID = 'dsh-plugin-cloud'
export const PROVIDER_ROW_ID = 'dsh-cloud-models'

/** The upstream multi-provider LLM plugin the provider row instantiates. */
export const PI_AI_PACKAGE = '@deepseek-ai/dsh-llm-pi-ai'
