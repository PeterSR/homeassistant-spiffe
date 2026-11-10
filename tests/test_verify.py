"""Tests for verifying incoming requests."""

from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from aiohttp.test_utils import make_mocked_request
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spiffe import (
    SpiffeAuthError,
    SpiffeError,
    SpiffeNotReady,
    async_register_verified_webhook,
    async_validate_jwt_svid,
    async_verify_request,
)
from custom_components.spiffe.const import (
    CONF_SINGLE_USE_TOKENS,
    CONF_SOCKET,
    CONF_SPIFFE_ID,
    DEFAULT_SOCKET,
    DOMAIN,
    REPLAY_CACHE_MAX_ENTRIES,
)
from custom_components.spiffe.replay import DATA_REPLAY_CACHE, ReplayCache, ReplayCacheFull

from .conftest import CALLER, HA_SPIFFE_ID, OTHER, FakeSource, Signer

SINGLE_USE = {CONF_SPIFFE_ID: HA_SPIFFE_ID, CONF_SINGLE_USE_TOKENS: True}


async def setup_spiffe(
    hass: HomeAssistant, options: dict | None = None, *, subentries: list | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SOCKET: DEFAULT_SOCKET},
        options={CONF_SPIFFE_ID: HA_SPIFFE_ID} if options is None else options,
        subentries_data=subentries or [],
        title="SPIFFE",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _request(token: str | None = None, *, header: str | None = None):
    headers = {}
    if header is not None:
        headers["Authorization"] = header
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return make_mocked_request("POST", "/", headers=headers)


async def _status(hass: HomeAssistant, request, callers) -> int:
    with pytest.raises(SpiffeAuthError) as err:
        await async_verify_request(hass, request, allowed_callers=callers)
    return err.value.status


