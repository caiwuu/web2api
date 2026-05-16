import asyncio
import unittest
from collections.abc import AsyncIterator

from core.api.chat_handler import (
    ChatHandler,
    _RequestTarget,
    _is_transient_browser_error,
)
from core.api.conv_parser import session_id_suffix
from core.api.schemas import OpenAIChatRequest
from core.config.schema import AccountConfig, ProxyGroupConfig
from core.plugin.base import AbstractPlugin, PluginRegistry
from core.plugin.helpers import stream_raw_via_page_fetch
from core.runtime.keys import ProxyKey


class _FakeCdpSession:
    def on(self, *_args: object) -> None:
        return None

    async def send(self, *_args: object) -> None:
        return None

    async def detach(self) -> None:
        return None


class _FakeContext:
    async def new_cdp_session(self, _page: object) -> _FakeCdpSession:
        return _FakeCdpSession()


class _SleepingPage:
    url = "https://claude.ai/login"

    async def evaluate(self, *_args: object) -> None:
        await asyncio.sleep(60)


class _FailCreateConversationOncePlugin(AbstractPlugin):
    type_name = "create-conv-test"

    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0
        self.stream_calls = 0

    async def create_conversation(self, *_args: object, **_kwargs: object) -> str | None:
        self.create_calls += 1
        if self.create_calls == 1:
            return None
        session_id = "session-ok"
        self._session_state[session_id] = {}
        return session_id

    async def stream_completion(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        self.stream_calls += 1
        yield "OK"


class _FailOncePlugin(AbstractPlugin):
    type_name = "transient-test"

    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0
        self.stream_calls = 0

    async def create_conversation(self, *_args: object, **_kwargs: object) -> str:
        self.create_calls += 1
        session_id = f"session-{self.create_calls}"
        self._session_state[session_id] = {}
        return session_id

    async def stream_completion(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        self.stream_calls += 1
        if self.stream_calls == 1:
            raise RuntimeError("Page.evaluate: Target crashed")
        yield "OK"


class _FakeBrowserManager:
    def __init__(self) -> None:
        self.drained: list[tuple[str, str]] = []
        self.released: list[tuple[str, str]] = []

    def register_session(
        self, _proxy_key: ProxyKey, _type_name: str, _session_id: str
    ) -> None:
        return None

    def mark_tab_draining(
        self,
        proxy_key: ProxyKey,
        type_name: str,
        *,
        frozen_until: int | None = None,
    ) -> None:
        del frozen_until
        self.drained.append((proxy_key.fingerprint_id, type_name))

    def release_tab(self, proxy_key: ProxyKey, type_name: str) -> None:
        self.released.append((proxy_key.fingerprint_id, type_name))

    def get_tab(self, _proxy_key: ProxyKey, _type_name: str) -> None:
        return None


class _FakeSessionCache:
    def put(self, *_args: object) -> None:
        return None


class _FakePool:
    def account_id(self, group: ProxyGroupConfig, account: AccountConfig) -> str:
        return f"{group.fingerprint_id}:{account.name}"


class TestTransientBrowserFailures(unittest.IsolatedAsyncioTestCase):
    def test_navigation_connection_closed_is_transient_browser_error(self) -> None:
        err = RuntimeError(
            "Page.goto: net::ERR_CONNECTION_CLOSED at https://claude.ai/"
        )

        self.assertTrue(_is_transient_browser_error(err))

    def test_proxy_tunnel_failure_is_transient_browser_error(self) -> None:
        err = RuntimeError(
            "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://claude.ai/"
        )

        self.assertTrue(_is_transient_browser_error(err))

    async def test_stream_first_chunk_timeout_raises_instead_of_ending_normally(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "流式首包超时"):
            async for _chunk in stream_raw_via_page_fetch(
                _FakeContext(),
                _SleepingPage(),
                "https://claude.ai/api/test",
                "{}",
                "req-timeout",
                read_timeout=0.01,
                first_chunk_timeout=0.01,
            ):
                pass

    async def test_create_conversation_failure_marks_tab_draining_and_retries(self) -> None:
        plugin = _FailCreateConversationOncePlugin()
        original_plugins = dict(PluginRegistry._plugins)
        PluginRegistry.register(plugin)
        browser_manager = _FakeBrowserManager()
        handler = ChatHandler(
            pool=_FakePool(),
            session_cache=_FakeSessionCache(),
            browser_manager=browser_manager,
        )

        group_1 = ProxyGroupConfig(
            proxy_host="http://proxy-1",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-1",
        )
        group_2 = ProxyGroupConfig(
            proxy_host="http://proxy-2",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-2",
        )
        account = AccountConfig(name="a", type="create-conv-test", auth={})
        targets = [
            _RequestTarget(
                proxy_key=ProxyKey("http://proxy-1", "", "fp-1", True, "UTC"),
                group=group_1,
                account=account,
                context=object(),
                page=object(),
                session_id=None,
                full_history=True,
            ),
            _RequestTarget(
                proxy_key=ProxyKey("http://proxy-2", "", "fp-2", True, "UTC"),
                group=group_2,
                account=account,
                context=object(),
                page=object(),
                session_id=None,
                full_history=True,
            ),
        ]

        async def _allocate(
            _type_name: str,
            **_kwargs: object,
        ) -> _RequestTarget:
            return targets.pop(0)

        async def _reconcile() -> None:
            return None

        handler._allocate_new_target_locked = _allocate
        handler._reconcile_tabs_locked = _reconcile

        try:
            req = OpenAIChatRequest(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
            chunks = [
                chunk async for chunk in handler._stream_completion("create-conv-test", req)
            ]
        finally:
            PluginRegistry._plugins = original_plugins

        self.assertEqual(chunks, ["OK", session_id_suffix("session-ok")])
        self.assertEqual(plugin.create_calls, 2)
        self.assertEqual(plugin.stream_calls, 1)
        self.assertEqual(browser_manager.drained, [("fp-1", "create-conv-test")])
        self.assertEqual(
            browser_manager.released,
            [("fp-1", "create-conv-test"), ("fp-2", "create-conv-test")],
        )

    async def test_target_crash_marks_tab_draining_and_retries(self) -> None:
        plugin = _FailOncePlugin()
        original_plugins = dict(PluginRegistry._plugins)
        PluginRegistry.register(plugin)
        browser_manager = _FakeBrowserManager()
        handler = ChatHandler(
            pool=_FakePool(),
            session_cache=_FakeSessionCache(),
            browser_manager=browser_manager,
        )

        group_1 = ProxyGroupConfig(
            proxy_host="http://proxy-1",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-1",
        )
        group_2 = ProxyGroupConfig(
            proxy_host="http://proxy-2",
            proxy_user="",
            proxy_pass="",
            fingerprint_id="fp-2",
        )
        account = AccountConfig(name="a", type="transient-test", auth={})
        targets = [
            _RequestTarget(
                proxy_key=ProxyKey("http://proxy-1", "", "fp-1", True, "UTC"),
                group=group_1,
                account=account,
                context=object(),
                page=object(),
                session_id=None,
                full_history=True,
            ),
            _RequestTarget(
                proxy_key=ProxyKey("http://proxy-2", "", "fp-2", True, "UTC"),
                group=group_2,
                account=account,
                context=object(),
                page=object(),
                session_id=None,
                full_history=True,
            ),
        ]

        async def _allocate(
            _type_name: str,
            **_kwargs: object,
        ) -> _RequestTarget:
            return targets.pop(0)

        async def _reconcile() -> None:
            return None

        handler._allocate_new_target_locked = _allocate
        handler._reconcile_tabs_locked = _reconcile

        try:
            req = OpenAIChatRequest(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
            )
            chunks = [
                chunk async for chunk in handler._stream_completion("transient-test", req)
            ]
        finally:
            PluginRegistry._plugins = original_plugins

        self.assertEqual(chunks, ["OK", session_id_suffix("session-2")])
        self.assertEqual(plugin.create_calls, 2)
        self.assertEqual(plugin.stream_calls, 2)
        self.assertEqual(browser_manager.drained, [("fp-1", "transient-test")])
        self.assertEqual(
            browser_manager.released,
            [("fp-1", "transient-test"), ("fp-2", "transient-test")],
        )


if __name__ == "__main__":
    unittest.main()
