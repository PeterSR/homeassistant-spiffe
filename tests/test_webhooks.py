"""Tests for verified webhooks and event webhook subentries."""

from __future__ import annotations

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigSubentry, ConfigSubentryData
from homeassistant.core import Event, HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.spiffe import async_register_verified_webhook
from custom_components.spiffe.const import (
    CONF_ALLOWED_CALLERS,
    CONF_NAME,
    CONF_WEBHOOK_ID,
    EVENT_WEBHOOK,
    SUBENTRY_EVENT_WEBHOOK,
)

from .conftest import CALLER, HA_SPIFFE_ID, OTHER, FakeSource, Signer
from .test_verify import setup_spiffe

WEBHOOK_ID = "test-hook-id-0123456789"


def _bearer(signer: Signer, subject: str = CALLER, audience: str = HA_SPIFFE_ID) -> dict[str, str]:
    return {"Authorization": f"Bearer {signer.token([audience], subject=subject)}"}


def _subentry(webhook_id: str = WEBHOOK_ID, callers: list[str] | None = None) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={
            CONF_NAME: "Orders",
            CONF_WEBHOOK_ID: webhook_id,
            CONF_ALLOWED_CALLERS: callers or [CALLER],
        },
        subentry_type=SUBENTRY_EVENT_WEBHOOK,
        title="Orders",
        unique_id=webhook_id,
    )


async def test_handler_runs_only_for_verified_caller(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass)
    seen: list[tuple[str, str]] = []

    async def handler(
        hass: HomeAssistant, webhook_id: str, request: web.Request, caller: str
    ) -> web.Response | None:
        seen.append((webhook_id, caller))
        return web.json_response({"hello": caller})

    async_register_verified_webhook(
        hass, "test", "Test", "hook1", handler, allowed_callers=[CALLER]
    )
    client = await hass_client_no_auth()

    resp = await client.post("/api/webhook/hook1")
    assert resp.status == 401
    assert await resp.json() == {"error": "unauthorized"}
    resp = await client.post("/api/webhook/hook1", headers=_bearer(signer, subject=OTHER))
    assert resp.status == 403
    assert await resp.json() == {"error": "forbidden"}
    assert seen == []

    resp = await client.post("/api/webhook/hook1", headers=_bearer(signer))
    assert resp.status == 200
    assert await resp.json() == {"hello": CALLER}
    assert seen == [("hook1", CALLER)]

    # Only POST by default.
    resp = await client.put("/api/webhook/hook1", headers=_bearer(signer))
    assert resp.status == 405
    webhook.async_unregister(hass, "hook1")


async def test_handler_returning_none_answers_200(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass)

    async def handler(hass, webhook_id, request, caller):
        return None

    async_register_verified_webhook(
        hass, "test", "Test", "hook2", handler, allowed_callers=[CALLER], allowed_methods=["PUT"]
    )
    client = await hass_client_no_auth()
    resp = await client.put("/api/webhook/hook2", headers=_bearer(signer))
    assert resp.status == 200


async def test_not_ready_maps_to_503_without_details(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass, options={})  # no SPIFFE ID of its own

    async def handler(hass, webhook_id, request, caller):
        raise AssertionError("must not run")

    async_register_verified_webhook(
        hass, "test", "Test", "hook3", handler, allowed_callers=[CALLER]
    )
    client = await hass_client_no_auth()
    resp = await client.post("/api/webhook/hook3", headers=_bearer(signer))
    assert resp.status == 503
    assert await resp.json() == {"error": "unavailable"}


async def test_errors_do_not_leak_token_or_reasons(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass)

    async def handler(hass, webhook_id, request, caller):
        raise AssertionError("must not run")

    async_register_verified_webhook(
        hass, "test", "Test", "hook4", handler, allowed_callers=[CALLER]
    )
    client = await hass_client_no_auth()
    token = signer.token(["spiffe://example.org/other"], subject=CALLER)
    resp = await client.post("/api/webhook/hook4", headers={"Authorization": f"Bearer {token}"})
    text = await resp.text()
    assert resp.status == 401
    assert token not in text
    assert text.strip() == '{"error": "unauthorized"}'


