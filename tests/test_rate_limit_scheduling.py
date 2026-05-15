import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.account.pool import AccountPool
from core.api.chat_handler import ChatHandler, _AccountCooldownWait
from core.config.schema import AccountConfig, ProxyGroupConfig
from core.plugin.claude import ClaudePlugin
from core.runtime.keys import ProxyKey


class _FakeBrowserManager:
    def __init__(self, entries: list[tuple[ProxyKey, object]]) -> None:
        self._entries = entries
        self.acquired: list[tuple[str, str]] = []

    def list_browser_entries(self) -> list[tuple[ProxyKey, object]]:
        return list(self._entries)

    def get_tab(self, proxy_key: ProxyKey, type_name: str) -> object | None:
        for key, entry in self._entries:
            if key == proxy_key:
                return entry.tabs.get(type_name)
        return None

    def current_proxy_keys(self) -> list[ProxyKey]:
        return [key for key, _entry in self._entries]

    def acquire_tab(
        self,
        proxy_key: ProxyKey,
        type_name: str,
        _max_concurrent: int,
    ) -> object | None:
        for key, entry in self._entries:
            if key != proxy_key:
                continue
            tab = entry.tabs.get(type_name)
            if tab is None or not tab.accepting_new:
                return None
            tab.active_requests += 1
            tab.last_used_at = time.time()
            self.acquired.append((proxy_key.fingerprint_id, tab.account_id))
            return tab.page
        return None

    async def ensure_browser(self, _proxy_key: ProxyKey, _proxy_pass: str) -> object:
        return object()


class _FakeSessionCache:
    pass


