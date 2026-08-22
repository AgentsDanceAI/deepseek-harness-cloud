# Release checklist

This checklist prepares evidence; it does not authorize publication or
deployment.

## Identity and scope

- [ ] Release version and source revision come from the repository's canonical
      release metadata and agree across server, container, npm, Python, and docs.
- [ ] Changelog entries correspond to verified changes and do not reconstruct
      unsupported history.
- [ ] Current license state, notices, source archive, and edition boundary have
      been reviewed.
- [ ] The release tree contains no credentials, personal/customer data, private
      operations material, or generated build debris.

## Verification

- [ ] Server tests, JavaScript tests, lint/format checks, and documentation checks
      pass from a clean checkout.
- [ ] Docker and Compose smoke tests pass on supported architectures.
- [ ] npm/npx and uv/uvx packages pass equivalent contract fixtures and dry runs.
- [ ] Database upgrade, backup, restore, and rollback are exercised against
      disposable data.
- [ ] SBOM, provenance, checksums, signatures, and artifact-to-source revision
      mapping are generated and verified.
- [ ] Security review covers dependency changes, request boundaries, secrets,
      authentication, authorization, webhooks, previews, and workspaces.

## Publication gate

- [ ] Registry namespaces, OIDC/trusted publisher configuration, protected
      environments, maintainers, and recovery contacts are independently verified.
- [ ] An authorized release owner approves the exact immutable version, artifact
      hashes/digests, destinations, and rollback/revocation procedure.
- [ ] Floating tags or channels move only after immutable artifacts pass consumer
      verification.
- [ ] Publishing does not deploy or change production; a production rollout needs
      its own approval, backup, canary, observation, and rollback evidence.
