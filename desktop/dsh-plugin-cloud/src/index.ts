/** DSH Cloud gate: the login wall and the gateway injection.
 *
 * Called from dsh-plugin-desktop/src/main.ts by the 0003-cloud-gate patch —
 * the ONLY two upstream call sites. Everything else lives in this directory so
 * upstream bumps stay a mechanical rebase.
 */

import { app } from 'electron'
import { validateToken, type CloudUser } from './api.ts'
import { clearToken, loadToken, saveToken } from './auth-store.ts'
import { CLOUD_BASE, CLOUD_TOKEN_ENV } from './config.ts'
import { runLoginWindow } from './login-window.ts'

export interface CloudSession {
  token: string
  user?: CloudUser
}

/**
 * Blocks until a usable cloud session exists, or returns undefined when the
 * user declined to log in (the launcher then quits).
 *
 * On success the token is exported into the real process environment under
 * CLOUD_TOKEN_ENV, where dsh's credential seam (env source: highest priority,
 * read-only, resolved per request) picks it up via the apiKeyEnv references
 * injected by cloudProfilePatches().
 */
export async function cloudGate(): Promise<CloudSession | undefined> {
  const userDataDir = app.getPath('userData')
  const stored = loadToken(userDataDir)
  if (stored !== undefined) {
    try {
      const user = await validateToken(stored.token)
      if (user !== undefined) {
        process.env[CLOUD_TOKEN_ENV] = stored.token
        return { token: stored.token, user }
      }
      clearToken(userDataDir) // definitively rejected (revoked device, epoch bump)
    } catch {
      // Network failure: offline grace. The gateway is the real enforcement
      // point — a revoked token still dies there on the next request.
      process.env[CLOUD_TOKEN_ENV] = stored.token
      return { token: stored.token }
    }
  }

  const outcome = await runLoginWindow()
  if (outcome === undefined) return undefined
  saveToken(userDataDir, outcome.token, outcome.user.email)
  process.env[CLOUD_TOKEN_ENV] = outcome.token
  return outcome
}

/**
 * Cordis patch rows appended after prepareDesktopProfile()'s own pushes, so
 * they win over bundle, profile, and machine-level layers (last write wins per
 * row). Row ids and config fields are upstream contract points — see
 * docs/compatibility.md; desktop/scripts/verify-contract.mjs asserts them on
 * every upstream bump.
 *
 * Typed loosely on purpose: PatchOptions lives in @deepseek-ai/dsh-app-boot
 * and tracking its exact shape here would couple us to internals we don't use.
 */
export function cloudProfilePatches(): { id: string, disabled?: boolean, config?: object }[] {
  return [
    {
      // OpenAI-compatible chat completions -> our gateway. The user token is
      // the "API key"; the upstream provider key never reaches this machine.
      id: 'llm-deepseek',
      config: {
        baseURL: `${CLOUD_BASE}/llm/v1`,
        apiKeyEnv: CLOUD_TOKEN_ENV,
      },
    },
    {
      // web_search speaks Anthropic Messages on a SEPARATE endpoint; without
      // this row it would leak to the official API with a useless token.
      id: 'web-search-deepseek',
      config: {
        baseURL: `${CLOUD_BASE}/llm/anthropic/v1`,
        apiKeyEnv: CLOUD_TOKEN_ENV,
      },
    },
    {
      // Upstream's telemetry defaults to DISABLED; pin the row off so a future
      // upstream default flip cannot ship user session events to a collector.
      id: 'session-telemetry-otel',
      disabled: true,
    },
  ]
}
