# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - Unreleased

### Added

- Receive calls from other workloads: `async_verify_request` authenticates a request carrying a
  JWT-SVID (exactly Home Assistant's audience, and allowed callers), and
  `async_register_verified_webhook` registers a webhook that only runs for a verified caller. New
  error `SpiffeAuthError`. The public Python API is now a stable contract under semantic
  versioning.
- Options: Home Assistant's own SPIFFE ID (the audience of incoming tokens), an opt-in
  `POST /api/spiffe/whoami` endpoint, and an opt-in single-use mode that refuses a token already
  accepted. It is off by default, because agents that cache tokens (SPIRE, spiffex) hand out the
  same token repeatedly.
- Event webhook subentries: a verified request fires the `spiffe_webhook` event.
- A repair issue when an event webhook cannot be registered because its ID is already in use.
- Admin-only action `spiffe.http_request` to call a URL with a JWT-SVID for an audience.

## [0.1.0] - 2026-11-10

### Added

- First version: connect to any SPIFFE Workload API on a Unix socket, and give other custom
  integrations `async_fetch_jwt_svid`, `async_validate_jwt_svid`, `async_get_jwt_bundles` and
  `async_x509_available`.
- Diagnostic entities for the Workload API's health, the trust bundles and X.509 support.
- Repair issues when the Workload API has not answered for five minutes, when it has no identity
  for Home Assistant at all, and when an integration's token request is refused.

[Unreleased]: https://github.com/PeterSR/homeassistant-spiffe/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/PeterSR/homeassistant-spiffe/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/PeterSR/homeassistant-spiffe/releases/tag/v0.1.0
