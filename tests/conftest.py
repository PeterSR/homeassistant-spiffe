"""Fixtures for the SPIFFE integration tests."""

from __future__ import annotations

import time
from collections.abc import Generator, Iterable
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from spiffe import JwtBundle, JwtBundleSet, TrustDomain

from custom_components.spiffe.source import (
    FetchedJwtSvid,
    SourceUnavailableError,
    SpiffeSource,
    socket_path_from_uri,
)

TRUST_DOMAIN = "example.org"
SPIFFE_ID = "spiffe://example.org/ha/core"
KEY_ID = "test-key"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration from custom_components."""


class Signer:
    """An ES256 key that mints JWT-SVIDs, and the bundle that trusts it."""

    def __init__(self, trust_domain: str = TRUST_DOMAIN, key_id: str = KEY_ID) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.key_id = key_id
        self.trust_domain = trust_domain

    def bundles(self) -> JwtBundleSet:
        td = TrustDomain(self.trust_domain)
        return JwtBundleSet({td: JwtBundle(td, {self.key_id: self.key.public_key()})})

    def token(
        self,
        audience: Iterable[str],
        subject: str = SPIFFE_ID,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "sub": subject,
                "aud": sorted(audience),
                "iat": now,
                "exp": now + int(lifetime.total_seconds()),
            },
            self.key,
            algorithm="ES256",
            headers={"kid": self.key_id, "typ": "JWT"},
        )


class FakeSource(SpiffeSource):
    """A SpiffeSource whose answers the test controls."""

    def __init__(self, signer: Signer) -> None:
        self.signer = signer
        self.reachable = True
        self.permission_denied = False
        self.x509 = False
        self.closed = False
        self.close_calls = 0
        self.svid_requests: list[tuple[set[str], str | None]] = []

    def _check(self) -> None:
        if not self.reachable:
            raise SourceUnavailableError("connection refused (StatusCode.UNAVAILABLE)")

    def fetch_jwt_svid(
        self, audiences: Iterable[str], subject: str | None = None
    ) -> FetchedJwtSvid:
        self._check()
        wanted = {audiences} if isinstance(audiences, str) else set(audiences)
        if not wanted or any(not a for a in wanted):
            raise ValueError("at least one non-empty audience is required")
        if self.permission_denied:
            raise SourceUnavailableError(
                "no identity issued (StatusCode.PERMISSION_DENIED)", permission_denied=True
            )
        self.svid_requests.append((wanted, subject))
        token = self.signer.token(wanted, subject or SPIFFE_ID)
        return FetchedJwtSvid(
            token=token,
            spiffe_id=subject or SPIFFE_ID,
            expiry=datetime.now(UTC) + timedelta(minutes=5),
        )

    def fetch_jwt_bundles(self) -> JwtBundleSet:
        self._check()
        return self.signer.bundles()

    def x509_available(self) -> bool:
        return self.x509

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1


@pytest.fixture
def signer() -> Signer:
    return Signer()


@pytest.fixture
def fake_source(signer: Signer) -> Generator[FakeSource]:
    """Replace the Workload API with a FakeSource everywhere the integration builds one."""
    source = FakeSource(signer)

    def build(uri: str, timeout: float) -> FakeSource:
        # Keep the address validation real, so the tests cover it.
        socket_path_from_uri(uri)
        if source.closed:
            source.closed = False
        return source

    with (
        patch("custom_components.spiffe.WorkloadApiSource", side_effect=build),
        patch("custom_components.spiffe.config_flow.WorkloadApiSource", side_effect=build),
    ):
        yield source
