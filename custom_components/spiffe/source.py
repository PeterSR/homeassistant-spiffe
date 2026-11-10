"""Where SPIFFE credentials come from.

The rest of the integration talks to a `SpiffeSource` and never to py-spiffe directly, so a
backend other than the Workload API (for example one that reads SVIDs written to disk) can be
added without changing the helpers other integrations call.

Every method here blocks. Call them in the executor.
"""

from __future__ import annotations

import logging
import os
import stat
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from spiffe import JwtBundleSet, JwtSvid, SpiffeId, WorkloadApiClient
from spiffe.errors import ArgumentError, PySpiffeError

from .const import MAX_SOCKET_PATH_BYTES, X509_PROBE_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class SpiffeSourceError(Exception):
    """A SPIFFE source could not do what was asked."""


class InvalidSocketError(SpiffeSourceError):
    """The Workload API address is malformed. Retrying will not help."""


class SocketMissingError(SpiffeSourceError):
    """Nothing exists at the socket path yet. The agent may still be starting."""


class SourceUnavailableError(SpiffeSourceError):
    """The source did not answer, or answered with an error."""

    def __init__(self, message: str, *, permission_denied: bool = False) -> None:
        super().__init__(message)
        self.permission_denied = permission_denied


class AudienceNotAvailableError(SpiffeSourceError):
    """The source cannot provide a JWT-SVID for this audience.

    A Workload API agent never raises this. A backend that can only serve audiences configured
    ahead of time (files written by a helper process) does.
    """


@dataclass(frozen=True, slots=True)
class FetchedJwtSvid:
    """A JWT-SVID and the facts about it that are safe to log or show."""

    token: str
    spiffe_id: str
    expiry: datetime

    def __repr__(self) -> str:  # never let the token reach a log line by accident
        return f"FetchedJwtSvid(spiffe_id={self.spiffe_id!r}, expiry={self.expiry.isoformat()})"


class SpiffeSource(ABC):
    """A provider of SPIFFE credentials and trust bundles."""

    @abstractmethod
    def fetch_jwt_svid(
        self, audiences: Iterable[str], subject: str | None = None
    ) -> FetchedJwtSvid:
        """Fetch a JWT-SVID for these audiences."""

    @abstractmethod
    def fetch_jwt_bundles(self) -> JwtBundleSet:
        """Fetch the JWT bundles of every trust domain the source knows."""

    @abstractmethod
    def x509_available(self) -> bool:
        """Return whether the source can hand out X.509-SVIDs."""

    @abstractmethod
    def close(self) -> None:
        """Release the source's resources."""


def socket_path_from_uri(uri: str) -> str:
    """Return the filesystem path of a `unix://` Workload API address.

    Raises InvalidSocketError for anything else: SPIFFE also allows `tcp://`, but a TCP Workload
    API cannot tell callers apart, so this integration does not accept one.
    """
    parsed = urlparse(uri.strip())
    if parsed.scheme != "unix":
        raise InvalidSocketError("the address must start with unix://")
    if parsed.netloc or parsed.query or parsed.fragment or parsed.params:
        raise InvalidSocketError("use the form unix:///absolute/path/to/agent.sock")
    path = parsed.path
    if not path.startswith("/"):
        raise InvalidSocketError("the socket path must be absolute")
    if len(path.encode()) > MAX_SOCKET_PATH_BYTES:
        raise InvalidSocketError(f"the socket path is longer than {MAX_SOCKET_PATH_BYTES} bytes")
    return path


def _audience_set(audiences: Iterable[str]) -> set[str]:
    if isinstance(audiences, str):
        audiences = [audiences]
    result = set(audiences)
    if not result or any(not isinstance(a, str) or not a.strip() for a in result):
        raise ValueError("at least one non-empty audience is required")
    return result


