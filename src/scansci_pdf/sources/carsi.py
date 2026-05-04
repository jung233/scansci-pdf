"""CARSI (Shibboleth/SAML) federated authentication for publisher access.

Provides institutional login through CARSI federation, supporting
publishers like Elsevier, Springer Nature, Wiley, ACS, etc.
"""

from __future__ import annotations

import json
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from ..config import DATA_DIR
from ..log import get_logger

log = get_logger()

_PUBLISHER_CONFIGS_FILE = DATA_DIR / "publisher_carsi.json"


@dataclass
class PublisherCARSIConfig:
    name: str
    domains: list[str]
    login_url: str
    search_selector: str
    result_selector: str
    success_url_pattern: str
    pdf_pattern: str


def _load_publisher_configs() -> dict[str, PublisherCARSIConfig]:
    if not _PUBLISHER_CONFIGS_FILE.exists():
        return {}
    data = json.loads(_PUBLISHER_CONFIGS_FILE.read_text(encoding="utf-8"))
    configs = {}
    for key, val in data.items():
        configs[key] = PublisherCARSIConfig(**val)
    return configs


def detect_publisher(url: str) -> str | None:
    """Detect publisher key from a URL."""
    hostname = urlparse(url).hostname or ""
    configs = _load_publisher_configs()
    for key, cfg in configs.items():
        for domain in cfg.domains:
            if domain in hostname:
                return key
    return None


class CARSIClient:
    """Manages CARSI/Shibboleth federated authentication with academic publishers."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._sessions: dict[str, requests.Session] = {}
        self._publisher_configs = _load_publisher_configs()
        self._cookie_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache"))) / "carsi_cookies"
        self._cookie_dir.mkdir(parents=True, exist_ok=True)

    def _cookie_path(self, publisher: str) -> Path:
        return self._cookie_dir / f"{publisher}.json"

    def _get_session(self, publisher: str) -> requests.Session:
        if publisher not in self._sessions:
            sess = requests.Session()
            sess.trust_env = False
            sess.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })
            self._sessions[publisher] = sess
        return self._sessions[publisher]

    def login(self, publisher: str, force: bool = False) -> bool:
        """Ensure we have a valid CARSI session for the given publisher."""
        if not force and self._try_load_cookies(publisher):
            log.info(f"   [CARSI] Loaded saved cookies for {publisher}")
            return True
        log.info(f"   [CARSI] No valid session for {publisher}. Opening browser...")
        return self._browser_login(publisher)

    def fetch(self, url: str, **kwargs) -> requests.Response | None:
        """Fetch a URL using CARSI-authenticated session."""
        publisher = detect_publisher(url)
        if not publisher:
            return None

        if not self.login(publisher):
            return None

        sess = self._get_session(publisher)
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)
        try:
            return sess.get(url, **kwargs)
        except requests.RequestException as e:
            log.warning(f"   [CARSI] Fetch failed: {e}")
            return None

    def _try_load_cookies(self, publisher: str) -> bool:
        cookie_file = self._cookie_path(publisher)
        if not cookie_file.exists():
            return False
        try:
            cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False

        sess = self._get_session(publisher)
        for cookie in cookies:
            sess.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )
        return self._validate_session(publisher)

    def _validate_session(self, publisher: str) -> bool:
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return False
        sess = self._get_session(publisher)
        try:
            resp = sess.get(cfg.login_url, timeout=15, allow_redirects=True)
            url_lower = resp.url.lower()
            if "login" in url_lower and "institutional" not in url_lower:
                return False
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _browser_login(self, publisher: str) -> bool:
        """Login via CARSI by opening the publisher's institutional login page."""
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            log.error(f"   [CARSI] Unknown publisher: {publisher}")
            return False

        idp_name = self.config.get("carsi_idp_name", "your university")
        print(f"\n  CARSI Login: {cfg.name}")
        print(f"  Steps:")
        print(f"  1. Search for: {idp_name}")
        print(f"  2. Log in with your campus credentials")
        print(f"  3. After login, paste the final URL below\n")

        webbrowser.open(cfg.login_url)

        try:
            final_url = input("  Paste the final URL (or Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Login cancelled.")
            return False

        if not final_url:
            print("  No URL provided. Skipped.")
            return False

        on_publisher = any(d in final_url for d in cfg.domains)
        on_login = any(x in final_url.lower() for x in ("login", "institutional", "wayf", "saml"))

        if on_publisher and not on_login:
            log.info(f"   [CARSI] Login confirmed: {final_url}")
            print("  CARSI login successful!")
            self._extract_chrome_cookies(publisher)
            return True

        print(f"  URL doesn't look like a successful login: {final_url}")
        return False

    def _extract_chrome_cookies(self, publisher: str) -> None:
        """Try to extract cookies from Chrome's cookie database."""
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return

        cookie_paths = [
            Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Cookies",
            Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies",
        ]

        for cookie_path in cookie_paths:
            if not cookie_path.exists():
                continue
            try:
                import shutil
                import sqlite3
                tmp_cookie = self._cookie_dir / "chrome_cookies_tmp.db"
                shutil.copy2(cookie_path, tmp_cookie)

                conn = sqlite3.connect(str(tmp_cookie))
                cursor = conn.cursor()

                cookies = []
                for domain in cfg.domains:
                    cursor.execute(
                        "SELECT name, value, host_key, path FROM cookies WHERE host_key LIKE ?",
                        (f"%{domain}%",),
                    )
                    cookies.extend(cursor.fetchall())
                conn.close()
                tmp_cookie.unlink(missing_ok=True)

                if cookies:
                    cookie_file = self._cookie_path(publisher)
                    cookie_data = [
                        {"name": n, "value": v, "domain": h, "path": p}
                        for n, v, h, p in cookies
                    ]
                    cookie_file.write_text(
                        json.dumps(cookie_data, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    log.info(f"   [CARSI] Extracted {len(cookie_data)} cookies from Chrome")
                    return
            except Exception as e:
                log.warning(f"   [CARSI] Chrome cookie extraction failed: {e}")

    def close(self):
        for sess in self._sessions.values():
            sess.close()
        self._sessions.clear()
