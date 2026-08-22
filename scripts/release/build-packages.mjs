#!/usr/bin/env node
import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { manifestFromRelease } from './validate-release.mjs'

const root = resolve(new URL('../..', import.meta.url).pathname)
const output = resolve(process.argv[2] ?? 'dist/packages')
const npmOutput = resolve(output, 'npm')
const pythonOutput = resolve(output, 'python')
const release = JSON.parse(await readFile(resolve(root, 'release/release.json'), 'utf8'))
const manifest = manifestFromRelease(release)

function sourceFilter(source) {
  const normalized = source.replaceAll('\\', '/')
  const segments = new Set(normalized.split('/'))
  return !['node_modules', '.venv', '__pycache__', '.pytest_cache'].some(name => segments.has(name))
    && !normalized.includes('/test/')
    && !normalized.includes('/tests/')
    && !normalized.endsWith('.pyc')
}

await rm(output, { recursive: true, force: true })
await mkdir(output, { recursive: true })
await cp(resolve(root, 'packages/cli-npm'), npmOutput, { recursive: true, filter: sourceFilter })
await cp(resolve(root, 'packages/cli-python'), pythonOutput, { recursive: true, filter: sourceFilter })
await cp(resolve(root, 'LICENSE'), resolve(npmOutput, 'LICENSE'))
await cp(resolve(root, 'LICENSE'), resolve(pythonOutput, 'LICENSE'))

for (const destination of [resolve(npmOutput, 'templates'), resolve(pythonOutput, 'src/dsh_cloud_cli/templates')]) {
  for (const name of ['docker-compose.yml', 'Caddyfile', 'compose.build.yml', 'compose.postgres.yml']) {
    await cp(resolve(root, 'deploy/selfhost', name), resolve(destination, name))
  }
  await cp(resolve(root, 'server/config'), resolve(destination, 'config'), { recursive: true })
}

const manifestText = `${JSON.stringify(manifest, null, 2)}\n`
await writeFile(resolve(npmOutput, 'release-manifest.json'), manifestText)
await writeFile(resolve(pythonOutput, 'src/dsh_cloud_cli/release-manifest.json'), manifestText)

const npmPackage = JSON.parse(await readFile(resolve(npmOutput, 'package.json'), 'utf8'))
npmPackage.version = release.version
npmPackage.private = false
await writeFile(resolve(npmOutput, 'package.json'), `${JSON.stringify(npmPackage, null, 2)}\n`)

const pyprojectPath = resolve(pythonOutput, 'pyproject.toml')
const pyproject = await readFile(pyprojectPath, 'utf8')
await writeFile(pyprojectPath, pyproject.replace('version = "0.0.0.dev0"', `version = "${release.version}"`))

process.stdout.write(`${output}\n`)
