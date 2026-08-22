# Contributing to DSH Cloud

English | [简体中文](CONTRIBUTING.zh-CN.md)

Thank you for improving DSH Cloud. Small, focused pull requests with tests and
clear provenance are easiest to review.

By participating, you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Do not report vulnerabilities in a public
issue; follow [SECURITY.md](SECURITY.md).

## Before opening a change

1. Search existing issues and pull requests.
2. Use the bug or feature issue form for a change that needs design agreement.
3. Keep unrelated refactors out of the same pull request.
4. For edition-sensitive work, complete the checklist in
   [docs/editions.md](docs/editions.md) before implementation.

## Local setup

The server requires Python 3.11 or newer. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e './server[dev]'
DHC_DEV=1 AUTH_SECRET=local-development-secret \
  .venv/bin/python -m uvicorn app.main:app --app-dir server --reload
```

Equivalent `uv` setup:

```bash
uv sync --project server --extra dev
DHC_DEV=1 AUTH_SECRET=local-development-secret \
  uv run --project server uvicorn app.main:app --app-dir server --reload
```

Use only disposable local data and placeholder credentials. Never copy a
production environment file, database, support bundle, or user record into a
test or issue.

## Tests and checks

Run the smallest relevant test first, then the broader checks for the area you
changed:

```bash
uv run --project server pytest server/tests -q
uv run --project server ruff check server/app server/tests
node --test server/tests/js/*.test.mjs
git diff --check
```

If you do not have `uv`, use the virtual environment created above:

```bash
.venv/bin/python -m pytest server/tests -q
.venv/bin/python -m ruff check server/app server/tests
```

Record the exact commands and results in the pull request. A documentation-only
change still requires `git diff --check` and a review of every changed link and
command.

## Commit identity and DCO sign-off

Confirm that Git records the public identity you intend to publish:

```bash
git config user.name
git config user.email
git var GIT_AUTHOR_IDENT
```

Sign off each commit with the Developer Certificate of Origin attestation:

```bash
git commit -s
```

The resulting `Signed-off-by` line certifies the origin of the contribution. It
does not override the DSH Cloud Community License, transfer trademarks, or by
itself authorize a license change. Prefer a GitHub noreply address if you do not want a personal
mailbox published permanently in Git history.

## Provenance and generated material

Disclose all copied, adapted, generated, or third-party material in the pull
request. Include its source URL, exact revision or version, license, how it was
produced, and any required notice. Do not paste code merely because an AI tool
produced it; you remain responsible for provenance, correctness, security, and
license compatibility.

## Pull request expectations

- Keep APIs and stored data backward-compatible unless a reviewed migration says
  otherwise.
- Add or update tests for behavioral changes.
- Update user and operator documentation with the implementation.
- Redact credentials, personal data, customer data, private topology, costs, and
  incident details from code, fixtures, screenshots, logs, and commit messages.
- Explain schema, configuration, deployment, security, and rollback effects.
- Do not change a public license, production service, package registry, or public
  Git history without a separate, explicit authorization.
