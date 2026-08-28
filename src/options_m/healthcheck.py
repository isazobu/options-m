"""Container health probe.

Run as ``python -m options_m.healthcheck``. Kept dependency-free and separate
from the app so the probe cannot fail for reasons unrelated to health.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 5


def main() -> int:
    port = os.getenv("PORT", "8080")
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"health check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
