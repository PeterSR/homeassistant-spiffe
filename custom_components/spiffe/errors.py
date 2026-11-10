"""Errors of the SPIFFE integration's public API."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class SpiffeNotReady(HomeAssistantError):
    """The SPIFFE integration is not set up or not loaded.

    A caller that needs an identity to work should raise ConfigEntryNotReady when it gets this, so
    Home Assistant retries its setup later.
    """


class SpiffeError(HomeAssistantError):
    """The SPIFFE source could not provide what was asked, or a token did not validate."""


class SpiffeAuthError(SpiffeError):
    """An incoming request was not authenticated (401) or not allowed (403).

    The message is for logs. Never send it to the caller of the request.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
