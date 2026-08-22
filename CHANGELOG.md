# Changelog

English | [简体中文](CHANGELOG.zh-CN.md)

All notable changes to this project will be documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Release history
will be added only from verified tags and release metadata.

## [Unreleased]

### Added

### Changed

### Fixed

### Security

## [0.2.0] - 2026-08-21

### Added

- Version-locked npm/npx and Python/uv lifecycle installers for the canonical
  self-host stack.
- Docker Compose source-build, PostgreSQL, workspace, health, readiness, and
  release metadata contracts.
- English and Simplified Chinese documentation paths for maintained guides.

### Changed

- Adopted DSH Cloud Community License 1.0 and propagated its identifier through
  package and OCI metadata.
- Hardened account, webhook, request-body, streaming billing, preview, container,
  and deployment boundaries.
- Standardized release `0.2.0` across server, desktop assembly, CLIs, and images.

### Fixed

- Corrected self-host image/config version drift, persistent-volume upgrades,
  PostgreSQL 18 storage, first-account setup, and CLI lifecycle state handling.
- Made Python wheel and source archives reproducible and publish-ready.

[0.2.0]: https://github.com/AgentsDanceAI/deepseek-harness-cloud/releases/tag/v0.2.0
