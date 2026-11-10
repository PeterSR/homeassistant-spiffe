"""Tests for the config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spiffe.const import (
    CONF_ALLOWED_CALLERS,
    CONF_EXPOSE_WHOAMI,
    CONF_NAME,
    CONF_SINGLE_USE_TOKENS,
    CONF_SOCKET,
    CONF_SPIFFE_ID,
    CONF_WEBHOOK_ID,
    DEFAULT_SOCKET,
    DOMAIN,
    SUBENTRY_EVENT_WEBHOOK,
)
from custom_components.spiffe.source import SocketMissingError

from .conftest import CALLER, HA_SPIFFE_ID, OTHER, FakeSource
from .test_verify import setup_spiffe


async def test_user_flow(hass: HomeAssistant, fake_source: FakeSource) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    schema_default = next(iter(result["data_schema"].schema)).default()
    assert schema_default == DEFAULT_SOCKET

    with (
        patch(
            "custom_components.spiffe.config_flow.WorkloadApiSource",
            return_value=fake_source,
        ),
        patch("custom_components.spiffe.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SOCKET: DEFAULT_SOCKET}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SOCKET: DEFAULT_SOCKET}
    assert fake_source.close_calls == 1  # the connection check closes its own source


async def test_user_flow_default_from_environment(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPIFFE_ENDPOINT_SOCKET", "unix:///run/spire/agent.sock")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert next(iter(result["data_schema"].schema)).default() == "unix:///run/spire/agent.sock"


@pytest.mark.parametrize(
    ("socket", "error_key", "error"),
    [
        ("tcp://127.0.0.1:1", CONF_SOCKET, "invalid_socket"),
        ("/no/scheme.sock", CONF_SOCKET, "invalid_socket"),
    ],
)
async def test_user_flow_invalid(
    hass: HomeAssistant, fake_source: FakeSource, socket: str, error_key: str, error: str
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOCKET: socket}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {error_key: error}


async def test_user_flow_socket_missing(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.spiffe.config_flow.WorkloadApiSource",
        side_effect=SocketMissingError("no socket"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_SOCKET: DEFAULT_SOCKET}
        )
    assert result["errors"] == {CONF_SOCKET: "socket_missing"}


async def test_user_flow_cannot_connect_closes(
    hass: HomeAssistant, fake_source: FakeSource
) -> None:
    fake_source.reachable = False
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOCKET: DEFAULT_SOCKET}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert fake_source.closed

    fake_source.reachable = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOCKET: DEFAULT_SOCKET}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_single_instance(hass: HomeAssistant, fake_source: FakeSource) -> None:
    MockConfigEntry(domain=DOMAIN, data={CONF_SOCKET: DEFAULT_SOCKET}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reconfigure(hass: HomeAssistant, fake_source: FakeSource) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOCKET: DEFAULT_SOCKET})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOCKET: "unix:///share/spiffe/agent.sock"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SOCKET] == "unix:///share/spiffe/agent.sock"
    await hass.async_block_till_done()
    # The reload connected to the new address.
    assert fake_source.uris[-1] == "unix:///share/spiffe/agent.sock"


async def test_user_flow_no_identity(hass: HomeAssistant, fake_source: FakeSource) -> None:
    fake_source.bundles_denied = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOCKET: DEFAULT_SOCKET}
    )
    assert result["errors"] == {"base": "no_identity"}


async def test_options_flow_sets_identity(hass: HomeAssistant, fake_source: FakeSource) -> None:
    entry = await setup_spiffe(hass, options={})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SPIFFE_ID: f"  {HA_SPIFFE_ID}  ",
            CONF_EXPOSE_WHOAMI: True,
            CONF_SINGLE_USE_TOKENS: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_SPIFFE_ID: HA_SPIFFE_ID,
        CONF_EXPOSE_WHOAMI: True,
        CONF_SINGLE_USE_TOKENS: True,
    }


async def test_options_flow_defaults_and_empty_id(
    hass: HomeAssistant, fake_source: FakeSource
) -> None:
    entry = await setup_spiffe(hass, options={})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = _defaults(result)
    assert defaults[CONF_EXPOSE_WHOAMI] is False
    assert defaults[CONF_SINGLE_USE_TOKENS] is False

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EXPOSE_WHOAMI: False, CONF_SINGLE_USE_TOKENS: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_EXPOSE_WHOAMI: False, CONF_SINGLE_USE_TOKENS: False}


@pytest.mark.parametrize(
    "value",
    [
        "example.org/ha/core",
        "https://example.org/ha/core",
        "spiffe://example.org",
        "spiffe://example.org/",
        "spiffe://example.org/a b",
        "spiffe:///ha/core",
    ],
)
async def test_options_flow_rejects_invalid_spiffe_id(
    hass: HomeAssistant, fake_source: FakeSource, value: str
) -> None:
    entry = await setup_spiffe(hass, options={})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SPIFFE_ID: value, CONF_EXPOSE_WHOAMI: False, CONF_SINGLE_USE_TOKENS: False},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SPIFFE_ID: "invalid_spiffe_id"}
    assert entry.options == {}


def _defaults(result: dict) -> dict:
    return {k.schema: k.default() for k in result["data_schema"].schema if callable(k.default)}


async def _start_subentry(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_EVENT_WEBHOOK), context={"source": config_entries.SOURCE_USER}
    )


async def test_subentry_flow_creates_webhook(hass: HomeAssistant, fake_source: FakeSource) -> None:
    entry = await setup_spiffe(hass)
    result = await _start_subentry(hass, entry)
    assert result["type"] is FlowResultType.FORM
    defaults = _defaults(result)
    suggested = defaults[CONF_WEBHOOK_ID]
    assert len(suggested) >= 32

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: " Orders ",
            CONF_WEBHOOK_ID: suggested,
            CONF_ALLOWED_CALLERS: [CALLER, CALLER, OTHER],
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (subentry,) = entry.subentries.values()
    assert subentry.subentry_type == SUBENTRY_EVENT_WEBHOOK
    assert subentry.title == "Orders"
    assert subentry.data == {
        CONF_NAME: "Orders",
        CONF_WEBHOOK_ID: suggested,
        CONF_ALLOWED_CALLERS: [CALLER, OTHER],
    }


@pytest.mark.parametrize(
    ("user_input", "errors"),
    [
        (
            {CONF_NAME: "x", CONF_WEBHOOK_ID: "short", CONF_ALLOWED_CALLERS: [CALLER]},
            {CONF_WEBHOOK_ID: "invalid_webhook_id"},
        ),
        (
            {CONF_NAME: "x", CONF_WEBHOOK_ID: "has/slash/0123456", CONF_ALLOWED_CALLERS: [CALLER]},
            {CONF_WEBHOOK_ID: "invalid_webhook_id"},
        ),
        (
            {CONF_NAME: "  ", CONF_WEBHOOK_ID: "valid-id-0123456", CONF_ALLOWED_CALLERS: [CALLER]},
            {CONF_NAME: "name_required"},
        ),
        (
            {CONF_NAME: "x", CONF_WEBHOOK_ID: "valid-id-0123456", CONF_ALLOWED_CALLERS: []},
            {CONF_ALLOWED_CALLERS: "callers_required"},
        ),
        (
            {CONF_NAME: "x", CONF_WEBHOOK_ID: "valid-id-0123456", CONF_ALLOWED_CALLERS: ["nope"]},
            {CONF_ALLOWED_CALLERS: "invalid_spiffe_id"},
        ),
    ],
)
async def test_subentry_flow_validation(
    hass: HomeAssistant, fake_source: FakeSource, user_input: dict, errors: dict
) -> None:
    entry = await setup_spiffe(hass)
    result = await _start_subentry(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert not entry.subentries


async def test_subentry_flow_rejects_duplicate_webhook_id_and_reconfigures(
    hass: HomeAssistant, fake_source: FakeSource
) -> None:
    entry = await setup_spiffe(hass)
    first = await _start_subentry(hass, entry)
    await hass.config_entries.subentries.async_configure(
        first["flow_id"],
        {CONF_NAME: "A", CONF_WEBHOOK_ID: "valid-id-0123456", CONF_ALLOWED_CALLERS: [CALLER]},
    )
    await hass.async_block_till_done()

    second = await _start_subentry(hass, entry)
    second = await hass.config_entries.subentries.async_configure(
        second["flow_id"],
        {CONF_NAME: "B", CONF_WEBHOOK_ID: "valid-id-0123456", CONF_ALLOWED_CALLERS: [CALLER]},
    )
    assert second["errors"] == {CONF_WEBHOOK_ID: "webhook_id_in_use"}

    # Keeping its own ID while changing other things is fine.
    (subentry,) = entry.subentries.values()
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_EVENT_WEBHOOK),
        context={"source": config_entries.SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "A2", CONF_WEBHOOK_ID: "valid-id-0123456", CONF_ALLOWED_CALLERS: [OTHER]},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    (subentry,) = entry.subentries.values()
    assert subentry.title == "A2"
    assert subentry.data[CONF_ALLOWED_CALLERS] == [OTHER]


async def test_subentry_names_are_unique(hass: HomeAssistant, fake_source: FakeSource) -> None:
    entry = await setup_spiffe(hass)
    first = await _start_subentry(hass, entry)
    await hass.config_entries.subentries.async_configure(
        first["flow_id"],
        {CONF_NAME: "Orders", CONF_WEBHOOK_ID: "valid-id-0123456", CONF_ALLOWED_CALLERS: [CALLER]},
    )
    await hass.async_block_till_done()
    second = await _start_subentry(hass, entry)
    second = await hass.config_entries.subentries.async_configure(
        second["flow_id"],
        {
            CONF_NAME: " orders ",
            CONF_WEBHOOK_ID: "other-id-01234567",
            CONF_ALLOWED_CALLERS: [OTHER],
        },
    )
    assert second["errors"] == {CONF_NAME: "name_in_use"}
