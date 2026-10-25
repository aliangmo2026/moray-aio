"""moray-aio HTTP client helper (standard library only): attaches the X-API-Key
auth header consistently.

External systems (or scripts in this repo) calling the moray-aio HTTP API should
use this: the key is read exclusively from the MORAY_AIO_API_KEY environment
variable (never hardcoded into source, examples or tests) and sent as a request
header when non-empty.
"""

from __future__ import annotations

import os
from typing import Optional

API_KEY_HEADER = "X-API-Key"


def api_key_from_env() -> str:
    """Read the API key from the environment (empty string = auth disabled server-side)."""
    return os.environ.get("MORAY_AIO_API_KEY", "").strip()


def build_headers(extra: Optional[dict] = None) -> dict:
    """Build request headers: merge caller-provided headers and attach X-API-Key
    when a key is configured."""
    headers = dict(extra or {})
    key = api_key_from_env()
    if key:
        headers[API_KEY_HEADER] = key
    return headers
