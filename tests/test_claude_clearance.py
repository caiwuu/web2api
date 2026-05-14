import json
import unittest
from unittest.mock import patch

from core.plugin.cloudflare import FlareSolverrClearanceProvider
from core.plugin.claude import ClaudePlugin


class _FakeContext:
    def __init__(self) -> None:
        self.cookies_store = [
            {
                "name": "cf_clearance",
                "value": "keep-clearance",
                "domain": ".claude.ai",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            },
            {
                "name": "sessionKey",
                "value": "old-session",
                "domain": ".claude.ai",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            },
        ]

    async def cookies(self) -> list[dict]:
        return list(self.cookies_store)

    async def clear_cookies(self) -> None:
        self.cookies_store = []

    async def add_cookies(self, cookies: list[dict]) -> None:
        for cookie in cookies:
            self.cookies_store = [
                existing
                for existing in self.cookies_store
                if not (
                    existing.get("name") == cookie.get("name")
                    and existing.get("domain") == cookie.get("domain")
                    and existing.get("path", "/") == cookie.get("path", "/")
                )
            ]
            self.cookies_store.append(cookie)


class _FakeAuthPage:
    url = "https://claude.ai/"

    async def evaluate(self, *_args: object) -> None:
        return None

    async def goto(self, *_args: object, **_kwargs: object) -> None:
        return None


class _FakeFetchPage:
    url = "https://claude.ai/"

    def __init__(self) -> None:
        self.fetch_calls = 0

    async def evaluate(self, *_args: object) -> dict:
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            return {
                "status": 403,
                "url": "https://claude.ai/api/account",
                "text": "<title>Just a moment...</title><script src=\"https://challenges.cloudflare.com\"></script>",
            }
        return {
            "status": 200,
            "url": "https://claude.ai/api/account",
            "text": json.dumps(
                {
                    "memberships": [
                        {"organization": {"uuid": "org-from-retry"}},
                    ]
                }
            ),
        }


class _FakeClearanceProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def refresh(
        self,
        *,
        context: object,
        page: object,
        proxy_url: str,
        cookie_domain: str,
        start_url: str,
    ) -> bool:
        del page, cookie_domain, start_url
        self.calls.append(proxy_url)
        await context.add_cookies(
            [
                {
                    "name": "cf_clearance",
                    "value": "fresh-clearance",
                    "domain": ".claude.ai",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        )
        return True


class _FakeCdpSession:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict) -> None:
        self.sent.append((method, params))


class _FakeSolverContext(_FakeContext):
    def __init__(self) -> None:
        super().__init__()
        self.cdp = _FakeCdpSession()

    async def new_cdp_session(self, _page: object) -> _FakeCdpSession:
        return self.cdp


class _FakeHttpResponse:
    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "status": "ok",
                "solution": {
                    "userAgent": "Mozilla/5.0 FlareSolverr-UA",
                    "cookies": [
                        {
                            "name": "cf_clearance",
                            "value": "solver-clearance",
                            "domain": ".claude.ai",
                            "path": "/",
                            "secure": True,
                        }
                    ],
                },
            }
        ).encode()


class TestClaudeClearance(unittest.IsolatedAsyncioTestCase):
    async def test_apply_auth_preserves_cloudflare_cookie(self) -> None:
        plugin = ClaudePlugin()
        context = _FakeContext()
        page = _FakeAuthPage()

        await plugin.apply_auth(
            context,
            page,
            {"sessionKey": "new-session"},
            reload=False,
        )

        cookies = {(c["domain"], c["name"]): c["value"] for c in context.cookies_store}
        self.assertEqual(cookies[(".claude.ai", "cf_clearance")], "keep-clearance")
        self.assertEqual(cookies[(".claude.ai", "sessionKey")], "new-session")

    async def test_fetch_site_context_refreshes_clearance_on_cloudflare_challenge(
        self,
    ) -> None:
        plugin = ClaudePlugin()
        provider = _FakeClearanceProvider()
        plugin._clearance_provider = provider
        context = _FakeContext()
        page = _FakeFetchPage()

        site_context = await plugin.fetch_site_context(
            context,
            page,
            proxy_url="http://host.docker.internal:3891",
        )

        self.assertEqual(site_context, {"org_uuid": "org-from-retry"})
        self.assertEqual(provider.calls, ["http://host.docker.internal:3891"])
        self.assertEqual(page.fetch_calls, 2)

    async def test_flaresolverr_provider_applies_returned_user_agent(self) -> None:
        provider = FlareSolverrClearanceProvider("http://flaresolverr:8191", 10)
        context = _FakeSolverContext()
        page = _FakeAuthPage()

        with patch(
            "core.plugin.cloudflare.urllib_request.urlopen",
            return_value=_FakeHttpResponse(),
        ):
            refreshed = await provider.refresh(
                context=context,
                page=page,
                proxy_url="http://host.docker.internal:3891",
                cookie_domain=".claude.ai",
                start_url="https://claude.ai",
            )

        self.assertTrue(refreshed)
        self.assertIn(
            (
                "Network.setUserAgentOverride",
                {"userAgent": "Mozilla/5.0 FlareSolverr-UA"},
            ),
            context.cdp.sent,
        )


if __name__ == "__main__":
    unittest.main()
