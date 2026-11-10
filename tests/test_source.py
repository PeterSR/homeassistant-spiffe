"""Tests for the source layer that does not need Home Assistant."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.spiffe.source import (
    FetchedJwtSvid,
    InvalidSocketError,
    SocketMissingError,
    SourceUnavailableError,
    SpiffeSourceError,
    WorkloadApiSource,
    bundle_key_count,
    socket_path_from_uri,
    validate_jwt_svid,
)

from .conftest import SPIFFE_ID, Signer


@pytest.mark.parametrize(
    ("uri", "path"),
    [
        ("unix:///config/.spiffe/agent.sock", "/config/.spiffe/agent.sock"),
        ("  unix:///run/spire/agent.sock ", "/run/spire/agent.sock"),
    ],
)
def test_socket_path_from_uri(uri: str, path: str) -> None:
    assert socket_path_from_uri(uri) == path


@pytest.mark.parametrize(
    "uri",
    [
        "/config/.spiffe/agent.sock",
        "tcp://127.0.0.1:8081",
        "unix://config/agent.sock",
        "unix:relative/agent.sock",
        "unix:///config/agent.sock?x=1",
        "unix:///" + "a" * 120,
        "",
    ],
)
def test_socket_path_from_uri_rejects(uri: str) -> None:
    with pytest.raises(InvalidSocketError):
        socket_path_from_uri(uri)


def test_missing_socket(tmp_path: Path) -> None:
    with pytest.raises(SocketMissingError):
        WorkloadApiSource(f"unix://{tmp_path}/agent.sock", 1)


def test_not_a_socket(tmp_path: Path) -> None:
    regular = tmp_path / "agent.sock"
    regular.write_text("")
    with pytest.raises(InvalidSocketError):
        WorkloadApiSource(f"unix://{regular}", 1)


def test_stale_socket_fails_on_fetch(tmp_path: Path) -> None:
    """A socket file nobody listens on, as a stopped agent leaves behind."""
    path = tmp_path / "a.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()
    source = WorkloadApiSource(f"unix://{path}", 1)
    try:
        with pytest.raises(SourceUnavailableError):
            source.fetch_jwt_bundles()
    finally:
        source.close()


def test_validate_jwt_svid(signer: Signer) -> None:
    token = signer.token({"https://api.example.org"})
    svid = validate_jwt_svid(signer.bundles(), token, "https://api.example.org")
    assert svid.spiffe_id == SPIFFE_ID


@pytest.mark.parametrize("case", ["audience", "expired", "other_key", "other_domain", "junk"])
def test_validate_jwt_svid_rejects(signer: Signer, case: str) -> None:
    bundles = signer.bundles()
    if case == "audience":
        token = signer.token({"someone-else"})
    elif case == "expired":
        token = signer.token({"aud"}, lifetime=timedelta(minutes=-1))
    elif case == "other_key":
        token = Signer().token({"aud"})
    elif case == "other_domain":
        token = Signer("other.example").token({"aud"}, "spiffe://other.example/x")
    else:
        token = "not-a-token"
    with pytest.raises(SpiffeSourceError):
        validate_jwt_svid(bundles, token, "aud")


def test_bundle_key_count(signer: Signer) -> None:
    assert bundle_key_count(signer.bundles()) == 1


def test_fetched_svid_repr_hides_token(signer: Signer) -> None:
    token = signer.token({"aud"})
    svid = FetchedJwtSvid(token=token, spiffe_id=SPIFFE_ID, expiry=datetime.now(UTC))
    assert token not in repr(svid)
    assert token not in str(svid)
