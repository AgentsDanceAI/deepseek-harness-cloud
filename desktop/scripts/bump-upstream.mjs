#!/usr/bin/env node
/** Move the upstream pins and rehearse the overlay against the new tree.
 *
 *   node desktop/scripts/bump-upstream.mjs --desktop-commit <sha> \
 *     [--runtime <version>] [--harness-commit <sha>]
 *
 * Updates desktop/upstream.json, clones the new pin into a throwaway dir, and
 * checks every patch with `git apply --check`. Conflicting patches are listed;
 * regenerate them against the new tree (edit + git diff, same as they were
 * born) and re-run. Finish with assemble.mjs + yarn install +
 * verify-contract.mjs before committing the bump.
 */
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const desktopDir = resolve(here, '..')
const upstreamPath = join(desktopDir, 'upstream.json')
const upstream = JSON.parse(readFileSync(upstreamPath, 'utf8'))

const args = process.argv.slice(2)
const opt = (name) => {
  const index = args.indexOf(name)
  return index >= 0 ? args[index + 1] : undefined
}
const desktopCommit = opt('--desktop-commit')
if (desktopCommit === undefined) {
  console.error('bump-upstream: --desktop-commit <sha> is required')
  process.exit(2)
}

const next = {
  ...upstream,
  desktopCommit,
  ...opt('--runtime') === undefined ? {} : { runtimePackageVersion: opt('--runtime') },
  ...opt('--harness-commit') === undefined ? {} : { harnessCommit: opt('--harness-commit') },
}

const scratch = mkdtempSync(join(tmpdir(), 'dshcloud-bump-'))
try {
  execFileSync('git', ['clone', '--no-checkout', next.desktopRepository, scratch], { stdio: 'inherit' })
  execFileSync('git', ['checkout', '--quiet', desktopCommit], { cwd: scratch, stdio: 'inherit' })

  const patchDir = join(desktopDir, 'patches')
  const patches = readdirSync(patchDir).filter(name => name.endsWith('.patch')).sort()
  const conflicts = []
  for (const patch of patches) {
    try {
      execFileSync('git', ['apply', '--check', join(patchDir, patch)], { cwd: scratch })
      // apply for real so later patches are checked against the combined tree
      execFileSync('git', ['apply', join(patchDir, patch)], { cwd: scratch })
      console.log(`bump-upstream: OK ${patch}`)
    } catch {
      conflicts.push(patch)
      console.error(`bump-upstream: CONFLICT ${patch}`)
    }
  }

  const pinned = JSON.parse(readFileSync(join(scratch, 'upstream.json'), 'utf8'))
  if (pinned.runtimePackageVersion !== next.runtimePackageVersion) {
    console.warn(`bump-upstream: NOTE upstream now pins runtime family ${pinned.runtimePackageVersion}; `
      + `ours says ${next.runtimePackageVersion} — pass --runtime to follow`)
  }
  if (pinned.commit !== undefined && pinned.commit !== next.harnessCommit) {
    console.warn(`bump-upstream: NOTE upstream pins dsh source ${String(pinned.commit).slice(0, 10)}; `
      + 'pass --harness-commit to follow')
  }

  if (conflicts.length > 0) {
    console.error(`\nbump-upstream: ${conflicts.length} patch(es) need regeneration: ${conflicts.join(', ')}`)
    console.error('upstream.json NOT updated. Regenerate the conflicting patches against the new tree, then re-run.')
    process.exit(1)
  }

  writeFileSync(upstreamPath, `${JSON.stringify(next, null, 2)}\n`)
  console.log(`\nbump-upstream: pins updated -> ${desktopCommit.slice(0, 10)} `
    + `(runtime ${next.runtimePackageVersion})`)
  console.log('Next: node desktop/scripts/assemble.mjs && yarn install && verify-contract.mjs, then commit.')
} finally {
  rmSync(scratch, { recursive: true, force: true })
}
