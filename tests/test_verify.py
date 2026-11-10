"""Tests for verifying incoming requests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiohttp.test_utils import make_mocked_request
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spiffe import (
    SpiffeAuthError,
    SpiffeError,
    SpiffeNotReady,
    async_verify_request,
)
from custom_components.spiffe.const import (
    CONF_SOCKET,
    CONF_SPIFFE_ID,
    DEFAULT_SOCKET,
    DOMAIN,
    REPLAY_CACHE_MAX_ENTRIES,
)
from custom_components.spiffe.replay import DATA_REPLAY_CACHE, ReplayCache

from .conftest import CALLER, HA_SPIFFE_ID, OTHER, FakeSource, Signer


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


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer ", "Basic abc", "Bearer not.a.jwt"])
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
    await setup_spiffe(hass)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    await async_verify_request(hass, _request(token), allowed_callers=[CALLER])
    assert await _status(hass, _request(token), [CALLER]) == 401
    # The cache is shared: the same token is refused for any other caller list too.
    assert await _status(hass, _request(token), [CALLER, OTHER]) == 401


async def test_refused_request_does_not_burn_the_token(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass)
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
    assert cache.check_and_record("a", 110, now=100)
    assert not cache.check_and_record("a", 110, now=101)
    assert cache.check_and_record("b", 200, now=101)
    assert cache.check_and_record("c", 200, now=102)
    # "a" has expired by now and makes room, so nothing live is dropped.
    assert cache.check_and_record("d", 300, now=120)
    assert len(cache) == 3
    assert not cache.check_and_record("b", 200, now=121)
    # Full of live entries: the oldest goes, the cache never grows past its bound.
    assert cache.check_and_record("e", 300, now=122)
    assert len(cache) == 3
    assert REPLAY_CACHE_MAX_ENTRIES > 0


async def test_cache_lives_in_hass_data(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    await setup_spiffe(hass)
    token = signer.token([HA_SPIFFE_ID], subject=CALLER)
    await async_verify_request(hass, _request(token), allowed_callers=[CALLER])
    assert len(hass.data[DATA_REPLAY_CACHE]) == 1
