"""
浏览器管理器：按 ProxyKey 管理浏览器进程；每个浏览器内每个 type 仅保留一个 tab。

当前实现的职责：

- 一个 ProxyKey 对应一个 Chromium 进程
- 一个浏览器内，一个 type 只允许一个 page/tab
- tab 绑定一个 account，只有 drained 后才能切号
- tab 可承载多个 session，并记录活跃请求数与最近使用时间
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from core.runtime.local_proxy_forwarder import LocalProxyForwarder

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from core.constants import CDP_PORT_RANGE, CHROMIUM_BIN, TIMEZONE, user_data_dir
from core.runtime.keys import ProxyKey

logger = logging.getLogger(__name__)

CreatePageFn = Callable[[BrowserContext, Page | None], Coroutine[Any, Any, Page]]


async def _wait_for_cdp(
    host: str,
    port: int,
    max_attempts: int = 60,
    interval: float = 2.0,
    connect_timeout: float = 2.0,
) -> bool:
    for _ in range(max_attempts):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=connect_timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(interval)
    return False


def _is_cdp_listening(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            pass
        return True
    except OSError:
        return False


@dataclass
class TabRuntime:
    """浏览器中的一个 type tab。"""

    type_name: str
    page: Page
    account_id: str
    active_requests: int = 0
    accepting_new: bool = True
    state: str = "ready"
    last_used_at: float = field(default_factory=time.time)
    frozen_until: int | None = None
    sessions: set[str] = field(default_factory=set)


@dataclass
class BrowserEntry:
    """单个 ProxyKey 对应的浏览器运行时。"""

    proc: subprocess.Popen[Any]
    port: int
    browser: Browser
    context: BrowserContext
    stderr_path: Path | None = None
    tabs: dict[str, TabRuntime] = field(default_factory=dict)
    last_used_at: float = field(default_factory=time.time)
    proxy_forwarder: Any = None  # LocalProxyForwarder | None，仅 use_proxy 时非空


@dataclass
class ClosedTabInfo:
    """关闭 tab/browser 时回传的 session 清理信息。"""

    proxy_key: ProxyKey
    type_name: str
    account_id: str
    session_ids: list[str]


class BrowserManager:
    """按代理组管理浏览器及其 type -> tab 映射。"""

    def __init__(
        self,
        chromium_bin: str = CHROMIUM_BIN,
        headless: bool = False,
        no_sandbox: bool = False,
        disable_gpu: bool = False,
        disable_gpu_sandbox: bool = False,
        port_range: list[int] | None = None,
        cdp_wait_max_attempts: int = 90,
        cdp_wait_interval_seconds: float = 2.0,
        cdp_wait_connect_timeout_seconds: float = 2.0,
        download_dir: str | Path | None = None,
    ) -> None:
        self._chromium_bin = chromium_bin
        self._headless = headless
        self._no_sandbox = no_sandbox
        self._disable_gpu = disable_gpu
        self._disable_gpu_sandbox = disable_gpu_sandbox
        self._port_range = port_range or list(CDP_PORT_RANGE)
        self._entries: dict[ProxyKey, BrowserEntry] = {}
        self._available_ports: set[int] = set(self._port_range)
        self._playwright: Any = None
        self._cdp_wait_max_attempts = max(1, int(cdp_wait_max_attempts))
        self._cdp_wait_interval_seconds = max(0.05, float(cdp_wait_interval_seconds))
        self._cdp_wait_connect_timeout_seconds = max(
            0.2, float(cdp_wait_connect_timeout_seconds)
        )
        self._download_dir = download_dir

    def _stderr_log_path(self, proxy_key: ProxyKey, port: int) -> Path:
        log_dir = Path(tempfile.gettempdir()) / "web2api-browser-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / (
            f"{proxy_key.fingerprint_id}-{port}-{int(time.time())}.stderr.log"
        )

    @staticmethod
    def _read_stderr_tail(stderr_path: Path | None, max_chars: int = 4000) -> str:
        if stderr_path is None or not stderr_path.exists():
            return ""
        try:
            content = stderr_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        content = content.strip()
        if not content:
            return ""
        return content[-max_chars:]

    @staticmethod
    def _cleanup_stderr_log(stderr_path: Path | None) -> None:
        if stderr_path is None:
            return
        try:
            stderr_path.unlink(missing_ok=True)
        except Exception:
            pass

    def current_proxy_keys(self) -> list[ProxyKey]:
        return list(self._entries.keys())

    def browser_count(self) -> int:
        return len(self._entries)

    def list_browser_entries(self) -> list[tuple[ProxyKey, BrowserEntry]]:
        return list(self._entries.items())

    def get_browser_entry(self, proxy_key: ProxyKey) -> BrowserEntry | None:
        return self._entries.get(proxy_key)

    def get_tab(self, proxy_key: ProxyKey, type_name: str) -> TabRuntime | None:
        entry = self._entries.get(proxy_key)
        if entry is None:
            return None
        return entry.tabs.get(type_name)

    def browser_load(self, proxy_key: ProxyKey) -> int:
        entry = self._entries.get(proxy_key)
        if entry is None:
            return 0
        return sum(tab.active_requests for tab in entry.tabs.values())

    def touch_browser(self, proxy_key: ProxyKey) -> None:
        entry = self._entries.get(proxy_key)
        if entry is not None:
            entry.last_used_at = time.time()

    def _launch_process(
        self,
        proxy_key: ProxyKey,
        proxy_pass: str,
        port: int,
    ) -> tuple[subprocess.Popen[Any], Path, LocalProxyForwarder | None]:
        """启动 Chromium 进程（代理时使用本地转发鉴权，无扩展），使用指定 port。"""
        udd = user_data_dir(proxy_key.fingerprint_id)
        udd.mkdir(parents=True, exist_ok=True)

        if not Path(self._chromium_bin).exists():
            raise RuntimeError(f"Chromium 不存在: {self._chromium_bin}")

        args = [
            self._chromium_bin,
            f"--remote-debugging-port={port}",
            f"--fingerprint={proxy_key.fingerprint_id}",
            "--fingerprint-platform=windows",
            "--fingerprint-brand=Edge",
            f"--user-data-dir={udd}",
            f"--timezone={proxy_key.timezone or TIMEZONE}",
            "--force-webrtc-ip-handling-policy",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--disable-features=AsyncDNS",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        proxy_forwarder = None
        if proxy_key.use_proxy:
            from core.runtime.local_proxy_forwarder import (
                LocalProxyForwarder,
                UpstreamProxy,
                parse_proxy_server,
            )

            upstream_host, upstream_port = parse_proxy_server(proxy_key.proxy_host)
            upstream = UpstreamProxy(
                host=upstream_host,
                port=upstream_port,
                username=proxy_key.proxy_user,
                password=proxy_pass,
            )
            proxy_forwarder = LocalProxyForwarder(
                upstream,
                listen_host="127.0.0.1",
                listen_port=0,
                on_log=lambda msg: logger.debug("[proxy] %s", msg),
            )
            proxy_forwarder.start()
            args.append(f"--proxy-server={proxy_forwarder.proxy_url}")
        if self._headless:
            args.extend(
                [
                    "--headless=new",
                    "--window-size=1920,1080",
                ]
            )
        if self._headless or self._disable_gpu:
            args.append("--disable-gpu")
        if self._disable_gpu_sandbox:
            args.append("--disable-gpu-sandbox")
        if self._no_sandbox:
            args.extend(
                [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ]
            )
        env = os.environ.copy()
        env["NODE_OPTIONS"] = (
            env.get("NODE_OPTIONS") or ""
        ).strip() + " --no-deprecation"
        stderr_path = self._stderr_log_path(proxy_key, port)
        stderr_fp = stderr_path.open("ab")
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_fp,
                env=env,
            )
        finally:
            stderr_fp.close()
        return proc, stderr_path, proxy_forwarder

    async def ensure_browser(
        self,
        proxy_key: ProxyKey,
        proxy_pass: str,
    ) -> BrowserContext:
        """
        确保存在对应 proxy_key 的浏览器；若已有且存活则直接复用。
        """
        entry = self._entries.get(proxy_key)
        if entry is not None:
            if entry.proc.poll() is not None or not _is_cdp_listening(entry.port):
                await self._close_entry_async(proxy_key)
            else:
                entry.last_used_at = time.time()
                return entry.context

        if not self._available_ports:
            raise RuntimeError(
                "无可用 CDP 端口，当前并发浏览器数已达上限，请稍后重试或增大 cdp_port_count"
            )
        port = self._available_ports.pop()
        proc, stderr_path, proxy_forwarder = self._launch_process(
            proxy_key, proxy_pass, port
        )
        logger.info(
            "已启动 Chromium PID=%s port=%s mode=%s headless=%s no_sandbox=%s disable_gpu=%s disable_gpu_sandbox=%s，等待 CDP 就绪...",
            proc.pid,
            port,
            "proxy" if proxy_key.use_proxy else "direct",
            self._headless,
            self._no_sandbox,
            self._disable_gpu,
            self._disable_gpu_sandbox,
        )
        ok = await _wait_for_cdp(
            "127.0.0.1",
            port,
            max_attempts=self._cdp_wait_max_attempts,
            interval=self._cdp_wait_interval_seconds,
            connect_timeout=self._cdp_wait_connect_timeout_seconds,
        )
        if not ok:
            self._available_ports.add(port)
            if proxy_forwarder is not None:
                try:
                    proxy_forwarder.stop()
                except Exception:
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
            stderr_tail = self._read_stderr_tail(stderr_path)
            self._cleanup_stderr_log(stderr_path)
            if stderr_tail:
                logger.error(
                    "Chromium 启动失败，CDP 未就绪。stderr tail:\n%s",
                    stderr_tail,
                )
            raise RuntimeError("CDP 未在预期时间内就绪")

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        endpoint = f"http://127.0.0.1:{port}"
        try:
            browser = await self._playwright.chromium.connect_over_cdp(
                endpoint, timeout=10000
            )
        except Exception:
            self._available_ports.add(port)
            if proxy_forwarder is not None:
                try:
                    proxy_forwarder.stop()
                except Exception:
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
            stderr_tail = self._read_stderr_tail(stderr_path)
            self._cleanup_stderr_log(stderr_path)
            if stderr_tail:
                logger.error(
                    "Chromium 已监听 CDP 但 connect_over_cdp 失败。stderr tail:\n%s",
                    stderr_tail,
                )
            raise
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            await browser.close()
            self._available_ports.add(port)
            if proxy_forwarder is not None:
                try:
                    proxy_forwarder.stop()
                except Exception:
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
            self._cleanup_stderr_log(stderr_path)
            raise RuntimeError("浏览器无默认 context")

        # 通过 CDP 显式设置下载目录，避免 Playwright 接管下载后写入临时目录
        # 以及文件名被替换为 UUID。
        if self._download_dir is not None and str(self._download_dir).strip():
            download_dir = Path(self._download_dir).expanduser()
        else:
            # 默认：每个 fingerprint 的 user-data-dir 里单独维护 downloads，避免不同实例互相干扰
            download_dir = user_data_dir(proxy_key.fingerprint_id) / "downloads"
        try:
            download_dir.mkdir(parents=True, exist_ok=True)
            # Playwright 的 CDP 方法在 async/sync API 中返回类型不一定一致：
            # 这里做兼容：如果返回的是协程再 await，否则直接使用。
            cdp_session = browser.new_browser_cdp_session()
            if inspect.isawaitable(cdp_session):
                cdp_session = await cdp_session

            send_result = cdp_session.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(download_dir),
                },
            )
            if inspect.isawaitable(send_result):
                await send_result
        except Exception:
            logger.warning(
                "设置下载目录失败（将继续使用默认下载行为）proxy=%s download_dir=%s",
                proxy_key.fingerprint_id,
                download_dir,
                exc_info=True,
            )

        self._entries[proxy_key] = BrowserEntry(
            proc=proc,
            port=port,
            browser=browser,
            context=context,
            stderr_path=stderr_path,
            proxy_forwarder=proxy_forwarder,
        )
        return context

    async def open_tab(
        self,
        proxy_key: ProxyKey,
        proxy_pass: str,
        type_name: str,
        account_id: str,
        create_page_fn: CreatePageFn,
    ) -> TabRuntime:
        """在指定浏览器中创建一个 type tab，并绑定到 account。"""
        context = await self.ensure_browser(proxy_key, proxy_pass)
        entry = self._entries.get(proxy_key)
        if entry is None:
            raise RuntimeError("ensure_browser 未创建 entry")
        existing = entry.tabs.get(type_name)
        if existing is not None:
            return existing

        # 首个 tab 时复用 Chromium 默认空白页，避免多一个无用标签
        reuse_page = (
            context.pages[0] if (len(entry.tabs) == 0 and context.pages) else None
        )
        page = await create_page_fn(context, reuse_page)

        tab = TabRuntime(
            type_name=type_name,
            page=page,
            account_id=account_id,
        )
        entry.tabs[type_name] = tab
        entry.last_used_at = time.time()
        logger.info(
            "[tab] opened mode=%s proxy=%s type=%s account=%s",
            "proxy" if proxy_key.use_proxy else "direct",
            proxy_key.fingerprint_id,
            type_name,
            account_id,
        )
        return tab

    async def switch_tab_account(
        self,
        proxy_key: ProxyKey,
        type_name: str,
        account_id: str,
    ) -> bool:
        """
        在同一个 page 上切换账号。只有 drained 后（active_requests==0）才允许切号。
        """
        entry = self._entries.get(proxy_key)
        if entry is None:
            return False
        tab = entry.tabs.get(type_name)
        if tab is None or tab.active_requests != 0:
            return False

        tab.account_id = account_id
        tab.accepting_new = True
        tab.state = "ready"
        tab.frozen_until = None
        tab.last_used_at = time.time()
        tab.sessions.clear()
        entry.last_used_at = time.time()
        logger.info(
            "[tab] switched account mode=%s proxy=%s type=%s account=%s",
            "proxy" if proxy_key.use_proxy else "direct",
            proxy_key.fingerprint_id,
            type_name,
            account_id,
        )
        return True

    def acquire_tab(
        self,
        proxy_key: ProxyKey,
        type_name: str,
        max_concurrent: int,
    ) -> Page | None:
        """
        为一次请求占用 tab；tab 必须存在、可接新请求且未达到并发上限。
        """
        entry = self._entries.get(proxy_key)
        if entry is None:
            return None
        tab = entry.tabs.get(type_name)
        if tab is None:
            return None
        if not tab.accepting_new or tab.active_requests >= max_concurrent:
            return None
        tab.active_requests += 1
        tab.last_used_at = time.time()
        entry.last_used_at = tab.last_used_at
        tab.state = "busy"
        return tab.page

    def release_tab(self, proxy_key: ProxyKey, type_name: str) -> None:
        """释放一次请求占用。"""
        entry = self._entries.get(proxy_key)
        if entry is None:
            return
        tab = entry.tabs.get(type_name)
        if tab is None:
            return
        if tab.active_requests > 0:
            tab.active_requests -= 1
        tab.last_used_at = time.time()
        entry.last_used_at = tab.last_used_at
        if tab.active_requests == 0:
            if tab.accepting_new:
                tab.state = "ready"
            elif tab.frozen_until is not None:
                tab.state = "frozen"
            else:
                tab.state = "draining"

    def mark_tab_draining(
        self,
        proxy_key: ProxyKey,
        type_name: str,
        *,
        frozen_until: int | None = None,
    ) -> None:
        """禁止 tab 接受新请求，并标记为 draining/frozen。"""
        entry = self._entries.get(proxy_key)
        if entry is None:
            return
        tab = entry.tabs.get(type_name)
        if tab is None:
            return
        tab.accepting_new = False
        tab.frozen_until = frozen_until
        tab.last_used_at = time.time()
        entry.last_used_at = tab.last_used_at
        if frozen_until is not None:
            tab.state = "frozen"
        else:
            tab.state = "draining"

    def register_session(
        self,
        proxy_key: ProxyKey,
        type_name: str,
        session_id: str,
    ) -> None:
        entry = self._entries.get(proxy_key)
        if entry is None:
            return
        tab = entry.tabs.get(type_name)
        if tab is None:
            return
        tab.sessions.add(session_id)
        tab.last_used_at = time.time()
        entry.last_used_at = tab.last_used_at

    def unregister_session(
        self,
        proxy_key: ProxyKey,
        type_name: str,
        session_id: str,
    ) -> None:
        entry = self._entries.get(proxy_key)
        if entry is None:
            return
        tab = entry.tabs.get(type_name)
        if tab is None:
            return
        tab.sessions.discard(session_id)

    async def close_tab(
        self,
        proxy_key: ProxyKey,
        type_name: str,
    ) -> ClosedTabInfo | None:
        """关闭某个 type 的 tab，并返回需要失效的 session 列表。"""
        entry = self._entries.get(proxy_key)
        if entry is None:
            return None
        tab = entry.tabs.pop(type_name, None)
        if tab is None:
            return None
        try:
            await tab.page.close()
        except Exception:
            pass
        entry.last_used_at = time.time()
        logger.info(
            "[tab] closed mode=%s proxy=%s type=%s",
            "proxy" if proxy_key.use_proxy else "direct",
            proxy_key.fingerprint_id,
            type_name,
        )
        return ClosedTabInfo(
            proxy_key=proxy_key,
            type_name=type_name,
            account_id=tab.account_id,
            session_ids=list(tab.sessions),
        )

    async def close_browser(self, proxy_key: ProxyKey) -> list[ClosedTabInfo]:
        return await self._close_entry_async(proxy_key)

    async def _close_entry_async(self, proxy_key: ProxyKey) -> list[ClosedTabInfo]:
        entry = self._entries.get(proxy_key)
        if entry is None:
            return []

        closed_tabs = [
            ClosedTabInfo(
                proxy_key=proxy_key,
                type_name=type_name,
                account_id=tab.account_id,
                session_ids=list(tab.sessions),
            )
            for type_name, tab in entry.tabs.items()
        ]
        for tab in list(entry.tabs.values()):
            try:
                await tab.page.close()
            except Exception:
                pass
        entry.tabs.clear()
        if entry.proxy_forwarder is not None:
            try:
                entry.proxy_forwarder.stop()
            except Exception as e:
                logger.warning("关闭本地代理转发时异常: %s", e)
        if entry.browser is not None:
            try:
                await entry.browser.close()
            except Exception as e:
                logger.warning("关闭 CDP 浏览器时异常: %s", e)
        try:
            entry.proc.terminate()
            entry.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            entry.proc.kill()
            entry.proc.wait(timeout=3)
        except Exception as e:
            logger.warning("关闭浏览器进程时异常: %s", e)
        self._cleanup_stderr_log(entry.stderr_path)
        self._available_ports.add(entry.port)
        del self._entries[proxy_key]
        logger.info(
            "[browser] closed mode=%s proxy=%s",
            "proxy" if proxy_key.use_proxy else "direct",
            proxy_key.fingerprint_id,
        )
        return closed_tabs

    async def collect_idle_browsers(
        self,
        *,
        idle_seconds: float,
        resident_browser_count: int,
    ) -> list[ClosedTabInfo]:
        """
        关闭空闲浏览器：

        - 浏览器下所有 tab 都没有活跃请求
        - 所有 tab 均已空闲超过 idle_seconds
        - 当前浏览器数 > resident_browser_count
        """
        if len(self._entries) <= resident_browser_count:
            return []

        now = time.time()
        candidates: list[tuple[float, ProxyKey]] = []
        for proxy_key, entry in self._entries.items():
            if any(tab.active_requests > 0 for tab in entry.tabs.values()):
                continue
            if entry.tabs:
                last_tab_used = max(tab.last_used_at for tab in entry.tabs.values())
            else:
                last_tab_used = entry.last_used_at
            if now - last_tab_used < idle_seconds:
                continue
            candidates.append((last_tab_used, proxy_key))

        if not candidates:
            return []

        closed: list[ClosedTabInfo] = []
        max_close = max(0, len(self._entries) - resident_browser_count)
        for _, proxy_key in sorted(candidates, key=lambda item: item[0])[:max_close]:
            closed.extend(await self._close_entry_async(proxy_key))
        return closed

    async def close_all(self) -> list[ClosedTabInfo]:
        """关闭全部浏览器和 tab。"""
        closed: list[ClosedTabInfo] = []
        for proxy_key in list(self._entries.keys()):
            closed.extend(await self._close_entry_async(proxy_key))
        return closed
