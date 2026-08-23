/** dsh-plugin-cloud — cordis entry.
 *
 * The runtime job is deliberately small: make sure the gateway token is present
 * in the process environment before the LLM plugins resolve their apiKeyEnv
 * references. Everything interactive (device login, catalog refresh, writing
 * the provider row) lives in the CLI: `npx dsh-plugin-cloud setup`.
 */

import { TOKEN_ENV } from './config.js'
import { loadAuthSync } from './store.js'

export const name = 'dsh-plugin-cloud'
export const inject = []

export function apply() {
  if (process.env[TOKEN_ENV]) return
  const auth = loadAuthSync()
  if (auth?.token) {
    process.env[TOKEN_ENV] = auth.token
    return
  }
  console.warn(
    `[dsh-plugin-cloud] no DSH Cloud session — run \`npx dsh-plugin-cloud setup\` to log in`,
  )
}
