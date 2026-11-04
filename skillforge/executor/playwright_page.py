"""真实浏览器实现：把 Playwright Page 接成 executor 的 PageAdapter。

只有这一层依赖 playwright；executor / healer / golden 全部与它解耦，
所以它们的逻辑可以离线测（FakePage），这一层才需要真浏览器。

沙箱注意：在 DSH 沙箱里无法启动浏览器（Windows asyncio 子进程用命名管道，
会被拒绝，报 WinError 5）。真浏览器测试请在普通终端里跑。
"""
from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator, Sequence

from .. import __version__
from ..schema.types import ElementFingerprint, Locator
from ..observer.js_capture import capture_script


class PlaywrightPage:
    """把一个 Playwright Page 包成 PageAdapter。"""

    def __init__(self, page: Any, *, version: str = __version__) -> None:
        self.page = page
        self.version = version

    # -- 导航 / 读取 --------------------------------------------------------
    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def text(self) -> str:
        try:
            return await self.page.inner_text("body")
        except Exception:
            return ""

    async def url(self) -> str:
        return str(self.page.url or "")

    # -- 交互 --------------------------------------------------------------
    async def fill(self, locator: Locator, value: str) -> None:
        target = self._locator(locator)
        if target is None:
            raise ValueError(f"空定位器，无法填充：{value!r}")
        await target.fill(value)

    async def click(self, locator: Locator) -> None:
        target = self._locator(locator)
        if target is None:
            raise ValueError("空定位器，无法点击")
        await target.click()

    async def exists(self, locator: Locator) -> bool:
        return await self.count(locator) > 0

    async def count(self, locator: Locator) -> int:
        target = self._locator(locator)
        if target is None:
            return 0
        try:
            return await target.count()
        except Exception:
            return 0

    # -- 指纹 --------------------------------------------------------------
    async def fingerprints(self) -> Sequence[ElementFingerprint]:
        out: list[ElementFingerprint] = []
        for frame in list(getattr(self.page, "frames", []) or []):
            try:
                if not await frame.evaluate("!!window.__skillforgeCollectCandidates"):
                    await frame.evaluate(capture_script(self.version))
                raw = await frame.evaluate("window.__skillforgeCollectCandidates(300)")
            except Exception:
                continue
            for item in raw or []:
                if not isinstance(item, dict):
                    continue
                out.append(
                    ElementFingerprint(
                        role=str(item.get("role") or ""),
                        accessible_name=str(item.get("accessible_name") or ""),
                        text_norm=str(item.get("text_norm") or ""),
                        attrs_subset=dict(item.get("attrs_subset") or {}),
                        neighbor_text=str(item.get("neighbor_text") or ""),
                        bbox_rel=tuple(item.get("bbox_rel") or (0.0, 0.0, 0.0, 0.0)),
                        frame_url=str(item.get("frame_url") or ""),
                        frame_path=str(item.get("frame_path") or ""),
                        captured_version=str(item.get("captured_version") or self.version),
                    )
                )
        return out

    # -- 内部 --------------------------------------------------------------
    def _locator(self, locator: Locator | None) -> Any:
        if locator is None:
            return None
        page = self.page
        if locator.role and locator.name:
            try:
                return page.get_by_role(locator.role, name=locator.name, exact=False)
            except Exception:
                return page.get_by_text(locator.name, exact=False)
        if locator.role:
            return page.get_by_role(locator.role)
        if locator.css:
            return page.locator(locator.css)
        if locator.name:
            return page.get_by_text(locator.name, exact=False)
        return None


@contextlib.asynccontextmanager
async def launch_page(
    *,
    channel: str = "chrome",
    headless: bool = True,
    profile_dir: str | None = None,
) -> AsyncIterator[PlaywrightPage]:
    """起一个真浏览器页面。channel 用系统已装的 Chrome/Edge，免下载 ~130MB。

    `profile_dir` 给需要登录的系统用：传一个目录，浏览器就使用持久化配置，
    登录一次之后 Cookie 留在里面，后续每次跑都还是你那个身份。
    不传则用全新临时配置（默认，干净但每次都是"未登录"）。
    """
    from ..pwtmp import ensure_playwright_temp

    # 必须在 playwright 启动前做：它会在 TMP 下建 playwright-artifacts-*
    ensure_playwright_temp()

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        if profile_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel=channel,
                headless=headless,
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                yield PlaywrightPage(page)
            finally:
                await context.close()
            return

        browser = await p.chromium.launch(channel=channel, headless=headless)
        try:
            page = await browser.new_page()
            yield PlaywrightPage(page)
        finally:
            await browser.close()


__all__ = ["PlaywrightPage", "launch_page"]
