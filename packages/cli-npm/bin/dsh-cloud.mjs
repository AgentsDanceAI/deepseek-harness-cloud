#!/usr/bin/env node
import { CliError, execute, parseArgs } from '../src/cli.mjs'

try {
  const parsed = parseArgs(process.argv.slice(2))
  const result = await execute(parsed)
  if (result.text) process.stdout.write(result.text)
  if (result.json) process.stdout.write(`${JSON.stringify(result.json)}\n`)
} catch (error) {
  const cliError = error instanceof CliError ? error : new CliError(error?.message || 'unexpected CLI failure')
  if (process.argv.includes('--json')) process.stdout.write(`${JSON.stringify({ ok: false, error: cliError.message })}\n`)
  else process.stderr.write(`error: ${cliError.message}\n`)
  process.exitCode = cliError.exitCode
}