class WorkloadApiSource(SpiffeSource):
    """Credentials from a SPIFFE Workload API served on a Unix socket.

    Only one-shot calls are used, each with a timeout. py-spiffe's streaming sources retry some
    failures forever without telling anyone, which would hide a stopped agent.
    """

    def __init__(self, uri: str, timeout: float) -> None:
        path = socket_path_from_uri(uri)
        try:
            mode = os.stat(path).st_mode
        except FileNotFoundError as err:
            raise SocketMissingError(f"no socket at {path}") from err
        except OSError as err:
            raise SourceUnavailableError(f"cannot read {path}: {err.strerror}") from err
        if not stat.S_ISSOCK(mode):
            raise InvalidSocketError(f"{path} is not a socket")
        try:
            self._client = WorkloadApiClient(socket_path=f"unix://{path}", default_timeout=timeout)
        except ArgumentError as err:
            raise InvalidSocketError(str(err)) from err
        self._timeout = timeout

    def fetch_jwt_svid(
        self, audiences: Iterable[str], subject: str | None = None
    ) -> FetchedJwtSvid:
        wanted = _audience_set(audiences)
        try:
            spiffe_subject = SpiffeId(subject) if subject else None
        except PySpiffeError as err:
            raise ValueError(f"invalid SPIFFE ID {subject!r}: {err}") from err
        try:
            svid: JwtSvid = self._client.fetch_jwt_svid(audience=wanted, subject=spiffe_subject)
        except PySpiffeError as err:
            raise _unavailable(err) from err
        return FetchedJwtSvid(
            token=svid.token,
            spiffe_id=str(svid.spiffe_id),
            expiry=datetime.fromtimestamp(svid.expiry, tz=UTC),
        )

    def fetch_jwt_bundles(self) -> JwtBundleSet:
        try:
            return self._client.fetch_jwt_bundles()
        except PySpiffeError as err:
            raise _unavailable(err) from err

    def x509_available(self) -> bool:
        # A first X.509-SVID may take the agent a round trip to its issuer, so allow longer than
        # for other calls. Building an X509Source instead would retry UNIMPLEMENTED forever.
        try:
            self._client.fetch_x509_svid(timeout=X509_PROBE_TIMEOUT)
        except PySpiffeError as err:
            _LOGGER.info("X.509-SVIDs are not available, using JWT-SVIDs only: %s", err)
            return False
        return True

    def close(self) -> None:
        self._client.close()


def _unavailable(err: PySpiffeError) -> SourceUnavailableError:
    # py-spiffe folds the gRPC status code into the message text, for example
    # "Error fetching JWT-SVID: no identity issued (StatusCode.PERMISSION_DENIED)".
    message = str(err)
    return SourceUnavailableError(message, permission_denied="PERMISSION_DENIED" in message)


def bundle_key_count(bundles: JwtBundleSet) -> int:
    """Count the JWT authorities across every trust domain in a bundle set."""
    return sum(len(b.jwt_authorities) for b in bundles.bundles)


def validate_jwt_svid(bundles: JwtBundleSet, token: str, audience: str) -> FetchedJwtSvid:
    """Check a JWT-SVID someone presented against the cached bundles.

    Raises SpiffeSourceError if the token is malformed, from an unknown trust domain, signed by
    an unknown key, expired, or not for this audience, and ValueError for a bad audience.
    """
    if not isinstance(audience, str) or not audience.strip():
        raise ValueError("the audience must be a non-empty string")
    try:
        unverified = JwtSvid.parse_insecure(token, {audience})
        bundle = bundles.get_bundle_for_trust_domain(unverified.spiffe_id.trust_domain)
        if bundle is None:
            raise SpiffeSourceError(
                f"no bundle for trust domain {unverified.spiffe_id.trust_domain}"
            )
        svid = JwtSvid.parse_and_validate(token, bundle, {audience})
    except PySpiffeError as err:
        raise SpiffeSourceError(f"invalid JWT-SVID: {err}") from err
    return FetchedJwtSvid(
        token=svid.token,
        spiffe_id=str(svid.spiffe_id),
        expiry=datetime.fromtimestamp(svid.expiry, tz=UTC),
    )


__all__ = [
    "AudienceNotAvailableError",
    "FetchedJwtSvid",
    "InvalidSocketError",
    "SocketMissingError",
    "SourceUnavailableError",
    "SpiffeSource",
    "SpiffeSourceError",
    "WorkloadApiSource",
    "bundle_key_count",
    "socket_path_from_uri",
    "validate_jwt_svid",
]
