"""Tests for the http_request action."""

from __future__ import annotations

import aiohttp
import pytest
from homeassistant.auth.models import User
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.spiffe import async_validate_jwt_svid
from custom_components.spiffe.const import DOMAIN, RESPONSE_LIMIT_BYTES

from .conftest import SPIFFE_ID, FakeSource, Signer
from .test_verify import setup_spiffe

URL = "https://api.example.org/hook"
AUDIENCE = "spiffe://example.org/orders/api"
JSON = {"Content-Type": "application/json; charset=utf-8"}


async def _call(hass: HomeAssistant, data: dict, context: Context | None = None) -> dict:
    return await hass.services.async_call(
        DOMAIN,
        "http_request",
        {"url": URL, "audience": AUDIENCE, **data},
        blocking=True,
        return_response=True,
        context=context,
    )


async def test_sends_bearer_for_the_audience(
    hass: HomeAssistant,
    fake_source: FakeSource,
    signer: Signer,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    await setup_spiffe(hass)
    aioclient_mock.post(URL, json={"ok": True}, headers=JSON)

    result = await _call(hass, {"json": {"a": 1}, "headers": {"X-Test": "1"}})

    assert result == {"status": 200, "json": {"ok": True}}
    assert fake_source.svid_requests == [({AUDIENCE}, None)]
    method, url, body, headers = aioclient_mock.mock_calls[0]
    assert (method.lower(), str(url), body) == ("post", URL, {"a": 1})
    assert headers["X-Test"] == "1"
    token = headers["Authorization"].removeprefix("Bearer ")
    assert await async_validate_jwt_svid(hass, token, AUDIENCE) == SPIFFE_ID


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
async def test_methods(
    hass: HomeAssistant,
    fake_source: FakeSource,
    aioclient_mock: AiohttpClientMocker,
    method: str,
) -> None:
    await setup_spiffe(hass)
    aioclient_mock.request(method, URL, text="done")
    assert await _call(hass, {"method": method}) == {"status": 200, "text": "done"}
    assert aioclient_mock.mock_calls[0][0].lower() == method.lower()


async def test_default_method_is_post(
    hass: HomeAssistant, fake_source: FakeSource, aioclient_mock: AiohttpClientMocker
) -> None:
    await setup_spiffe(hass)
    aioclient_mock.post(URL, text="x")
    await _call(hass, {})
    assert aioclient_mock.mock_calls[0][0].lower() == "post"


async def test_returns_error_status_and_text(
    hass: HomeAssistant, fake_source: FakeSource, aioclient_mock: AiohttpClientMocker
) -> None:
    await setup_spiffe(hass)
    aioclient_mock.post(URL, status=403, text="no entry")
    assert await _call(hass, {}) == {"status": 403, "text": "no entry"}


async def test_json_content_type_that_is_not_json_falls_back_to_text(
    hass: HomeAssistant, fake_source: FakeSource, aioclient_mock: AiohttpClientMocker
) -> None:
    await setup_spiffe(hass)
    aioclient_mock.post(URL, text="oops", headers={"Content-Type": "application/json"})
    assert await _call(hass, {}) == {"status": 200, "text": "oops"}


async def test_text_is_truncated(
    hass: HomeAssistant, fake_source: FakeSource, aioclient_mock: AiohttpClientMocker
) -> None:
    await setup_spiffe(hass)
    aioclient_mock.post(URL, text="a" * (RESPONSE_LIMIT_BYTES + 500))
    result = await _call(hass, {})
    assert result["status"] == 200
    assert result["text"] == "a" * RESPONSE_LIMIT_BYTES


async def test_large_json_is_returned_as_truncated_text(
    hass: HomeAssistant, fake_source: FakeSource, aioclient_mock: AiohttpClientMocker
) -> None:
    await setup_spiffe(hass)
    aioclient_mock.post(URL, json={"blob": "a" * (RESPONSE_LIMIT_BYTES + 10)}, headers=JSON)
    result = await _call(hass, {})
    assert "json" not in result
    assert len(result["text"]) == RESPONSE_LIMIT_BYTES


@pytest.mark.parametrize(
    "url", ["ftp://example.org/x", "file:///etc/passwd", "javascript:alert(1)", "example.org", ""]
)
async def test_refuses_non_http_url(
    hass: HomeAssistant,
    fake_source: FakeSource,
    aioclient_mock: AiohttpClientMocker,
    url: str,
) -> None:
    await setup_spiffe(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, {"url": url})
    assert aioclient_mock.call_count == 0
    assert fake_source.svid_requests == []


@pytest.mark.parametrize("name", ["Authorization", "authorization", " AUTHORIZATION"])
async def test_refuses_authorization_header(
    hass: HomeAssistant,
    fake_source: FakeSource,
    aioclient_mock: AiohttpClientMocker,
    name: str,
) -> None:
    await setup_spiffe(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, {"headers": {name: "Bearer stolen"}})
    assert aioclient_mock.call_count == 0


async def test_refuses_invalid_audience_and_method(
    hass: HomeAssistant, fake_source: FakeSource
) -> None:
    await setup_spiffe(hass)
    with pytest.raises(Exception, match="SPIFFE ID"):
        await _call(hass, {"audience": "https://api.example.org"})
    with pytest.raises(Exception, match="method"):
        await _call(hass, {"method": "TRACE"})


async def test_network_failure_raises_home_assistant_error(
    hass: HomeAssistant, fake_source: FakeSource, aioclient_mock: AiohttpClientMocker
) -> None:
    await setup_spiffe(hass)
    aioclient_mock.post(URL, exc=aiohttp.ClientConnectionError("boom"))
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, {})
    assert "Bearer" not in str(err.value)

    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, exc=TimeoutError())
    with pytest.raises(HomeAssistantError):
        await _call(hass, {})


