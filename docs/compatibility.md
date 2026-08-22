# Upstream compatibility policy

English | [简体中文](compatibility.zh-CN.md)

DeepSeek Harness and its desktop application are developer-preview dependencies
that may introduce breaking changes. DSH Cloud does not maintain an upstream
source fork. Desktop customization is limited to small patches and a
self-contained cloud integration directory so upgrades remain auditable.

## Version sources

`desktop/upstream.json` pins the desktop and harness revisions. Runtime versions
are independently recorded in `release/release.json`:

- `harnessRuntime` is used by the workspace image;
- `desktopRuntime` is the runtime family supported by the pinned desktop tree.

They must not be forced to match. Raise `desktopRuntime` only after updating the
desktop pin and passing assembly, type checking, contract tests, and packaging.

## Assembly contract

`desktop/scripts/assemble.mjs` clones the exact upstream commit, applies the
ordered patches, copies the cloud integration, and runs guard checks. The output
is an ordinary upstream worktree built with upstream commands.

The maintained seams are the configured model/search rows, credential injection,
desktop profile patch ordering, Electron build metadata, update URLs, and the
documented patch anchors. Do not depend on private runtime internals or unexported
symbols.

## Upgrade procedure

```bash
node desktop/scripts/bump-upstream.mjs --desktop-commit <sha> --runtime <version>
node desktop/scripts/assemble.mjs
cd desktop/build/upstream
corepack enable && yarn install
node ../../scripts/verify-contract.mjs "$PWD"
yarn check
cd dsh-plugin-desktop && yarn build && yarn package:dir
```

If a patch no longer applies, reproduce the same narrow semantic change on the
new pin and regenerate that patch. A failed contract means an upstream seam has
changed and must be reviewed, not bypassed.

## Server compatibility buffer

The server exposes standard OpenAI- and Anthropic-compatible surfaces with
Bearer authentication. Client and server upgrades can therefore be staged
independently as long as the documented request, response, model, and `usage`
contracts remain compatible.
