# SPIFFE for Home Assistant

Give your Home Assistant a [SPIFFE](https://spiffe.io) identity, so your integrations can call
other services with short-lived, signed tokens instead of long-lived API keys.

```python
from custom_components.spiffe import async_fetch_jwt_svid

token = await async_fetch_jwt_svid(hass, "https://api.example.org")
headers = {"Authorization": f"Bearer {token}"}
```

The service on the other side verifies the token against your trust domain's public keys and sees
who is calling, for example `spiffe://example.org/home-assistant`, without anyone having copied a
secret between the two.

## How it works

```
  issuer of your trust domain        agent (any SPIFFE implementation)         Home Assistant Core
 ┌──────────────────────────┐       ┌─────────────────────────────────┐      ┌───────────────────────┐
 │ SPIRE server, spiffex,   │◄─────►│ serves the SPIFFE Workload API  │◄────►│ this integration      │
 │ Teleport, ...            │       │ on a Unix socket                │      │ (py-spiffe)           │
 └──────────────────────────┘       └─────────────────────────────────┘      │   ▲ helpers           │
                                                                             │ your integrations     │
                                                                             └───────────────────────┘
```

This integration only speaks the standard
[SPIFFE Workload API](https://github.com/spiffe/spiffe/blob/main/standards/SPIFFE_Workload_API.md),
using the SPIFFE project's own Python library. It does not care which SPIFFE implementation serves
it. You bring the agent:

- **Home Assistant OS:** run the agent as an app (add-on) that serves its socket in the Home
  Assistant configuration folder, by default at `.spiffe/agent.sock`, which Core sees as
  `/config/.spiffe/agent.sock`. For example, the
  [spiffex agent app](https://github.com/PeterSR/homeassistant-spiffex-addon) does this. An app
  for another implementation only has to serve the Workload API at a path Core can see.
- **Home Assistant Container or Core:** run your usual agent on the host, mount its socket into
  the container, and either set `SPIFFE_ENDPOINT_SOCKET` or enter the address when setting up.

The integration uses JWT-SVIDs. It notices whether the agent also offers X.509-SVIDs, for mutual
TLS helpers planned for a later version.

## Installation

1. In HACS, add this repository as a custom repository of type *Integration*, then install
   **SPIFFE**. Or copy `custom_components/spiffe` into your configuration folder by hand.
2. Restart Home Assistant.
3. Start your agent, then go to **Settings → Devices & services → Add integration → SPIFFE** and
   enter the Workload API address, such as `unix:///config/.spiffe/agent.sock`.

Setting up checks that the agent answers. If the agent is not running yet, Home Assistant keeps
retrying in the background.

## Using it from your integration

Add `spiffe` to the `dependencies` in your `manifest.json`, then:

| Helper | What it does |
|---|---|
| `async_fetch_jwt_svid(hass, audience, subject=None)` | a JWT-SVID for this audience (or list of audiences), as a string |
| `async_validate_jwt_svid(hass, token, audience)` | checks a token someone sent to Home Assistant and returns its SPIFFE ID |
| `async_get_jwt_bundles(hass)` | a copy of the trust bundles (public keys), refreshed every minute |
| `async_x509_available(hass)` | whether the agent offered X.509-SVIDs at setup |

Validation refuses to use bundles that could not be refreshed for 15 minutes, so a key the issuer
withdrew is not trusted for as long as the agent is unreachable.

They raise `SpiffeNotReady` while the integration is not loaded (raise `ConfigEntryNotReady` in
your own setup when you get it), and `SpiffeError` when the agent cannot provide a token or a token
does not validate.

Tokens live for minutes. Fetch one when you need it rather than storing it, and never put one in
an entity, an attribute, a service response or a log line.

Use an audience that names the one service you are calling, and have that service check the
audience, the exact SPIFFE ID it expects, and the expiry.

## Entities

One service device, **SPIFFE Workload API**, with diagnostic entities:

- **Workload API**: connectivity, from a check every minute that also refreshes the trust bundles.
- **Trust bundle keys**: how many signing keys the bundles hold, with the trust domains as an
  attribute.
- **X.509-SVIDs available**.
- **Last JWT-SVID fetch** (disabled by default).

If the Workload API stops answering for five minutes, a repair issue tells you so. Another one
appears when an integration asks for a token and the Workload API refuses it, usually because Home
Assistant has not been admitted at the issuer yet, or the audience is not allowed.

## Security model

Home Assistant Core is a single process. Every integration running in it, and anything else that
can reach the socket, can obtain tokens as Home Assistant's identity. On Home Assistant OS that
includes apps that map the configuration folder, such as file editors, terminals and Samba. That
is no weaker than today: write access to the configuration folder already means full control of
Home Assistant. Still:

- ask your issuer to limit the audiences Home Assistant's identity may request, if it can;
- give each service you call its own audience;
- have every service check the audience, the SPIFFE ID and the expiry of each token.

The agent decides who Home Assistant is. An agent running in another container cannot reliably
tell which process connects to it (in practice, not without turning off the app's protection
mode), so whoever can reach the socket gets the identity, whichever implementation you use.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Development

```bash
uv sync --group test
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

`tests/test_e2e_spiffex.py` runs the Workload API code against a real agent. It uses spiffex
because `spiffex dev` sets up a whole local trust domain in one command, and is skipped unless
`SPIFFEX` names a spiffex binary:

```bash
SPIFFEX=$(command -v spiffex) uv run pytest tests/test_e2e_spiffex.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
