## Summary

Describe the user or operator problem, the chosen change, and what remains out of scope.

## Verification

List the exact commands and results. Include a minimal manual check when automated coverage is not practical.

```text
command:
result:
```

## Compatibility and operations

Describe API, schema, configuration, migration, deployment, observability, security, privacy, and rollback effects. Write `None` where an item does not apply.

## Edition and provenance

Identify affected Community baseline paths and any Hosted/Enterprise extension interface. List copied, generated, or third-party material with its source, revision, license, and notice requirements.

## Visual changes

For UI changes, include reviewed before/after screenshots with synthetic data and accessible-state notes. Otherwise write `None`.

## Checklist

- [ ] I ran the focused tests and recorded the commands below.
- [ ] I disclosed copied, generated, or third-party material and its provenance.
- [ ] I signed every commit with `Signed-off-by`.
- [ ] I checked the Community baseline and edition boundary.
- [ ] I removed secrets, personal data, customer data, and environment-specific operations detail.
- [ ] I did not change a public license, production system, or public Git history without a separate approval.
- [ ] I updated documentation and the changelog when user-visible behavior changed.
- [ ] I added migration and rollback notes for persisted-data or configuration changes.
