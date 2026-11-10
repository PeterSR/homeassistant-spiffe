"""Helpers for SPIFFE IDs."""

from __future__ import annotations

from spiffe import SpiffeId
from spiffe.errors import PySpiffeError


def normalize_spiffe_id(value: str) -> str:
    """Return the canonical form of a SPIFFE ID that names a workload.

    Raises ValueError unless it is `spiffe://<trust-domain>/<path>`, with a path.
    """
    if not isinstance(value, str):
        raise ValueError("a SPIFFE ID must be a string")
    try:
        spiffe_id = SpiffeId(value.strip())
    except PySpiffeError as err:
        raise ValueError(str(err)) from err
    if not spiffe_id.path:
        raise ValueError("a SPIFFE ID of a workload needs a path after the trust domain")
    return str(spiffe_id)
