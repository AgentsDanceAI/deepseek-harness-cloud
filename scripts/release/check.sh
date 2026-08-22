#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo"
node scripts/release/validate-release.mjs
node --test scripts/release/test/*.test.mjs
"${PYTHON:-server/.venv/bin/python}" -m pytest tests/deploy tests/distribution -q