async def test_is_admin_only(
    hass: HomeAssistant,
    fake_source: FakeSource,
    aioclient_mock: AiohttpClientMocker,
    hass_admin_user: User,
    hass_read_only_user: User,
) -> None:
    await setup_spiffe(hass)
    aioclient_mock.post(URL, text="ok")

    with pytest.raises(Unauthorized):
        await _call(hass, {}, Context(user_id=hass_read_only_user.id))
    assert aioclient_mock.call_count == 0
    assert fake_source.svid_requests == []

    assert await _call(hass, {}, Context(user_id=hass_admin_user.id)) == {
        "status": 200,
        "text": "ok",
    }


async def test_not_ready(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    # The action exists once Home Assistant has set up the component, even without an entry.
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(HomeAssistantError):
        await _call(hass, {})
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Test": "a\r\nX-Evil: 1"},
        {"X-Test": "a\nb"},
        {"X-Test": "a\x00b"},
        {"Bad Name": "1"},
        {"Bad:Name": "1"},
        {"": "1"},
        {"X-Ü": "1"},
    ],
)
async def test_refuses_invalid_headers(
    hass: HomeAssistant,
    fake_source: FakeSource,
    aioclient_mock: AiohttpClientMocker,
    headers: dict[str, str],
) -> None:
    await setup_spiffe(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, {"headers": headers})
    assert err.value.translation_key == "invalid_header"
    assert aioclient_mock.call_count == 0
    assert fake_source.svid_requests == []


async def test_error_messages_use_translation_and_hide_credentials(
    hass: HomeAssistant, fake_source: FakeSource, aioclient_mock: AiohttpClientMocker
) -> None:
    await setup_spiffe(hass)
    url = "https://user:secret@api.example.org:8443/hook"
    aioclient_mock.post(url, exc=aiohttp.ClientConnectionError(f"cannot reach {url}"))
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, {"url": url})
    assert err.value.translation_key == "request_failed"
    assert err.value.translation_placeholders == {
        "host": "api.example.org",
        "error": "ClientConnectionError",
    }
    assert "secret" not in str(err.value)

    with pytest.raises(ServiceValidationError) as invalid:
        await _call(hass, {"url": "ftp://x/y"})
    assert invalid.value.translation_key == "invalid_url"
    with pytest.raises(ServiceValidationError) as auth:
        await _call(hass, {"headers": {"authorization": "x"}})
    assert auth.value.translation_key == "authorization_header"