async def test_event_webhook_fires_event(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass, subentries=[_subentry()])
    events: list[Event] = async_capture_events(hass, EVENT_WEBHOOK)
    client = await hass_client_no_auth()

    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}", json={"night": 3}, headers=_bearer(signer)
    )
    assert resp.status == 200
    assert await resp.json() == {"ok": True}
    await hass.async_block_till_done()
    assert [e.data for e in events] == [
        {"webhook_id": WEBHOOK_ID, "name": "Orders", "caller": CALLER, "data": {"night": 3}}
    ]


async def test_event_webhook_empty_body_is_null_data(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass, subentries=[_subentry()])
    events = async_capture_events(hass, EVENT_WEBHOOK)
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", headers=_bearer(signer))
    assert resp.status == 200
    await hass.async_block_till_done()
    assert events[0].data["data"] is None


async def test_event_webhook_rejects_non_json(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass, subentries=[_subentry()])
    events = async_capture_events(hass, EVENT_WEBHOOK)
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{WEBHOOK_ID}", data="not json", headers=_bearer(signer))
    assert resp.status == 400
    await hass.async_block_till_done()
    assert events == []


async def test_event_webhook_refuses_other_callers(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    await setup_spiffe(hass, subentries=[_subentry()])
    events = async_capture_events(hass, EVENT_WEBHOOK)
    client = await hass_client_no_auth()
    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}", json={}, headers=_bearer(signer, subject=OTHER)
    )
    assert resp.status == 403
    await hass.async_block_till_done()
    assert events == []


async def test_event_webhook_unregistered_on_unload_and_follows_subentries(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    fake_source: FakeSource,
    signer: Signer,
) -> None:
    entry = await setup_spiffe(hass, subentries=[_subentry()])
    client = await hass_client_no_auth()
    events = async_capture_events(hass, EVENT_WEBHOOK)

    # Adding a subentry reloads the entry and registers the new webhook.
    hass.config_entries.async_add_subentry(
        entry,
        _make_subentry("second-hook-id-0123456", [OTHER]),
    )
    await hass.async_block_till_done()
    resp = await client.post(
        "/api/webhook/second-hook-id-0123456", json={}, headers=_bearer(signer, subject=OTHER)
    )
    assert resp.status == 200

    # Removing one unregisters it: HA answers 200 to unknown webhooks without running anything.
    first = next(s for s in entry.subentries.values() if s.data[CONF_WEBHOOK_ID] == WEBHOOK_ID)
    hass.config_entries.async_remove_subentry(entry, first.subentry_id)
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    events.clear()
    await client.post(f"/api/webhook/{WEBHOOK_ID}", json={}, headers=_bearer(signer))
    await hass.async_block_till_done()
    assert events == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    events.clear()
    await client.post(
        "/api/webhook/second-hook-id-0123456", json={}, headers=_bearer(signer, subject=OTHER)
    )
    await hass.async_block_till_done()
    assert events == []


async def test_options_change_does_not_reload(hass: HomeAssistant, fake_source: FakeSource) -> None:
    entry = await setup_spiffe(hass, subentries=[_subentry()])
    builds = len(fake_source.uris)
    hass.config_entries.async_update_entry(entry, options={"expose_whoami": True})
    await hass.async_block_till_done()
    assert len(fake_source.uris) == builds


def _make_subentry(webhook_id: str, callers: list[str]):
    return ConfigSubentry(
        data={CONF_NAME: "Second", CONF_WEBHOOK_ID: webhook_id, CONF_ALLOWED_CALLERS: callers},
        subentry_type=SUBENTRY_EVENT_WEBHOOK,
        title="Second",
        unique_id=webhook_id,
    )
