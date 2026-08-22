# dsh-plugin-cloud

[简体中文](./README.zh-CN.md)

Connect a **stock DeepSeek Harness** install to [DSH Cloud](https://dshcloud.online):
device login plus a gateway provider serving **20 models** through one endpoint —
no upstream API key of your own on any client.

> **Honest disclosure:** DSH Cloud is a commercial hosted service by AgentsDance AI.
> Self-hosting the whole platform is free and open —
> [deepseek-harness-cloud](https://github.com/AgentsDanceAI/deepseek-harness-cloud).
> New hosted accounts include **500 free credits**. This plugin also works against a
> self-hosted deployment via `DSH_CLOUD_BASE`.

## Setup (one command)

```bash
npx --yes dsh-plugin-cloud setup
```

This opens the browser for device approval (RFC 8628-style, the same flow the
DSH Cloud desktop app uses), fetches the live model catalog, and writes two rows
into `$DSH_HOME/cordis.patch.yml` — the user-owned config layer:

- `dsh-plugin-cloud` — the runtime plugin (exports your token as `DSH_CLOUD_TOKEN`)
- `dsh-cloud-models` — a dedicated `@deepseek-ai/dsh-llm-pi-ai` instance with the
  gateway provider. **Your own `llm-pi-ai` row is never touched.**

Restart DeepSeek Harness and pick a model under **DSH Cloud** (e.g.
`deepseek-v4-pro` with its 1M-token context window). Re-run `setup` any time to
refresh the catalog; if the file already exists it is backed up first.

Alternatively, register the plugin through the bundle mechanism and log in
separately:

```bash
dsh add dsh-plugin-cloud
npx --yes dsh-plugin-cloud login
```

## Commands

| Command | Effect |
|---|---|
| `setup` | login if needed + fetch catalog + write config rows |
| `login` | device login only |
| `status` | show session, service URL and target paths |
| `logout` | delete the locally stored token |

## Security notes

- The token is stored at `$DSH_HOME/dsh-cloud-auth.json` (mode 600) and exported
  as `DSH_CLOUD_TOKEN` — a name that matches upstream's sensitive-env pattern,
  so dsh scrubs it from every spawned subprocess (bash tool, MCP servers).
- Revoke a device any time from the DSH Cloud console; revocation kills the token.
- This package only ever rewrites rows it owns (`dsh-plugin-cloud`,
  `dsh-cloud-models`) and backs up your patch file before writing.

## Uninstall

```bash
npx --yes dsh-plugin-cloud logout
```

Then delete the two rows named above from `$DSH_HOME/cordis.patch.yml` (or
restore the `.bak-*` backup).

## License

Apache-2.0 (this connector only). The DSH Cloud platform is licensed separately —
see the [repository](https://github.com/AgentsDanceAI/deepseek-harness-cloud).
