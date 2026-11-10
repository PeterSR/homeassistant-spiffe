# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-10

### Added

- First version: connect to any SPIFFE Workload API on a Unix socket, and give other custom
  integrations `async_fetch_jwt_svid`, `async_validate_jwt_svid`, `async_get_jwt_bundles` and
  `async_x509_available`.
- Diagnostic entities for the Workload API's health, the trust bundles and X.509 support.
- Repair issues when the Workload API has not answered for five minutes, when it has no identity
  for Home Assistant at all, and when an integration's token request is refused.

[Unreleased]: https://github.com/PeterSR/homeassistant-spiffe/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/PeterSR/homeassistant-spiffe/releases/tag/v0.1.0
