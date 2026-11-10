"""A bounded memory of the tokens Home Assistant has accepted."""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, REPLAY_CACHE_MAX_ENTRIES


class ReplayCacheFull(Exception):
    """The cache holds its maximum of tokens that have not expired yet."""


class ReplayCache:
    """Remembers accepted tokens until they expire, so a token works once.

    Keys are the SHA-256 of the signing input (the header and payload segments), not of the whole
    token: the signature segment can be rewritten without invalidating it (base64 padding, the
    other valid form of an ECDSA signature), and such a variant must count as the same token. A
    memory dump of this cache holds no usable token.

    The cache is bounded: expired entries are dropped on every insert. If it is still full, it
    refuses to record anything (ReplayCacheFull) rather than forget a token that could still be
    replayed. Not thread safe; use it from the event loop only.
    """

    def __init__(self, max_entries: int = REPLAY_CACHE_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._expiries: OrderedDict[str, float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._expiries)

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.rsplit(".", 1)[0].encode()).hexdigest()

    def check_and_record(self, token: str, expires_at: float, now: float) -> bool:
        """Record the token. Return False, recording nothing, if it was already recorded.

        Raises ReplayCacheFull when there is no room for a token that is not recorded yet.
        """
        key = self._key(token)
        previous = self._expiries.get(key)
        if previous is not None and previous > now:
            return False
        self._drop_expired(now)
        if len(self._expiries) >= self._max_entries:
            raise ReplayCacheFull
        self._expiries[key] = expires_at
        return True

    def _drop_expired(self, now: float) -> None:
        expired = [key for key, expiry in self._expiries.items() if expiry <= now]
        for key in expired:
            del self._expiries[key]


DATA_REPLAY_CACHE: HassKey[ReplayCache] = HassKey(DOMAIN)
