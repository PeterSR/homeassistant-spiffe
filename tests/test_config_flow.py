"""Tests for the config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spiffe.const import CONF_SOCKET, DEFAULT_SOCKET, DOMAIN
from custom_components.spiffe.source import SocketMissingError

from .conftest import FakeSource


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
