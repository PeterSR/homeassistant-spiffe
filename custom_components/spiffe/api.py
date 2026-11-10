"""The public Python API: helpers other integrations call.

Everything here is re-exported from the package, so use `from ..spiffe import ...` (or
`from custom_components.spiffe import ...`) and never import this module directly.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Iterable

import jwt
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from spiffe import JwtBundleSet

from .const import CONF_SINGLE_USE_TOKENS, CONF_SPIFFE_ID, DOMAIN, MAX_BUNDLE_AGE
from .coordinator import SpiffeCoordinator
from .errors import SpiffeAuthError, SpiffeError, SpiffeNotReady
from .ids import normalize_spiffe_id
from .replay import DATA_REPLAY_CACHE, ReplayCache, ReplayCacheFull
from .source import (
    AudienceNotAvailableError,
    FetchedJwtSvid,
    SourceUnavailableError,
    SpiffeSourceError,
    validate_jwt_svid,
)

_LOGGER = logging.getLogger(__name__)

BEARER_PREFIX = "bearer "

type VerifiedWebhookHandler = Callable[
    [HomeAssistant, str, web.Request, str], Awaitable[web.Response | None]
]


def _coordinator(hass: HomeAssistant) -> SpiffeCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise SpiffeNotReady("The SPIFFE integration is not set up or not loaded")
    return entries[0].runtime_data


async def async_fetch_jwt_svid(
    hass: HomeAssistant,
    audience: str | Iterable[str],
    subject: str | None = None,
) -> str:
    """Return a JWT-SVID for this audience (or audiences), to send as a bearer token.

    `subject` picks one SPIFFE ID when the source holds several; leave it out otherwise.
    Raises SpiffeNotReady when the integration is not loaded, SpiffeError when the source
    cannot provide the token, and ValueError for an empty audience or invalid subject.
    The token itself is never logged or stored by this integration.
    """
    coordinator = _coordinator(hass)
    try:
        svid = await hass.async_add_executor_job(
            coordinator.source.fetch_jwt_svid, audience, subject
        )
    except SourceUnavailableError as err:
        if err.permission_denied:
            coordinator.note_permission_denied(str(err))
        raise SpiffeError(f"Could not get a JWT-SVID: {err}") from err
    except AudienceNotAvailableError as err:
        raise SpiffeError(f"Could not get a JWT-SVID: {err}") from err
    coordinator.note_svid_fetched()
    return svid.token


async def async_get_jwt_bundles(hass: HomeAssistant) -> JwtBundleSet:
    """Return a copy of the JWT bundles from the latest successful check.

    They are refreshed every minute while the Workload API answers. Raises SpiffeError when they
    could not be refreshed for a while, as validation does.
    """
    coordinator = _coordinator(hass)
    _check_bundle_age(coordinator)
    bundles = coordinator.bundles
    return JwtBundleSet({b.trust_domain: b for b in bundles.bundles})


def _check_bundle_age(coordinator: SpiffeCoordinator) -> None:
    age = dt_util.utcnow() - coordinator.bundles_fetched_at
    if age > MAX_BUNDLE_AGE:
        raise SpiffeError(
            f"The trust bundles have not been refreshed for {int(age.total_seconds() // 60)}"
            " minutes; not using them"
        )


async def _async_validate(hass: HomeAssistant, token: str, audience: str) -> FetchedJwtSvid:
    coordinator = _coordinator(hass)
    if not isinstance(audience, str) or not audience.strip():
        raise ValueError("the audience must be a non-empty string")
    _check_bundle_age(coordinator)
    try:
        return await hass.async_add_executor_job(
            validate_jwt_svid, coordinator.bundles, token, audience
        )
    except SpiffeSourceError as err:
        raise SpiffeError(str(err)) from err


async def async_validate_jwt_svid(hass: HomeAssistant, token: str, audience: str) -> str:
    """Validate a JWT-SVID someone presented to Home Assistant, and return its SPIFFE ID.

    It must be for `audience`, unexpired, and signed by a key in the cached bundles. Check the
    returned SPIFFE ID against the caller you expect: a valid token only says who sent it.
    Raises SpiffeNotReady, SpiffeError (also when the bundles could not be refreshed for a
    while, so a key the issuer withdrew is not trusted indefinitely), or ValueError for an empty
    or non-string audience.

    This does not stop a token from being used twice. For a request, use `async_verify_request`.
    """
    return (await _async_validate(hass, token, audience)).spiffe_id


async def async_x509_available(hass: HomeAssistant) -> bool:
    """Return whether the source offered X.509-SVIDs when the integration was set up."""
    return _coordinator(hass).x509_available


async def async_verify_request(
    hass: HomeAssistant,
    request: web.Request,
    *,
    allowed_callers: Iterable[str],
) -> str:
    """Authenticate an incoming request and return the SPIFFE ID of its caller.

    The request must carry `Authorization: Bearer <JWT-SVID>` whose audience is Home Assistant's
    own SPIFFE ID (the `spiffe_id` option of the integration), and whose subject is one of
    `allowed_callers` (a collection of SPIFFE IDs; a bare string raises TypeError, an invalid ID
    ValueError). The audience must be exactly that one SPIFFE ID, not a list with other services
    next to it. An empty `allowed_callers` admits nobody. With the option `single_use_tokens` on,
    a token is accepted once: replaying it, on this or any other verified endpoint, fails. It is
    off by default, because agents that cache tokens hand out the same one for repeated fetches.

    Raises SpiffeAuthError with status 401 (no, invalid, expired or replayed token) or 403 (a valid
    caller that is not allowed), and SpiffeNotReady when the integration is not loaded or has no
    SPIFFE ID configured (answer 503). The messages are for logs, not for the caller.
    """
    return await _async_verify(hass, request, _normalize_callers(allowed_callers))


def _normalize_callers(allowed_callers: Iterable[str]) -> frozenset[str]:
    """Check and canonicalize a list of caller SPIFFE IDs.

    Raises TypeError for a bare string (a typo that would match single characters) and ValueError
    for an entry that is not a SPIFFE ID of a workload.
    """
    if isinstance(allowed_callers, str):
        raise TypeError("allowed_callers must be a collection of SPIFFE IDs, not a string")
    return frozenset(normalize_spiffe_id(caller) for caller in allowed_callers)


def _audiences(token: str) -> set[str]:
    """The `aud` claim of a token whose signature has been checked."""
    claims = jwt.decode(
        token, options={"verify_signature": False, "verify_aud": False, "verify_exp": False}
    )
    aud = claims.get("aud")
    return {aud} if isinstance(aud, str) else set(aud) if isinstance(aud, list) else set()


async def _async_verify(
    hass: HomeAssistant, request: web.Request, allowed_callers: Collection[str] | None
) -> str:
    """Verify a request. `None` for `allowed_callers` admits any valid caller."""
    # Look at the request before anything about this installation, so an unauthenticated caller
    # learns nothing about whether SPIFFE is ready.
    header = request.headers.get("Authorization", "")
    if header[: len(BEARER_PREFIX)].lower() != BEARER_PREFIX or not header[len(BEARER_PREFIX) :]:
        raise SpiffeAuthError(401, "no bearer token")
    token = header[len(BEARER_PREFIX) :].strip()

    coordinator = _coordinator(hass)
    audience = coordinator.config_entry.options.get(CONF_SPIFFE_ID)
    if not audience:
        raise SpiffeNotReady("No SPIFFE ID is configured for Home Assistant")
    try:
        _check_bundle_age(coordinator)
    except SpiffeError as err:
        # Not the caller's fault, so not a 401.
        raise SpiffeNotReady(str(err)) from err

    try:
        svid = await _async_validate(hass, token, audience)
    except SpiffeNotReady:
        raise
    except (SpiffeError, ValueError) as err:
        raise SpiffeAuthError(401, f"invalid token: {err}") from err
    # py-spiffe accepts a token that names other audiences as well. A token meant for several
    # services could be replayed by any of them, so a request must be for this one alone.
    if _audiences(token) != {audience}:
        raise SpiffeAuthError(401, "token has more than one audience")

    if allowed_callers is not None and svid.spiffe_id not in allowed_callers:
        raise SpiffeAuthError(403, f"caller {svid.spiffe_id} is not allowed")

    if not coordinator.config_entry.options.get(CONF_SINGLE_USE_TOKENS):
        # Tokens are bearer tokens, valid until they expire. Agents such as SPIRE and spiffex hand
        # out the same token for repeated fetches, so refusing a second use would refuse honest
        # callers.
        return svid.spiffe_id

    # Nothing awaits between the check and the record, so two requests with the same token cannot
    # both get through.
    cache = hass.data.setdefault(DATA_REPLAY_CACHE, ReplayCache())
    try:
        fresh = cache.check_and_record(token, svid.expiry.timestamp(), dt_util.utcnow().timestamp())
    except ReplayCacheFull as err:
        # Failing closed: forgetting a live token would let it be replayed.
        raise SpiffeNotReady("too many tokens are in use; try again shortly") from err
    if not fresh:
        raise SpiffeAuthError(401, "token already used")
    return svid.spiffe_id


_ERROR_REASONS = {401: "unauthorized", 403: "forbidden", 503: "unavailable"}


def error_response(status: int) -> web.Response:
    """A JSON error with a fixed reason. Never put exception text in it."""
    return web.json_response({"error": _ERROR_REASONS.get(status, "error")}, status=status)


def async_register_verified_webhook(
    hass: HomeAssistant,
    domain: str,
    name: str,
    webhook_id: str,
    handler: VerifiedWebhookHandler,
    *,
    allowed_callers: Iterable[str],
    allowed_methods: Iterable[str] = ("POST",),
) -> None:
    """Register a webhook that only answers callers presenting a valid JWT-SVID.

    `handler(hass, webhook_id, request, caller)` runs only for a request that passed
    `async_verify_request` for one of `allowed_callers` (raises TypeError for a bare string and
    ValueError for an invalid SPIFFE ID); `caller` is its SPIFFE ID. Failures
    answer a JSON body `{"error": "<reason>"}` with 401, 403 or 503 and never include token
    contents or exception text. The webhook is not limited to the local network. Unregister it
    with `homeassistant.components.webhook.async_unregister(hass, webhook_id)`.
    """
    callers = _normalize_callers(allowed_callers)

    async def _handle(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        try:
            caller = await _async_verify(hass, request, callers)
        except SpiffeAuthError as err:
            _LOGGER.info("Webhook %s refused a request (%s): %s", name, err.status, err)
            return error_response(err.status)
        except SpiffeNotReady as err:
            _LOGGER.warning("Webhook %s cannot verify requests yet: %s", name, err)
            return error_response(503)
        except SpiffeError as err:
            _LOGGER.warning("Webhook %s could not verify a request: %s", name, err)
            return error_response(503)
        response = await handler(hass, webhook_id, request, caller)
        return response if response is not None else web.Response(status=200)

    webhook.async_register(
        hass,
        domain,
        name,
        webhook_id,
        _handle,
        local_only=False,
        allowed_methods=allowed_methods,
    )
