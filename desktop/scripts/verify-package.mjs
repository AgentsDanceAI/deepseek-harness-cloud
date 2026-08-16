/**
 * Post-packaging guard: assert the login-wall assets actually shipped inside
 * the packaged app.
 *
 * Why this exists as its own step: electron-builder's `files` is an explicit
 * ALLOW-LIST, and anything unlisted is dropped SILENTLY — no warning, no error,
 * a green build. Version 2.0.0 shipped exactly that way: `build/cloud/login.html`
 * was present in the assembled tree (so verify-contract passed) but absent from
 * the DMG, so the login window loaded a missing file and every user saw a blank
 * white window. Checking the source tree cannot catch that; only the artifact can.
 *
 *   node desktop/scripts/verify-package.mjs <packaging-output-dir>
 *
 * Passes when each required asset is found either unpacked on disk (asarUnpack
 * copies `build/**` out) or listed in an app.asar header.
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

const REQUIRED = ['login.html', 'login-preload.cjs']
const outDir = process.argv[2]
if (!outDir || !existsSync(outDir)) {
  console.error(`verify-package: output dir not found: ${outDir}`)
  process.exit(1)
}

/** Every file path under `dir`, skipping nothing — packaged trees are small enough. */
function* walk(dir) {
  let entries
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return
  }
  for (const entry of entries) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) yield* walk(path)
    else if (entry.isFile()) yield path
  }
}

const asars = []
const unpacked = new Set()
for (const path of walk(outDir)) {
  if (path.endsWith('app.asar')) asars.push(path)
  const marker = path.replace(/\\/g, '/')
  if (marker.includes('/build/cloud/')) unpacked.add(marker.split('/build/cloud/')[1])
}

/** Asset names named in any app.asar header (the header is plain JSON text). */
const inAsar = new Set()
for (const asar of asars) {
  const head = readFileSync(asar).subarray(0, Math.min(statSync(asar).size, 1 << 22)).toString('latin1')
  for (const name of REQUIRED) if (head.includes(name)) inAsar.add(name)
}

const missing = REQUIRED.filter(name => !unpacked.has(name) && !inAsar.has(name))
console.log(`verify-package: scanned ${asars.length} asar(s) under ${outDir}`)
console.log(`  unpacked build/cloud: ${[...unpacked].join(', ') || '(none)'}`)
console.log(`  named inside asar:    ${[...inAsar].join(', ') || '(none)'}`)
if (missing.length > 0) {
  console.error('verify-package: FAILED — these login-wall assets did not ship: '
    + missing.join(', '))
  console.error('  electron-builder `files` is an allow-list; ensure build/cloud/** is listed '
    + '(assemble.mjs registers it) and rerun packaging.')
  process.exit(1)
}
console.log('verify-package: login-wall assets present in the packaged app')
