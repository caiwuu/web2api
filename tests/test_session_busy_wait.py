import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.api.chat_handler import ChatHandler, _BusySessionState, _SessionBusyWait
from core.config.schema import AccountConfig, ProxyGroupConfig
from core.runtime.browser_manager import ClosedTabInfo
from core.runtime.keys import ProxyKey
from core.runtime.session_cache import SessionCache


class _FakePool:
    def __init__(self) -> None:
        self.group = ProxyGroupConfig(
            proxy_host="http://proxy",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-1",
        )
        self.account = AccountConfig(name="acc-1", type="claude", auth={})

    def reload(self, _groups):
        return None

    def account_id(self, group: ProxyGroupConfig, account: AccountConfig) -> str:
        return f"{group.fingerprint_id}:{account.name}"

    def get_account_by_id(self, account_id: str):
        if account_id == "fp-1:acc-1":
            return self.group, self.account
        return None

    def find_account(self, _type_name: str, _selector: str):
        return None


class _FakeBrowserManager:
    def __init__(self, *, active_requests: int = 0) -> None:
        self.closed_tabs: list[tuple[ProxyKey, str]] = []
        self.tab = SimpleNamespace(
            account_id="fp-1:acc-1",
            accepting_new=True,
            active_requests=active_requests,
            sessions={"session-1"},
            state="ready",
            last_used_at=0.0,
        )

    def get_tab(self, _proxy_key: ProxyKey, _type_name: str):
        return self.tab

    def acquire_tab(
        self, _proxy_key: ProxyKey, _type_name: str, max_concurrent: int
    ):
        if self.tab.active_requests >= max_concurrent:
            return None
        self.tab.active_requests += 1
        return object()

    async def ensure_browser(self, _proxy_key: ProxyKey, _proxy_pass: str):
        return object()

    def release_tab(self, _proxy_key: ProxyKey, _type_name: str) -> None:
        return None

    def unregister_session(
        self, _proxy_key: ProxyKey, _type_name: str, session_id: str
    ) -> None:
        self.tab.sessions.discard(session_id)

    async def close_tab(self, proxy_key: ProxyKey, type_name: str):
        self.closed_tabs.append((proxy_key, type_name))
        session_ids = list(self.tab.sessions)
        self.tab.sessions.clear()
        return ClosedTabInfo(
            proxy_key=proxy_key,
            type_name=type_name,
            account_id=self.tab.account_id,
            session_ids=session_ids,
        )


class _FakePlugin:
    def has_session(self, _session_id: str) -> bool:
        return True


class TestSessionBusyWait(unittest.IsolatedAsyncioTestCase):
    async def test_reuse_busy_session_raises_retryable_wait(self) -> None:
        handler = ChatHandler(_FakePool(), SessionCache(), _FakeBrowserManager())
        handler._session_busy_wait_seconds = 0.2
        handler._busy_sessions["session-1"] = _BusySessionState()
        handler._session_cache.put(
            "session-1",
            ProxyKey("http://proxy", "", "fp-1", True, "UTC"),
            "claude",
            "fp-1:acc-1",
        )

        with self.assertRaises(_SessionBusyWait) as ctx:
            await handler._reuse_session_target_locked(
                _FakePlugin(),
                "claude",
                "session-1",
                forced_account_selector=None,
            )

        self.assertAlmostEqual(ctx.exception.wait_seconds, 0.2)

    async def test_stale_busy_session_is_pruned_before_reuse(self) -> None:
        browser_manager = _FakeBrowserManager(active_requests=1)
        handler = ChatHandler(_FakePool(), SessionCache(), browser_manager)
        handler._session_busy_stale_seconds = 5.0
        handler._busy_sessions["session-1"] = _BusySessionState(started_at=10.0)
        handler._session_cache.put(
            "session-1",
            ProxyKey("http://proxy", "", "fp-1", True, "UTC"),
            "claude",
            "fp-1:acc-1",
        )

        with patch("core.api.chat_handler.time.time", return_value=20.0):
            target = await handler._reuse_session_target_locked(
                _FakePlugin(),
                "claude",
                "session-1",
                forced_account_selector=None,
            )

        self.assertIsNone(target)
        self.assertNotIn("session-1", handler._busy_sessions)
        self.assertIsNone(handler._session_cache.get("session-1"))
        self.assertEqual(len(browser_manager.closed_tabs), 1)

    async def test_full_tab_reuse_misses_instead_of_raising(self) -> None:
        browser_manager = _FakeBrowserManager(active_requests=1)
        handler = ChatHandler(_FakePool(), SessionCache(), browser_manager)
        handler._tab_max_concurrent = 1
        handler._session_cache.put(
            "session-1",
            ProxyKey("http://proxy", "", "fp-1", True, "UTC"),
            "claude",
            "fp-1:acc-1",
        )

        target = await handler._reuse_session_target_locked(
            _FakePlugin(),
            "claude",
            "session-1",
            forced_account_selector=None,
        )

        self.assertIsNone(target)


if __name__ == "__main__":
    unittest.main()
