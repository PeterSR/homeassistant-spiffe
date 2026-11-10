"""Tests for setup, health checks and the helpers."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.spiffe import (
    SpiffeError,
    SpiffeNotReady,
    async_fetch_jwt_svid,
    async_get_jwt_bundles,
    async_validate_jwt_svid,
    async_x509_available,
)
from custom_components.spiffe.const import (
    CONF_SOCKET,
    DEFAULT_SOCKET,
    DOMAIN,
    HEALTH_INTERVAL,
    ISSUE_PERMISSION_DENIED,
    ISSUE_UNREACHABLE,
)
from custom_components.spiffe.source import SocketMissingError

from .conftest import SPIFFE_ID, FakeSource, Signer

REACHABLE = "binary_sensor.spiffe_workload_api_workload_api"
BUNDLE_KEYS = "sensor.spiffe_workload_api_trust_bundle_keys"


def _entry(socket: str = DEFAULT_SOCKET) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data={CONF_SOCKET: socket}, title="SPIFFE")


async def _setup(hass: HomeAssistant, entry: MockConfigEntry | None = None) -> MockConfigEntry:
    entry = entry or _entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_and_unload(hass: HomeAssistant, fake_source: FakeSource) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(REACHABLE).state == "on"
    assert hass.states.get(BUNDLE_KEYS).state == "1"
    assert hass.states.get(BUNDLE_KEYS).attributes["trust_domains"] == ["example.org"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert fake_source.closed


async def test_setup_retries_when_socket_missing(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.spiffe.WorkloadApiSource",
        side_effect=SocketMissingError("no socket"),
    ):
        entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_and_closes_on_stale_socket(
    hass: HomeAssistant, fake_source: FakeSource
) -> None:
    fake_source.reachable = False
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert fake_source.closed


async def test_setup_fails_on_invalid_address(hass: HomeAssistant, fake_source: FakeSource) -> None:
    entry = await _setup(hass, _entry("tcp://127.0.0.1:1"))
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_helpers(hass: HomeAssistant, fake_source: FakeSource, signer: Signer) -> None:
    await _setup(hass)

    token = await async_fetch_jwt_svid(hass, "https://api.example.org")
    assert fake_source.svid_requests == [({"https://api.example.org"}, None)]
    assert await async_validate_jwt_svid(hass, token, "https://api.example.org") == SPIFFE_ID

    with pytest.raises(SpiffeError):
        await async_validate_jwt_svid(hass, token, "somewhere-else")
    with pytest.raises(ValueError):
        await async_fetch_jwt_svid(hass, "")

    bundles = await async_get_jwt_bundles(hass)
    assert {b.trust_domain.name for b in bundles.bundles} == {"example.org"}
    assert await async_x509_available(hass) is False


async def test_helpers_not_ready(hass: HomeAssistant) -> None:
    with pytest.raises(SpiffeNotReady):
        await async_fetch_jwt_svid(hass, "aud")
    with pytest.raises(SpiffeNotReady):
        await async_get_jwt_bundles(hass)


async def test_permission_denied_raises_issue(hass: HomeAssistant, fake_source: FakeSource) -> None:
    await _setup(hass)
    issues = ir.async_get(hass)

    fake_source.permission_denied = True
    with pytest.raises(SpiffeError):
        await async_fetch_jwt_svid(hass, "aud")
    assert issues.async_get_issue(DOMAIN, ISSUE_PERMISSION_DENIED)

    fake_source.permission_denied = False
    await async_fetch_jwt_svid(hass, "aud")
    assert not issues.async_get_issue(DOMAIN, ISSUE_PERMISSION_DENIED)


async def test_unreachable_then_recovers(
    hass: HomeAssistant, fake_source: FakeSource, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass)
    issues = ir.async_get(hass)

    fake_source.reachable = False
    freezer.tick(HEALTH_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(REACHABLE).state == "off"
    # The source is still in its grace period: no repair issue yet.
    assert not issues.async_get_issue(DOMAIN, ISSUE_UNREACHABLE)

    for _ in range(6):
        freezer.tick(HEALTH_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, ISSUE_UNREACHABLE)

    fake_source.reachable = True
    freezer.tick(HEALTH_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(REACHABLE).state == "on"
    assert not issues.async_get_issue(DOMAIN, ISSUE_UNREACHABLE)


async def test_bundles_refresh(
    hass: HomeAssistant, fake_source: FakeSource, freezer: FrozenDateTimeFactory
) -> None:
    """A key rotation at the issuer reaches the cached bundles on the next check."""
    await _setup(hass)
    fake_source.signer = Signer(key_id="rotated")
    token = fake_source.signer.token({"aud"})
    with pytest.raises(SpiffeError):
        await async_validate_jwt_svid(hass, token, "aud")

    freezer.tick(HEALTH_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert await async_validate_jwt_svid(hass, token, "aud") == SPIFFE_ID
