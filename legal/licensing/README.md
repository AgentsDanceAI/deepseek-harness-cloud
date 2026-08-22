# Licensing records

This directory records the project's current license state and the gates for any
future license evaluation. It does not contain a replacement license.

| File | Purpose |
|---|---|
| `LICENSE_STATE.toml` | Machine-readable statement that AGPL-3.0-only is active and transition gates are closed |
| `COMMUNITY_BASELINE.manifest` | Immutable tree inventory used to prevent retroactive reclassification of published Community capabilities |
| `COPYRIGHT_INVENTORY.csv` | Review ledger for ownership and third-party provenance; incomplete evidence keeps transition blocked |
| `FUTURE_SOURCE_AVAILABLE_REQUIREMENTS.md` | Non-operative product requirements for counsel to evaluate |
| `COUNSEL_REVIEW_CHECKLIST.md` | Evidence checklist that must be completed before any prospective change |

The repository-level explanation is [LICENSING.md](../../LICENSING.md). The
operative current license remains [LICENSE](../../LICENSE).

## Updating records

Do not infer ownership from a commit author, repository location, employment
assumption, or generated-file header. Each verified inventory entry needs a
durable evidence identifier. Confidential agreements stay in an access-controlled
record system; the public inventory records only a non-secret ID, reviewer, date,
and conclusion.

Never mark a transition gate complete in the same change that supplies its own
legal approval. Final license text, authority to relicense, contribution terms,
third-party compatibility, edition boundary, and effective version require
independent review.
