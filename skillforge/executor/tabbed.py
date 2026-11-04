"""多标签页执行：把一个浏览器上下文里的多个页面，伪装成一个 PageAdapter。

为什么需要：真实系统里"点开详情"常常是**新标签页**。技能里每一步都带
`step.tab`（0=初始页，1,2…=之后点开的），执行时必须把动作送到对应的标签页上，
否则点完第一下，后面全打在已经过时的页面上。

标签页编号规则两边必须一致：**按点开的先后顺序**。
录制时由 TraceRecorder 监听 context 的 page 事件编号；执行时这里用同样的顺序。
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncIterator

from ..pwtmp import ensure_playwright_temp
from .playwright_page import PlaywrightPage


class TabbedPage:
    """多标签页适配器：对外仍是一个 PageAdapter，内部按 tab 路由。"""

    def __init__(self, primary: PlaywrightPage, context: Any) -> None:
        self._adapters: dict[int, PlaywrightPage] = {0: primary}
        self._context = context
        self.current = 0
        try:
            context.on("page", self._register)
        except Exception:
            pass

    # -- 兼容单页接口 -------------------------------------------------------
    @property
    def page(self) -> Any:
        """当前标签页的原生 page。录制器需要它来注入脚本。"""
        return self._a().page

    @property
    def context(self) -> Any:
        return self._context

    # -- 标签页管理 ---------------------------------------------------------
    def _register(self, new_page: Any) -> None:
        if any(a.page is new_page for a in self._adapters.values()):
            return
        self._adapters[len(self._adapters)] = PlaywrightPage(new_page)

    def _a(self) -> PlaywrightPage:
        adapter = self._adapters.get(self.current)
        if adapter is None:
            raise RuntimeError(
                f"标签页 {self.current} 还不存在（现有 {sorted(self._adapters)}）："
                "说明前面应该有一个 click 会点开它"
            )
        return adapter

    async def use_tab(self, tab: int, *, timeout_ms: int = 8000) -> None:
        """切到某个标签页。还不存在就等 —— 新标签页是 click 之后才出现的。"""
        if tab not in self._adapters:
            deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
            while asyncio.get_event_loop().time() < deadline:
                if tab in self._adapters:
                    break
                await asyncio.sleep(0.1)
        self.current = tab
        self._a()  # 提前抛错，别等到动作执行一半才发现

    # -- PageAdapter 委托 ---------------------------------------------------
    async def goto(self, url: str) -> None:
        await self._a().goto(url)

    async def text(self) -> str:
        return await self._a().text()

    async def url(self) -> str:
        return await self._a().url()

    async def fill(self, locator: Any, value: str) -> None:
        await self._a().fill(locator, value)

    async def click(self, locator: Any) -> None:
        await self._a().click(locator)

    async def exists(self, locator: Any) -> bool:
        return await self._a().exists(locator)

    async def count(self, locator: Any) -> int:
        return await self._a().count(locator)

    async def fingerprints(self) -> list[Any]:
        return await self._a().fingerprints()


@contextlib.asynccontextmanager
async def launch_tabbed(
    *, channel: str = "chrome", headless: bool = True, profile_dir: str | None = None
) -> AsyncIterator[TabbedPage]:
    """起一个能跟踪多标签页的浏览器会话。"""
    ensure_playwright_temp()

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        if profile_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir), channel=channel, headless=headless
            )
            browser = None
        else:
            browser = await p.chromium.launch(channel=channel, headless=headless)
            context = await browser.new_context()
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            tabbed = TabbedPage(PlaywrightPage(page), context)
            yield tabbed
        finally:
            try:
                await context.close()
            except Exception:
                pass
            if browser is not None:
                await browser.close()


__all__ = ["TabbedPage", "launch_tabbed"]
