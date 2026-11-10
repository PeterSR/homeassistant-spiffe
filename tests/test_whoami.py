"""Tests for the opt-in whoami endpoint."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.spiffe.const import CONF_EXPOSE_WHOAMI, CONF_SPIFFE_ID, WHOAMI_PATH

from .conftest import CALLER, HA_SPIFFE_ID, FakeSource, Signer
from .test_verify import setup_spiffe


def _bearer(signer: Signer, subject: str = CALLER) -> dict[str, str]:
    return {"Authorization": f"Bearer {signer.token([HA_SPIFFE_ID], subject=subject)}"}


async def test_not_found_when_disabled(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass)
    client = await hass_client_no_auth()
    resp = await client.post(WHOAMI_PATH, headers=_bearer(signer))
    assert resp.status == 404
    # Even a request that would not verify gets the same answer, so it reveals nothing.
    resp = await client.post(WHOAMI_PATH)
    assert resp.status == 404


async def test_not_found_without_an_entry(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    assert await async_setup_component(hass, "spiffe", {})
    client = await hass_client_no_auth()
    assert (await client.post(WHOAMI_PATH)).status == 404


async def test_works_when_enabled(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass, options={CONF_SPIFFE_ID: HA_SPIFFE_ID, CONF_EXPOSE_WHOAMI: True})
    client = await hass_client_no_auth()

    resp = await client.post(WHOAMI_PATH, headers=_bearer(signer))
    assert resp.status == 200
    assert await resp.json() == {"caller": CALLER, "audience": HA_SPIFFE_ID}

    assert (await client.post(WHOAMI_PATH)).status == 401
    resp = await client.post(
        WHOAMI_PATH,
        headers={"Authorization": f"Bearer {signer.token(['spiffe://example.org/x'])}"},
    )
    assert resp.status == 401


async def test_replay_is_refused(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass, options={CONF_SPIFFE_ID: HA_SPIFFE_ID, CONF_EXPOSE_WHOAMI: True})
    client = await hass_client_no_auth()
    headers = _bearer(signer)
    assert (await client.post(WHOAMI_PATH, headers=headers)).status == 200
    assert (await client.post(WHOAMI_PATH, headers=headers)).status == 401


async def test_unavailable_without_spiffe_id(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass, options={CONF_EXPOSE_WHOAMI: True})
    client = await hass_client_no_auth()
    resp = await client.post(WHOAMI_PATH, headers=_bearer(signer))
    assert resp.status == 503
    assert await resp.json() == {"error": "unavailable"}


async def test_toggle_takes_effect_without_reload(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    entry = await setup_spiffe(hass)
    client = await hass_client_no_auth()
    assert (await client.post(WHOAMI_PATH, headers=_bearer(signer))).status == 404
    hass.config_entries.async_update_entry(
        entry, options={CONF_SPIFFE_ID: HA_SPIFFE_ID, CONF_EXPOSE_WHOAMI: True}
    )
    await hass.async_block_till_done()
    assert (await client.post(WHOAMI_PATH, headers=_bearer(signer))).status == 200
