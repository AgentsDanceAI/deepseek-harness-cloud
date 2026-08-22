/** Managed rows in $DSH_HOME/cordis.patch.yml — upstream's user-owned config
 * layer, applied after the profile's own patches (rows keyed by id, last write
 * wins per row).
 *
 * Contract: this module only ever touches rows whose id is in MANAGED_IDS.
 * Every other op and row in the file passes through untouched. Two design
 * choices follow from "we are a guest in the user's file":
 *  - the provider lives in its OWN pi-ai instance row (PROVIDER_ROW_ID), never
 *    in the stock `llm-pi-ai` row — last-write-wins would clobber providers the
 *    user configured there;
 *  - rewriting the file loses YAML comments (parse/stringify round-trip), so a
 *    timestamped backup is written first whenever the file already exists.
 */

import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import YAML from 'yaml'
import { CLOUD_BASE, PI_AI_PACKAGE, PLUGIN_ROW_ID, PROVIDER_ROW_ID, TOKEN_ENV } from './config.js'

export const MANAGED_IDS = new Set([PLUGIN_ROW_ID, PROVIDER_ROW_ID])

/** The pi-ai instance row exposing the cloud gateway as a provider. */
export function providerRow(models) {
  return {
    id: PROVIDER_ROW_ID,
    name: PI_AI_PACKAGE,
    config: {
      providers: {
        'dsh-cloud': {
          displayName: 'DSH Cloud',
          api: 'openai-completions',
          baseURL: `${CLOUD_BASE}/llm/v1`,
          apiKeyEnv: TOKEN_ENV,
          models,
        },
      },
    },
  }
}

export function pluginRow() {
  return { id: PLUGIN_ROW_ID, name: 'dsh-plugin-cloud' }
}

/** Remove managed rows wherever they appear; drop ops that become empty. */
export function stripManagedRows(ops) {
  const kept = []
  for (const op of Array.isArray(ops) ? ops : []) {
    if (op && typeof op === 'object' && Array.isArray(op.insert)) {
      const rows = op.insert.filter((row) => !MANAGED_IDS.has(row?.id))
      if (rows.length > 0 || Object.keys(op).length > 1) kept.push({ ...op, insert: rows })
    } else {
      kept.push(op)
    }
  }
  return kept
}

/** Pure core: previous ops → next ops with exactly one managed insert appended. */
export function upsertManagedOps(ops, rows) {
  return [...stripManagedRows(ops), { insert: rows }]
}

/** Read-modify-write $DSH_HOME/cordis.patch.yml. Returns the backup path, if any. */
export function writeManagedRows(path, rows) {
  let previous = []
  let backup
  if (existsSync(path)) {
    const raw = readFileSync(path, 'utf8')
    const parsed = YAML.parse(raw)
    if (parsed !== null && parsed !== undefined && !Array.isArray(parsed)) {
      throw new Error(`${path} is not a patch list — refusing to rewrite it`)
    }
    previous = parsed ?? []
    backup = `${path}.bak-${new Date().toISOString().replace(/[:.]/g, '-')}`
    copyFileSync(path, backup)
  }
  const next = upsertManagedOps(previous, rows)
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, YAML.stringify(next))
  return backup
}
