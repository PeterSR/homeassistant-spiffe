# Security policy

This integration hands out Home Assistant's identity to other integrations, so security reports get
priority.

## Reporting a vulnerability

Please report vulnerabilities **privately** through GitHub's private vulnerability reporting: the
**Security** tab of [PeterSR/homeassistant-spiffe](https://github.com/PeterSR/homeassistant-spiffe),
then *Report a vulnerability*. If that form is unavailable, use the contact options on the
maintainer's [GitHub profile](https://github.com/PeterSR) to arrange a private channel. Do not open
a public issue for anything you believe is a security problem.

You can expect an acknowledgement within a few days.

## Scope

In scope:

- a token, or anything that would let someone use one, reaching an entity, attribute, service
  response, trace, log line or diagnostics download;
- `async_validate_jwt_svid` accepting a token it should reject;
- the integration talking to anything other than the Workload API address it was configured with.

Not vulnerabilities of this integration, by design:

- anything that can reach the agent's socket, including every integration in Home Assistant Core
  and apps that map the configuration folder, obtaining Home Assistant's identity. See the security
  model in the README.
- flaws in the agent or issuer you run. Report those to their projects.

## Supported versions

Before 1.0, security fixes target the latest release only.
