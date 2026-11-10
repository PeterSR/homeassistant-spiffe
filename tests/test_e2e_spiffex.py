"""End-to-end test of the Workload API source against a real agent.

spiffex is used here because its `dev` command sets up a whole local trust domain in one step.
The integration itself knows nothing about spiffex; any Workload API would do.

Skipped unless SPIFFEX names a spiffex binary, for example:

    SPIFFEX=$(command -v spiffex) pytest tests/test_e2e_spiffex.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import pytest_socket
from aiohttp.test_utils import make_mocked_request
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spiffe import (
    SpiffeAuthError,
    async_fetch_jwt_svid,
    async_verify_request,
)
from custom_components.spiffe.const import (
    CONF_SINGLE_USE_TOKENS,
    CONF_SOCKET,
    CONF_SPIFFE_ID,
    DOMAIN,
)
from custom_components.spiffe.source import (
    SourceUnavailableError,
    WorkloadApiSource,
    validate_jwt_svid,
)

SPIFFEX = os.environ.get("SPIFFEX")

pytestmark = pytest.mark.skipif(not SPIFFEX, reason="set SPIFFEX to a spiffex binary")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(path: Path, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise TimeoutError(f"{path} did not appear")


@pytest.fixture(params=[False, True], ids=["jwt-only", "with-ca"])
def agent_socket(
    request: pytest.FixtureRequest,
) -> Generator[tuple[str, bool, subprocess.Popen[bytes]]]:
    with_ca: bool = request.param
    # Home Assistant's test plugin blocks TCP sockets; these tests talk to real local processes.
    pytest_socket.enable_socket()
    port = _free_port()
    # Short base directory: a Unix socket path must stay under about 100 bytes.
    with tempfile.TemporaryDirectory(prefix="sx", dir="/tmp") as base:
        td = Path(base) / "td"
        args = [SPIFFEX, "dev", "-dir", str(td), "-listen", f"127.0.0.1:{port}"]
        if with_ca:
            args.append("-ca")
        subprocess.run(args, check=True, capture_output=True)
        sock = Path(base) / "agent.sock"
        procs = [
            subprocess.Popen(
                [
                    SPIFFEX,
                    "issuer",
                    "-config",
                    str(td / "issuer.yaml"),
                    "-listen",
                    f"127.0.0.1:{port}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ),
        ]
        try:
            time.sleep(0.5)
            procs.append(
                subprocess.Popen(
                    [
                        SPIFFEX,
                        "agent",
                        "-issuer",
                        f"http://127.0.0.1:{port}",
                        "-client-id",
                        "dev/orders",
                        "-key-file",
                        str(td / "workloads/orders.key"),
                        "-socket",
                        str(sock),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            _wait_for(sock)
            yield f"unix://{sock}", with_ca, procs[1]
        finally:
            for proc in reversed(procs):
                proc.terminate()
                proc.wait(timeout=10)


def test_fetch_and_validate(
    agent_socket: tuple[str, bool, subprocess.Popen[bytes]],
) -> None:
    uri, with_ca, _ = agent_socket
    source = WorkloadApiSource(uri, 10)
    try:
        bundles = source.fetch_jwt_bundles()
        assert {b.trust_domain.name for b in bundles.bundles} == {"spiffex.test"}

        svid = source.fetch_jwt_svid("spiffe://spiffex.test/dev/billing")
        assert svid.spiffe_id == "spiffe://spiffex.test/dev/orders"
        checked = validate_jwt_svid(bundles, svid.token, "spiffe://spiffex.test/dev/billing")
        assert checked.spiffe_id == svid.spiffe_id

        assert source.x509_available() is with_ca
    finally:
        source.close()


def test_identity_not_admitted(
    agent_socket: tuple[str, bool, subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    """An agent whose key the issuer does not know serves bundles but no tokens."""
    uri, _, agent = agent_socket
    issuer = next(a for a in agent.args if a.startswith("http://"))
    key = tmp_path / "stranger.key"
    subprocess.run([SPIFFEX, "key", "generate", "-out", str(key)], check=True, capture_output=True)
    sock = Path(uri.removeprefix("unix://")).with_name("stranger.sock")
    stranger = subprocess.Popen(
        [
            SPIFFEX,
            "agent",
            "-issuer",
            issuer,
            "-client-id",
            "dev/stranger",
            "-key-file",
            str(key),
            "-socket",
            str(sock),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(sock)
        source = WorkloadApiSource(f"unix://{sock}", 10)
        try:
            source.fetch_jwt_bundles()
            with pytest.raises(SourceUnavailableError) as err:
                source.fetch_jwt_svid("spiffe://spiffex.test/dev/billing")
            assert err.value.permission_denied
        finally:
            source.close()
    finally:
        stranger.terminate()
        stranger.wait(timeout=10)


def test_agent_stopped(agent_socket: tuple[str, bool, subprocess.Popen[bytes]]) -> None:
    """An agent that crashes leaves its socket file behind; calls then fail with a clear error."""
    uri, _, agent = agent_socket
    source = WorkloadApiSource(uri, 3)
    try:
        source.fetch_jwt_bundles()
        agent.kill()
        agent.wait(timeout=10)
        assert Path(uri.removeprefix("unix://")).exists()
        with pytest.raises(SourceUnavailableError):
            source.fetch_jwt_bundles()
    finally:
        source.close()


async def _verify_setup(
    hass: HomeAssistant, uri: str, me: str, *, single_use: bool = False
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SOCKET: uri},
        options={CONF_SPIFFE_ID: me, CONF_SINGLE_USE_TOKENS: single_use},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _request(token: str):
    return make_mocked_request("POST", "/", headers={"Authorization": f"Bearer {token}"})


async def test_verify_request(
    hass: HomeAssistant, agent_socket: tuple[str, bool, subprocess.Popen[bytes]]
) -> None:
    """A token the agent minted verifies for the caller it names."""
    uri, _, _ = agent_socket
    me = "spiffe://spiffex.test/dev/billing"
    caller = "spiffe://spiffex.test/dev/orders"  # the identity the agent hands out
    entry = await _verify_setup(hass, uri, me)

    token = await async_fetch_jwt_svid(hass, me)
    assert await async_verify_request(hass, _request(token), allowed_callers=[caller]) == caller

    with pytest.raises(SpiffeAuthError) as refused:
        await async_verify_request(hass, _request(token), allowed_callers=[me])
    assert refused.value.status == 403

    wrong_audience = await async_fetch_jwt_svid(hass, "spiffe://spiffex.test/dev/elsewhere")
    with pytest.raises(SpiffeAuthError) as wrong:
        await async_verify_request(hass, _request(wrong_audience), allowed_callers=[caller])
    assert wrong.value.status == 401

    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("single_use", [False, True], ids=["default", "single-use"])
async def test_cached_tokens(
    hass: HomeAssistant,
    agent_socket: tuple[str, bool, subprocess.Popen[bytes]],
    single_use: bool,
) -> None:
    """The agent caches tokens, so two fetches are one token: reusable unless single use is on."""
    uri, _, _ = agent_socket
    me = "spiffe://spiffex.test/dev/billing"
    caller = "spiffe://spiffex.test/dev/orders"
    await _verify_setup(hass, uri, me, single_use=single_use)

    first = await async_fetch_jwt_svid(hass, me)
    second = await async_fetch_jwt_svid(hass, me)
    assert first == second

    assert await async_verify_request(hass, _request(first), allowed_callers=[caller]) == caller
    if single_use:
        with pytest.raises(SpiffeAuthError) as replayed:
            await async_verify_request(hass, _request(second), allowed_callers=[caller])
        assert replayed.value.status == 401
    else:
        assert (
            await async_verify_request(hass, _request(second), allowed_callers=[caller]) == caller
        )