async def test_accepts_allowed_caller(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    caller = await async_verify_request(hass, _request(token), allowed_callers=[CALLER])
    assert caller == CALLER


async def test_bearer_scheme_is_case_insensitive(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    request = _request(header=f"bearer {token}")
    assert await async_verify_request(hass, request, allowed_callers=[CALLER]) == CALLER


@pytest.mark.parametrize(
    "header", [None, "", "Bearer", "Bearer ", "Bearer    ", "Basic abc", "Bearer not.a.jwt"]
)
async def test_missing_or_garbage_bearer(
    hass: HomeAssistant, fake_source: FakeSource, header: str | None
) -> None:
    await setup_spiffe(hass)
    assert await _status(hass, _request(header=header), [CALLER]) == 401


async def test_wrong_audience(hass: HomeAssistant, fake_source: FakeSource, signer: Signer) -> None:
    await setup_spiffe(hass)
    token = signer.token(["spiffe://example.org/someone/else"], subject=CALLER)
    assert await _status(hass, _request(token), [CALLER]) == 401


async def test_unknown_signer(hass: HomeAssistant, fake_source: FakeSource) -> None:
    await setup_spiffe(hass)
    token = Signer().token([HA_SPIFFE_ID], subject=CALLER)  # a key the bundles do not hold
    assert await _status(hass, _request(token), [CALLER]) == 401


async def test_expired_token(hass: HomeAssistant, fake_source: FakeSource, signer: Signer) -> None:
    await setup_spiffe(hass)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER, lifetime=timedelta(minutes=-5))
    assert await _status(hass, _request(token), [CALLER]) == 401


async def test_unknown_caller_is_forbidden(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass)
    token = signer.token([HA_SPIFFE_ID], subject=OTHER)
    assert await _status(hass, _request(token), [CALLER]) == 403


async def test_empty_allowlist_admits_nobody(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    assert await _status(hass, _request(token), []) == 403


async def test_replay_is_refused(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass, options=SINGLE_USE)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    await async_verify_request(hass, _request(token), allowed_callers=[CALLER])
    assert await _status(hass, _request(token), [CALLER]) == 401
    # The cache is shared: the same token is refused for any other caller list too.
    assert await _status(hass, _request(token), [CALLER, OTHER]) == 401


async def test_tokens_are_reusable_by_default(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    """Agents such as SPIRE and spiffex hand out the same token repeatedly."""
    await setup_spiffe(hass)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    for _ in range(3):
        assert await async_verify_request(hass, _request(token), allowed_callers=[CALLER]) == CALLER
    assert DATA_REPLAY_CACHE not in hass.data


async def test_refused_request_does_not_burn_the_token(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass, options=SINGLE_USE)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    assert await _status(hass, _request(token), [OTHER]) == 403
    assert await async_verify_request(hass, _request(token), allowed_callers=[CALLER]) == CALLER


async def test_not_ready_when_integration_not_loaded(hass: HomeAssistant, signer: Signer) -> None:
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    with pytest.raises(SpiffeNotReady):
        await async_verify_request(hass, _request(token), allowed_callers=[CALLER])


async def test_not_ready_when_spiffe_id_unset(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass, options={})
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    with pytest.raises(SpiffeNotReady):
        await async_verify_request(hass, _request(token), allowed_callers=[CALLER])


async def test_not_ready_when_bundles_are_stale(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer, freezer
) -> None:
    await setup_spiffe(hass)
    fake_source.reachable = False
    freezer.tick(timedelta(minutes=20))
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    with pytest.raises(SpiffeNotReady):
        await async_verify_request(hass, _request(token), allowed_callers=[CALLER])


async def test_auth_error_is_a_spiffe_error() -> None:
    assert issubclass(SpiffeAuthError, SpiffeError)
    assert SpiffeAuthError(403, "nope").status == 403


def test_replay_cache_is_bounded_and_drops_expired() -> None:
    cache = ReplayCache(max_entries=3)
    assert cache.check_and_record("a.p.s", 110, now=100)
    assert not cache.check_and_record("a.p.s", 110, now=101)
    assert cache.check_and_record("b.p.s", 200, now=101)
    assert cache.check_and_record("c.p.s", 200, now=102)
    # "a" has expired by now and makes room, so nothing live is dropped.
    assert cache.check_and_record("d.p.s", 300, now=120)
    assert len(cache) == 3
    assert not cache.check_and_record("b.p.s", 200, now=121)
    # Full of live entries: it refuses instead of forgetting one, and keeps what it has.
    with pytest.raises(ReplayCacheFull):
        cache.check_and_record("e.p.s", 300, now=122)
    assert len(cache) == 3
    assert not cache.check_and_record("b.p.s", 200, now=122)
    assert REPLAY_CACHE_MAX_ENTRIES >= 16384


def test_replay_cache_ignores_the_signature_segment() -> None:
    cache = ReplayCache()
    assert cache.check_and_record("header.payload.sig1", 200, now=100)
    assert not cache.check_and_record("header.payload.sig2==", 200, now=100)


async def test_flood_does_not_free_a_used_token(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass, options=SINGLE_USE)
    hass.data[DATA_REPLAY_CACHE] = ReplayCache(max_entries=2)
    first = signer.token([HA_SPIFFE_ID], subject=CALLER)
    await async_verify_request(hass, _request(first), allowed_callers=[CALLER])
    await async_verify_request(
        hass, _request(signer.token([HA_SPIFFE_ID], subject=CALLER)), allowed_callers=[CALLER]
    )
    # The cache is full: new tokens are answered 503 (not ready), the used one stays refused.
    with pytest.raises(SpiffeNotReady):
        await async_verify_request(
            hass, _request(signer.token([HA_SPIFFE_ID], subject=CALLER)), allowed_callers=[CALLER]
        )
    assert await _status(hass, _request(first), [CALLER]) == 401


def _flip_s(token: str) -> str:
    """The other valid ECDSA signature for the same message: (r, n - s)."""
    head, sig = token.rsplit(".", 1)
    raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    r, s = raw[:32], int.from_bytes(raw[32:], "big")
    order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    flipped = r + (order - s).to_bytes(32, "big")
    return head + "." + base64.urlsafe_b64encode(flipped).rstrip(b"=").decode()


async def test_signature_variants_are_replays(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass, options=SINGLE_USE)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    variants = [token + "==", _flip_s(token)]
    # Each variant is itself accepted by validation, so only the replay cache can stop it.
    for variant in variants:
        assert await async_validate_jwt_svid(hass, variant, HA_SPIFFE_ID) == CALLER
    await async_verify_request(hass, _request(token), allowed_callers=[CALLER])
    for variant in variants:
        assert await _status(hass, _request(variant), [CALLER]) == 401


async def test_multiple_audiences_are_refused(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass)
    token = signer.token([HA_SPIFFE_ID, "spiffe://example.org/other/api"], subject=CALLER)
    # Plain validation still accepts it (the 0.1.0 contract); a request must not.
    assert await async_validate_jwt_svid(hass, token, HA_SPIFFE_ID) == CALLER
    assert await _status(hass, _request(token), [CALLER]) == 401


async def test_single_element_audience_list_is_accepted(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)  # the signer emits a list
    assert await async_verify_request(hass, _request(token), allowed_callers=[CALLER]) == CALLER


async def test_bad_allowed_callers(hass: HomeAssistant, fake_source: FakeSource) -> None:
    await setup_spiffe(hass)
    with pytest.raises(TypeError):
        await async_verify_request(hass, _request("x"), allowed_callers=CALLER)
    with pytest.raises(ValueError):
        await async_verify_request(hass, _request("x"), allowed_callers=["not-a-spiffe-id"])

    async def handler(hass, webhook_id, request, caller):
        return None

    with pytest.raises(TypeError):
        async_register_verified_webhook(hass, "t", "T", "h", handler, allowed_callers=CALLER)
    with pytest.raises(ValueError):
        async_register_verified_webhook(
            hass, "t", "T", "h", handler, allowed_callers=["spiffe://x"]
        )


async def test_unauthenticated_callers_cannot_probe_readiness(
    hass: HomeAssistant, fake_source: FakeSource
) -> None:
    await setup_spiffe(hass, options={})  # not ready: no SPIFFE ID
    assert await _status(hass, _request(), [CALLER]) == 401
    assert await _status(hass, _request(header="Basic abc"), [CALLER]) == 401
    with pytest.raises(SpiffeNotReady):  # only a request that has a bearer sees the 503
        await async_verify_request(hass, _request("a.b.c"), allowed_callers=[CALLER])


async def test_cache_lives_in_hass_data(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass, options=SINGLE_USE)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    await async_verify_request(hass, _request(token), allowed_callers=[CALLER])
    assert len(hass.data[DATA_REPLAY_CACHE]) == 1


async def test_flood_warning_is_throttled(
    hass: HomeAssistant,
    hass_client_no_auth,
    fake_source: FakeSource,
    signer: Signer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(hass, webhook_id, request, caller):
        return None

    await setup_spiffe(hass, options=SINGLE_USE)
    hass.data[DATA_REPLAY_CACHE] = ReplayCache(max_entries=0)
    async_register_verified_webhook(hass, "t", "Flood", "flood", handler, allowed_callers=[CALLER])
    client = await hass_client_no_auth()
    for _ in range(5):
        token = signer.token([HA_SPIFFE_ID], subject=CALLER)
        resp = await client.post("/api/webhook/flood", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 503
    assert sum("cannot verify requests" in r.message for r in caplog.records) == 1
