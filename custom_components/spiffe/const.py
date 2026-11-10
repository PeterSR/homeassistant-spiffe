"""Constants for the SPIFFE integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "spiffe"

CONF_SOCKET: Final = "socket"

# A documented convention, not a requirement: any provider can serve the Workload API here so
# that Core finds it without configuration. /config is the one folder both add-ons (apps) and
# Core can see on Home Assistant OS.
DEFAULT_SOCKET: Final = "unix:///config/.spiffe/agent.sock"

# Linux allows 108 bytes in sun_path including the terminating NUL; macOS allows 104. Stay under
# both so the same address works everywhere.
MAX_SOCKET_PATH_BYTES: Final = 103

CALL_TIMEOUT: Final = 10.0
X509_PROBE_TIMEOUT: Final = 15.0

# How often the Workload API is checked, and the cached trust bundles refreshed.
HEALTH_INTERVAL: Final = timedelta(seconds=60)

# Validating with older bundles than this is refused: a key the issuer removed in an emergency
# must not stay trusted for as long as the agent is unreachable.
MAX_BUNDLE_AGE: Final = timedelta(minutes=15)

# How long the Workload API may fail before a repair issue is raised.
UNREACHABLE_GRACE: Final = timedelta(minutes=5)

ISSUE_UNREACHABLE: Final = "workload_api_unreachable"
ISSUE_PERMISSION_DENIED: Final = "permission_denied"
