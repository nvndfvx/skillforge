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
) -> AsyncIterator[Any]:
    """起一个真浏览器页面。

    实际返回的是 `TabbedPage`（多标签适配器），它同时兼容单页用法，
    也暴露 `.page` / `.context` 给录制器用 —— 这样所有命令都白拿多标签能力。
    标签页按点开的先后编号：0=初始页，1,2…=之后弹出的。
    """
    from .tabbed import launch_tabbed

    async with launch_tabbed(
        channel=channel, headless=headless, profile_dir=profile_dir
    ) as tabbed:
        yield tabbed


__all__ = ["PlaywrightPage", "launch_page"]
