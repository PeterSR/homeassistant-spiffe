"""Tests for setup, health checks and the helpers."""

from __future__ import annotations

import logging
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
from spiffe import TrustDomain

from custom_components.spiffe import (
    SpiffeError,
    SpiffeNotReady,
    _delete_issues,
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
    ISSUE_NO_IDENTITY,
    ISSUE_PERMISSION_DENIED,
    ISSUE_UNREACHABLE,
    MAX_BUNDLE_AGE,
)
from custom_components.spiffe.source import SocketMissingError

from .conftest import SPIFFE_ID, FakeSource, Signer

REACHABLE = "binary_sensor.spiffe_workload_api_workload_api"
BUNDLE_KEYS = "sensor.spiffe_workload_api_trust_bundle_keys"
X509 = "binary_sensor.spiffe_workload_api_x_509_svids_available"


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


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, times: int = 1) -> None:
    for _ in range(times):
        freezer.tick(HEALTH_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_checks_run_with_polling_disabled(
    hass: HomeAssistant, fake_source: FakeSource, freezer: FrozenDateTimeFactory
) -> None:
    """Turning off polling must not stop the checks that refresh the bundles."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_SOCKET: DEFAULT_SOCKET}, pref_disable_polling=True
    )
    await _setup(hass, entry)
    fake_source.reachable = False
    await _tick(hass, freezer)
    assert hass.states.get(REACHABLE).state == "off"


async def test_stale_bundles_are_not_trusted(
    hass: HomeAssistant, fake_source: FakeSource, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass)
    token = fake_source.signer.token({"aud"})
    fake_source.reachable = False
    await _tick(hass, freezer, int(MAX_BUNDLE_AGE / HEALTH_INTERVAL) + 1)
    with pytest.raises(SpiffeError, match="not been refreshed"):
        await async_validate_jwt_svid(hass, token, "aud")
    with pytest.raises(SpiffeError, match="not been refreshed"):
        await async_get_jwt_bundles(hass)

    fake_source.reachable = True
    await _tick(hass, freezer)
    fresh = fake_source.signer.token({"aud"})
    assert await async_validate_jwt_svid(hass, fresh, "aud") == SPIFFE_ID


async def test_validate_rejects_bad_audience(hass: HomeAssistant, fake_source: FakeSource) -> None:
    await _setup(hass)
    token = fake_source.signer.token({"aud"})
    for audience in ("", ["aud"]):
        with pytest.raises(ValueError):
            await async_validate_jwt_svid(hass, token, audience)  # type: ignore[arg-type]


async def test_bundles_returned_are_a_copy(
    hass: HomeAssistant, fake_source: FakeSource, signer: Signer
) -> None:
    """A caller changing the bundles it got must not change what validation trusts."""
    await _setup(hass)
    other = Signer(key_id="other")
    bundles = await async_get_jwt_bundles(hass)
    bundles.put(other.bundles().get_bundle_for_trust_domain(TrustDomain("example.org")))
    with pytest.raises(SpiffeError):
        await async_validate_jwt_svid(hass, other.token({"aud"}), "aud")


async def test_unload_clears_issues(
    hass: HomeAssistant, fake_source: FakeSource, freezer: FrozenDateTimeFactory
) -> None:
    entry = await _setup(hass)
    issues = ir.async_get(hass)
    fake_source.permission_denied = True
    with pytest.raises(SpiffeError):
        await async_fetch_jwt_svid(hass, "aud")
    fake_source.reachable = False
    await _tick(hass, freezer, 7)
    assert issues.async_get_issue(DOMAIN, ISSUE_UNREACHABLE)
    assert issues.async_get_issue(DOMAIN, ISSUE_PERMISSION_DENIED)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not issues.async_get_issue(DOMAIN, ISSUE_UNREACHABLE)
    assert not issues.async_get_issue(DOMAIN, ISSUE_PERMISSION_DENIED)


async def test_x509_entity(hass: HomeAssistant, fake_source: FakeSource) -> None:
    fake_source.x509 = True
    await _setup(hass)
    assert hass.states.get(X509).state == "on"
    assert await async_x509_available(hass) is True


async def test_token_never_logged(
    hass: HomeAssistant, fake_source: FakeSource, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await _setup(hass)
    token = await async_fetch_jwt_svid(hass, "aud")
    await async_validate_jwt_svid(hass, token, "aud")
    with pytest.raises(SpiffeError):
        await async_validate_jwt_svid(hass, token, "other")
    assert token not in caplog.text
    for part in token.split("."):
        assert part not in caplog.text


async def test_setup_failure_after_connecting_closes(
    hass: HomeAssistant, fake_source: FakeSource
) -> None:
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        side_effect=RuntimeError("platform failed"),
    ):
        entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert fake_source.closed


async def test_no_identity_at_setup(hass: HomeAssistant, fake_source: FakeSource) -> None:
    """A Workload API that refuses even the bundles (SPIRE without a registration)."""
    fake_source.bundles_denied = True
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, ISSUE_NO_IDENTITY)
    assert not issues.async_get_issue(DOMAIN, ISSUE_UNREACHABLE)


async def test_no_identity_later_is_not_unreachable(
    hass: HomeAssistant, fake_source: FakeSource, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass)
    issues = ir.async_get(hass)
    fake_source.bundles_denied = True
    await _tick(hass, freezer, 7)
    assert issues.async_get_issue(DOMAIN, ISSUE_NO_IDENTITY)
    assert not issues.async_get_issue(DOMAIN, ISSUE_UNREACHABLE)

    fake_source.bundles_denied = False
    await _tick(hass, freezer)
    assert not issues.async_get_issue(DOMAIN, ISSUE_NO_IDENTITY)


async def test_check_in_flight_at_unload_leaves_no_issue(
    hass: HomeAssistant, fake_source: FakeSource, freezer: FrozenDateTimeFactory
) -> None:
    """A check that fails after unload began must not bring a repair issue back."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    fake_source.reachable = False
    await _tick(hass, freezer, 7)
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, ISSUE_UNREACHABLE)

    await coordinator.async_shutdown()
    _delete_issues(hass)
    await coordinator._async_update_data()
    assert not issues.async_get_issue(DOMAIN, ISSUE_UNREACHABLE)
