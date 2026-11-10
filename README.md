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

Add `spiffe` to the `dependencies` in your `manifest.json`, then import from the package. As a
sibling custom integration that is `from ..spiffe import ...`; `from custom_components.spiffe
import ...` works too.

**This is a stable contract from 0.2.0.** It follows [semantic
versioning](https://semver.org/spec/v2.0.0.html): what is documented here only changes in a major
version. That covers the Python API (names, signatures and exceptions), the shape of the
`spiffe_webhook` event, the fields and response of `spiffe.http_request`, the response of the
whoami endpoint, and the option keys. Anything not documented here, including the module layout
inside the package, may change in any release.

| Helper | What it does |
|---|---|
| `async_fetch_jwt_svid(hass, audience, subject=None)` | a JWT-SVID for this audience (or list of audiences), as a string |
| `async_validate_jwt_svid(hass, token, audience)` | checks a token someone sent to Home Assistant and returns its SPIFFE ID. It does not stop a token from being used twice |
| `async_verify_request(hass, request, *, allowed_callers)` | authenticates an incoming `aiohttp` request and returns the caller's SPIFFE ID (see below) |
| `async_register_verified_webhook(hass, domain, name, webhook_id, handler, *, allowed_callers, allowed_methods=("POST",))` | registers a webhook that only runs your handler for a verified caller (see below) |
| `async_get_jwt_bundles(hass)` | a copy of the trust bundles (public keys), refreshed every minute; refused when stale, like validation |
| `async_x509_available(hass)` | whether the agent offered X.509-SVIDs at setup |

`allowed_callers` is a collection of SPIFFE IDs. A bare string raises `TypeError` and an entry that
is not a SPIFFE ID of a workload raises `ValueError`, when you register and when you verify.

Errors: `SpiffeNotReady` while the integration is not loaded (raise `ConfigEntryNotReady` in your
own setup when you get it), `SpiffeError` when the agent cannot provide a token or a token does not
validate, and `SpiffeAuthError` (a `SpiffeError` with a `status` of 401 or 403) when a request is
not authenticated or not allowed.

Validation refuses to use bundles that could not be refreshed for 15 minutes, so a key the issuer
withdrew is not trusted for as long as the agent is unreachable.

Tokens live for minutes. Fetch one when you need it rather than storing it, and never put one in
an entity, an attribute, a service response or a log line.

Use an audience that names the one service you are calling, and have that service check the
audience, the exact SPIFFE ID it expects, and the expiry.

### Receiving calls

To accept calls from other workloads, first set Home Assistant's own SPIFFE ID in the
integration's options (**Configure**), for example `spiffe://example.org/ha/core`. It is the
audience every incoming token must name. While it is empty, a request that carries a bearer token
is answered with 503.

`async_verify_request` needs `Authorization: Bearer <JWT-SVID>` whose audience is exactly that one
SPIFFE ID, and answers as follows:

- no bearer token, or an empty one (checked before anything else, so a request without a bearer
  token learns nothing about readiness), an invalid or expired token, the wrong audience or an
  unknown signer: `SpiffeAuthError` with status 401;
- a valid token whose subject is not in `allowed_callers`: status 403 (an empty list admits nobody);
- with the option **Accept each token only once** on, a token that was already accepted: status
  401 (see the security model; it is off by default);
- no SPIFFE ID configured, integration not loaded, bundles too old, or (single-use mode only) the
  memory of used tokens full: `SpiffeNotReady` (answer 503).

`async_register_verified_webhook` wraps Home Assistant's webhook registration (it is not limited to
the local network, because the token is the authentication). The handler runs only for a verified
caller. Failures answer a JSON body such as `{"error": "unauthorized"}` with 401, 403 or 503, never
with token contents or exception text. Unregister with
`homeassistant.components.webhook.async_unregister(hass, webhook_id)`. It raises `ValueError` when
the webhook ID is already registered, and for an invalid `allowed_callers` or method.

An integration that lets one workload push data to Home Assistant:

```json
{ "domain": "orders_bridge", "dependencies": ["spiffe", "webhook"], "...": "..." }
```

```python
import json
from functools import partial

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant

from ..spiffe import async_register_verified_webhook

ORDERS_API = "spiffe://example.org/orders/api"


MAX_BODY = 64 * 1024


async def _handle(
    hass: HomeAssistant, webhook_id: str, request: web.Request, caller: str
) -> web.Response | None:
    if (request.content_length or 0) > MAX_BODY:
        return web.json_response({"error": "too_large"}, status=413)
    try:
        # Reads at most MAX_BODY + 1 bytes, in case the length was not declared.
        order = json.loads(await request.content.read(MAX_BODY + 1))
    except ValueError:
        return web.json_response({"error": "invalid_json"}, status=400)
    hass.bus.async_fire("orders_bridge_order", {"caller": caller, "order": order})
    return web.json_response({"ok": True})


async def async_setup_entry(hass, entry):
    webhook_id = entry.data["webhook_id"]
    async_register_verified_webhook(
        hass,
        "orders_bridge",
        "Orders bridge",
        webhook_id,
        _handle,
        allowed_callers=[ORDERS_API],
    )
    entry.async_on_unload(partial(webhook.async_unregister, hass, webhook_id))
    return True
```