class TestRateLimitScheduling(unittest.IsolatedAsyncioTestCase):
    def test_claude_429_without_reset_uses_short_configured_cooldown(self) -> None:
        plugin = ClaudePlugin()

        with patch("core.plugin.claude.time.time", return_value=1_000):
            unfreeze_at = plugin.on_http_error("HTTP 429 Too Many Requests", None)

        self.assertEqual(unfreeze_at, 1_060)

    async def test_recently_used_account_is_skipped_for_new_requests(self) -> None:
        group_1 = ProxyGroupConfig(
            proxy_host="http://proxy-1",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-1",
            timezone="UTC",
            accounts=[AccountConfig(name="a", type="claude", auth={})],
        )
        group_2 = ProxyGroupConfig(
            proxy_host="http://proxy-2",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-2",
            timezone="UTC",
            accounts=[AccountConfig(name="b", type="claude", auth={})],
        )
        pool = AccountPool([group_1, group_2])
        key_1 = ProxyKey("http://proxy-1", "", "fp-1", True, "UTC")
        key_2 = ProxyKey("http://proxy-2", "", "fp-2", True, "UTC")
        entries = [
            (
                key_1,
                SimpleNamespace(
                    tabs={
                        "claude": SimpleNamespace(
                            account_id="fp-1:a",
                            accepting_new=True,
                            active_requests=0,
                            last_used_at=10.0,
                            page=object(),
                        )
                    },
                    last_used_at=10.0,
                ),
            ),
            (
                key_2,
                SimpleNamespace(
                    tabs={
                        "claude": SimpleNamespace(
                            account_id="fp-2:b",
                            accepting_new=True,
                            active_requests=0,
                            last_used_at=20.0,
                            page=object(),
                        )
                    },
                    last_used_at=20.0,
                ),
            ),
        ]
        browser_manager = _FakeBrowserManager(entries)
        handler = ChatHandler(pool, _FakeSessionCache(), browser_manager)
        handler._account_min_interval_seconds = 10.0
        handler._account_last_started_at["fp-1:a"] = 100.0

        async def _noop_reconcile() -> None:
            return None

        handler._reconcile_tabs_locked = _noop_reconcile

        with patch("core.api.chat_handler.time.time", return_value=105.0):
            target = await handler._allocate_new_target_locked("claude")

        self.assertEqual(pool.account_id(target.group, target.account), "fp-2:b")
        self.assertEqual(browser_manager.acquired, [("fp-2", "fp-2:b")])

    async def test_forced_account_uses_requested_account(self) -> None:
        group_1 = ProxyGroupConfig(
            proxy_host="http://proxy-1",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-1",
            timezone="UTC",
            accounts=[AccountConfig(name="a", type="claude", auth={})],
        )
        group_2 = ProxyGroupConfig(
            proxy_host="http://proxy-2",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-2",
            timezone="UTC",
            accounts=[AccountConfig(name="b", type="claude", auth={})],
        )
        pool = AccountPool([group_1, group_2])
        key_1 = ProxyKey("http://proxy-1", "", "fp-1", True, "UTC")
        key_2 = ProxyKey("http://proxy-2", "", "fp-2", True, "UTC")
        entries = [
            (
                key_1,
                SimpleNamespace(
                    tabs={
                        "claude": SimpleNamespace(
                            account_id="fp-1:a",
                            accepting_new=True,
                            active_requests=0,
                            last_used_at=100.0,
                            page=object(),
                        )
                    },
                    last_used_at=100.0,
                ),
            ),
            (
                key_2,
                SimpleNamespace(
                    tabs={
                        "claude": SimpleNamespace(
                            account_id="fp-2:b",
                            accepting_new=True,
                            active_requests=0,
                            last_used_at=10.0,
                            page=object(),
                        )
                    },
                    last_used_at=10.0,
                ),
            ),
        ]
        browser_manager = _FakeBrowserManager(entries)
        handler = ChatHandler(pool, _FakeSessionCache(), browser_manager)

        async def _noop_reconcile() -> None:
            return None

        handler._reconcile_tabs_locked = _noop_reconcile

        target = await handler._allocate_new_target_locked(
            "claude",
            forced_account_selector="a",
        )

        self.assertEqual(pool.account_id(target.group, target.account), "fp-1:a")
        self.assertEqual(browser_manager.acquired, [("fp-1", "fp-1:a")])

    async def test_busy_open_tabs_wait_instead_of_reporting_no_account(self) -> None:
        group_1 = ProxyGroupConfig(
            proxy_host="http://proxy-1",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-1",
            timezone="UTC",
            accounts=[AccountConfig(name="a", type="claude", auth={})],
        )
        group_2 = ProxyGroupConfig(
            proxy_host="http://proxy-2",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-2",
            timezone="UTC",
            accounts=[AccountConfig(name="b", type="claude", auth={})],
        )
        pool = AccountPool([group_1, group_2])
        key_1 = ProxyKey("http://proxy-1", "", "fp-1", True, "UTC")
        key_2 = ProxyKey("http://proxy-2", "", "fp-2", True, "UTC")
        entries = [
            (
                key_1,
                SimpleNamespace(
                    tabs={
                        "claude": SimpleNamespace(
                            account_id="fp-1:a",
                            accepting_new=True,
                            active_requests=1,
                            last_used_at=10.0,
                            page=object(),
                        )
                    },
                    last_used_at=10.0,
                ),
            ),
            (
                key_2,
                SimpleNamespace(
                    tabs={
                        "claude": SimpleNamespace(
                            account_id="fp-2:b",
                            accepting_new=True,
                            active_requests=1,
                            last_used_at=20.0,
                            page=object(),
                        )
                    },
                    last_used_at=20.0,
                ),
            ),
        ]
        browser_manager = _FakeBrowserManager(entries)
        handler = ChatHandler(pool, _FakeSessionCache(), browser_manager)
        handler._tab_max_concurrent = 1

        async def _noop_reconcile() -> None:
            return None

        handler._reconcile_tabs_locked = _noop_reconcile

        with self.assertRaises(_AccountCooldownWait):
            await handler._allocate_new_target_locked("claude")


if __name__ == "__main__":
    unittest.main()
