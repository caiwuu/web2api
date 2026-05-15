"""Cloudflare clearance helpers for site plugins."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from playwright.async_api import BrowserContext, Page

from core.config.settings import get

logger = logging.getLogger(__name__)


def is_cloudflare_challenge(status: int, text: str) -> bool:
    """Return true when a response body is a Cloudflare challenge page."""
    if status not in {403, 429, 503}:
        return False
    body = (text or "").lower()
    return any(
        marker in body
        for marker in (
            "just a moment",
            "challenge.cloudflare.com",
            "cf-chl",
            "cf_chl",
            "cloudflare",
        )
    )


def _config_int(section: str, key: str, default: int) -> int:
    try:
        return int(get(section, key, default))
    except (TypeError, ValueError):
        return default


def _cookie_domain_allowed(domain: str, site_domain: str) -> bool:
    normalized_domain = domain.lstrip(".")
    normalized_site = site_domain.lstrip(".")
    return normalized_domain == normalized_site or normalized_domain.endswith(
        f".{normalized_site}"
    )


def _cookie_to_playwright(cookie: dict[str, Any], site_domain: str) -> dict[str, Any] | None:
    name = str(cookie.get("name") or "").strip()
    value = cookie.get("value")
    if not name or value is None:
        return None
    domain = str(cookie.get("domain") or site_domain).strip() or site_domain
    if not _cookie_domain_allowed(domain, site_domain):
        return None

    result: dict[str, Any] = {
        "name": name,
        "value": str(value),
        "domain": domain,
        "path": str(cookie.get("path") or "/"),
    }
    for source_key, target_key in (
        ("secure", "secure"),
        ("httpOnly", "httpOnly"),
    ):
        if source_key in cookie:
            result[target_key] = bool(cookie[source_key])
    same_site = cookie.get("sameSite")
    if same_site in {"Strict", "Lax", "None"}:
        result["sameSite"] = same_site
    expires = cookie.get("expires", cookie.get("expiry"))
    if isinstance(expires, (int, float)) and expires > 0:
        result["expires"] = float(expires)
    return result


class FlareSolverrClearanceProvider:
    """Refresh and inject Cloudflare clearance cookies via FlareSolverr."""

    def __init__(self, flaresolverr_url: str = "", timeout_seconds: int = 60) -> None:
        self._flaresolverr_url = flaresolverr_url.rstrip("/")
        self._timeout_seconds = max(10, int(timeout_seconds))

    @classmethod
    def from_config(cls) -> "FlareSolverrClearanceProvider":
        url = (
            os.environ.get("FLARESOLVERR_URL", "").strip()
            or str(get("claude", "flaresolverr_url", "") or "").strip()
        )
        timeout = _config_int("claude", "flaresolverr_timeout_seconds", 60)
        return cls(url, timeout)

    @property
    def enabled(self) -> bool:
        return bool(self._flaresolverr_url)

    async def refresh(
        self,
        *,
        context: BrowserContext,
        page: Page,
        proxy_url: str,
        cookie_domain: str,
        start_url: str,
    ) -> bool:
        if not self.enabled:
            return False

        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": start_url,
            "maxTimeout": self._timeout_seconds * 1000,
        }
        if proxy_url:
            payload["proxy"] = {"url": proxy_url}

        def _post() -> dict[str, Any]:
            request = urllib_request.Request(
                f"{self._flaresolverr_url}/v1",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib_request.urlopen(
                request, timeout=self._timeout_seconds + 30
            ) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            result = await asyncio.to_thread(_post)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            logger.warning(
                "[cloudflare] flaresolverr http failed status=%s body=%s",
                exc.code,
                body,
            )
            return False
        except URLError as exc:
            logger.warning("[cloudflare] flaresolverr connection failed: %s", exc.reason)
            return False
        except Exception as exc:
            logger.warning("[cloudflare] flaresolverr request failed: %s", exc)
            return False

        if result.get("status") != "ok":
            logger.warning(
                "[cloudflare] flaresolverr non-ok status=%s message=%s",
                result.get("status"),
                result.get("message", ""),
            )
            return False

        cookies = (result.get("solution") or {}).get("cookies") or []
        user_agent = str((result.get("solution") or {}).get("userAgent") or "").strip()
        playwright_cookies = [
            converted
            for cookie in cookies
            if isinstance(cookie, dict)
            for converted in [_cookie_to_playwright(cookie, cookie_domain)]
            if converted is not None
        ]
        if not playwright_cookies:
            logger.warning("[cloudflare] flaresolverr returned no usable cookies")
            return False

        if user_agent:
            try:
                cdp = await context.new_cdp_session(page)
                await cdp.send(
                    "Network.setUserAgentOverride", {"userAgent": user_agent}
                )
                logger.info("[cloudflare] applied flaresolverr user-agent")
            except Exception as exc:
                logger.warning("[cloudflare] user-agent override failed: %s", exc)

        await context.add_cookies(playwright_cookies)
        logger.info(
            "[cloudflare] refreshed clearance cookies count=%s proxy=%s",
            len(playwright_cookies),
            proxy_url or "direct",
        )
        return True