The webhook is not unregistered for you: do it on unload, as above. The caller sends
`POST /api/webhook/<id>` with an unexpired JWT-SVID whose audience is Home Assistant's SPIFFE
ID.

## Event webhooks

For automations, you do not need to write an integration. On the SPIFFE entry under
**Settings → Devices & services**, use **Add event webhook** to create a subentry with a unique
name, a webhook ID (a random one is suggested) and the SPIFFE IDs allowed to call it (at least
one). Each verified `POST` to `/api/webhook/<id>` with a JSON body fires the event
`spiffe_webhook`:

```yaml
event_type: spiffe_webhook
data:
  webhook_id: "..."
  name: Orders
  caller: spiffe://example.org/orders/api
  data: { ... }   # the parsed JSON body, null when the body is empty
```

The webhook answers `200 {"ok": true}`, `400` when the body is not JSON and `413` when it is
larger than 256 KiB. Trigger an automation with an event trigger on `spiffe_webhook` and match on
`trigger.event.data.webhook_id` together with `trigger.event.data.caller`. Home Assistant
administrators can fire `spiffe_webhook` by hand with any caller, so the event is only as
trustworthy as they are. Keep payloads small: the recorder does not store event data over 32 KiB
(it stores an empty object and logs a warning), so such an event is missing from the history.
If another integration already uses an event webhook's ID, the webhook is not registered and a
repair issue says so.

## Calling out: `spiffe.http_request`

The action `spiffe.http_request` calls a URL with a JWT-SVID for the audience you name, for
automations and scripts that cannot use Python. Fields: `url` (http or https), `audience` (a SPIFFE
ID), `method` (default `POST`), `json` (an optional object), `headers` (optional, `Authorization`
cannot be set) and `timeout` (seconds, default 10). It returns the response:

```yaml
action: spiffe.http_request
data:
  url: https://api.example.org/hook
  audience: spiffe://example.org/orders/api
  json: { night: 3 }
response_variable: reply   # {"status": 200, "json": {...}} or {"status": 200, "text": "..."}
```

`json` is returned when the response is JSON, otherwise `text`, cut at 64 KiB. Redirects are not
followed. Network failures raise an error.

**It is admin-only.** The action hands Home Assistant's identity token to whatever server the URL
points at, so letting a non-administrator pick the URL or the audience would let them collect
tokens that others trust. Do not expose it to untrusted users through scripts either: a script
that runs it with a user-supplied URL has the same problem.

## Whoami

When **Offer the whoami endpoint** is enabled in the options, `POST /api/spiffe/whoami` with a
bearer JWT-SVID runs the full check (signature, audience and expiry) for any caller, and answers
`{"caller": "<spiffe id>", "audience": "<Home Assistant's spiffe id>"}`. It never uses up a token,
even in single-use mode. It is for checking that a new workload is set up correctly. It is off by
default and, while it is off, answers 404 to every method the view defines (the web server
itself answers OPTIONS and TRACE).

## Entities

One service device, **SPIFFE Workload API**, with diagnostic entities:

- **Workload API**: connectivity, from a check every minute that also refreshes the trust bundles.
- **Trust bundle keys**: how many signing keys the bundles hold, with the trust domains as an
  attribute.
- **X.509-SVIDs available**.
- **Last JWT-SVID fetch** (disabled by default).

Repair issues tell you when something needs attention:

- the Workload API has not answered for five minutes;
- it answers, but has no identity for Home Assistant at all, for example a SPIRE agent without a
  registration entry for it;
- an integration asked for a token and was refused, usually because Home Assistant has not been
  admitted at the issuer yet, or the audience is not allowed.

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

Incoming calls are only as trustworthy as your trust domain's keys: Home Assistant accepts any
token signed by a key in the bundles it holds, from the callers you allow.

- A request must be for Home Assistant alone: the token's audience must be exactly Home
  Assistant's SPIFFE ID, not a list that names other services too. (`async_validate_jwt_svid`
  keeps its 0.1.0 behavior and accepts a token that lists more audiences.)
- Tokens are bearer tokens: whoever holds one can use it until it expires, typically 5 minutes.
  What protects them is TLS between the caller and Home Assistant, the short lifetime, the exact
  audience rule (a token minted for another service cannot be replayed to Home Assistant) and the
  allowlist of callers. Home Assistant does not remember tokens by default, so a captured token
  can be used again until it expires.
- Optionally, turn on **Accept each token only once** in the options to refuse a token that was
  already accepted, on any verified endpoint. Home Assistant then remembers accepted tokens in
  memory, by the SHA-256 of their header and payload (so a variant with a rewritten signature
  counts as the same token), until they expire. A token is used up as soon as it passes the check,
  even if the handler then fails or the body is refused (400, 413). The memory is lost on a
  restart, and when it is full of live tokens requests are answered 503 rather than forgetting
  one. **Only enable this if your issuer mints a fresh token for every fetch.** Agents that cache
  tokens, such as SPIRE and spiffex, hand out the identical token for repeated fetches, and their
  callers' repeated calls would be refused. An issuer that does mint fresh tokens should put a
  unique `jti` in each, or two tokens issued in the same second look like one.
- `http_request` over plain `http://` sends the token in cleartext. Use `https://`.
- The `spiffe_webhook` event is only as trustworthy as Home Assistant's administrators: an admin
  can fire it by hand with any `caller`. In automations, match on `webhook_id` together with
  `caller`, not on the event type alone.

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
