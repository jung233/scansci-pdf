"""FlareSolverr client for bypassing Cloudflare protection.

FlareSolverr must be running as a separate service (Docker or standalone).
See: https://github.com/FlareSolverr/FlareSolverr
"""

from __future__ import annotations

from typing import Any

import requests

from ..log import get_logger

log = get_logger()

DEFAULT_URL = "http://127.0.0.1:8191/v1"


class FlareSolverrClient:
    """HTTP client that uses FlareSolverr to bypass Cloudflare challenges."""

    def __init__(self, base_url: str = DEFAULT_URL):
        self.base_url = base_url.rstrip("/")

    def is_available(self) -> bool:
        """Check if FlareSolverr is running."""
        try:
            resp = requests.get(self.base_url, timeout=3)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def get(self, url: str, wait_seconds: int = 8) -> str | None:
        """Fetch a URL through FlareSolverr, returning HTML content.

        Uses fast-path retry: first tries with wait_seconds=0, then retries
        with the full wait if a challenge is detected.

        Returns:
            HTML string if successful, None if failed.
        """
        # Fast path: no wait
        html = self._request(url, wait_seconds=0)
        if html and len(html) > 1000:
            return html

        # Challenge detected or content too short, retry with wait
        log.info(f"   [FlareSolverr] Fast path failed, retrying with {wait_seconds}s wait...")
        return self._request(url, wait_seconds=wait_seconds)

    def _request(self, url: str, wait_seconds: int = 8) -> str | None:
        """Make a single FlareSolverr request."""
        payload = {
            "cmd": "request.get",
            "url": url,
            "waitInSeconds": wait_seconds,
            "disableMedia": True,
        }
        try:
            resp = requests.post(
                self.base_url,
                json=payload,
                timeout=wait_seconds + 30,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != "ok":
                log.warning(f"   [FlareSolverr] Status: {data.get('status')}")
                return None

            solution = data.get("solution", {})
            status_code = solution.get("status", 0)
            html = solution.get("response", "")

            if status_code == 200 and html:
                return html

            log.warning(f"   [FlareSolverr] Solution status: {status_code}")
            return None

        except requests.RequestException as e:
            log.warning(f"   [FlareSolverr] Request failed: {e}")
            return None
        except (KeyError, ValueError) as e:
            log.warning(f"   [FlareSolverr] Parse error: {e}")
            return None


def get_flaresolverr(config: dict[str, Any]) -> FlareSolverrClient | None:
    """Get a FlareSolverr client if available."""
    url = config.get("flaresolverr_url", DEFAULT_URL)
    client = FlareSolverrClient(url)
    if client.is_available():
        return client
    return None
