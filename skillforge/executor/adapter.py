"""页面适配器：executor 只依赖这个协议，不依赖 playwright。

好处：整个执行/幂等/断言逻辑可以用 FakePage 离线测试，
playwright 只在真正跑浏览器时作为实现注入。
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from ..schema.types import ElementFingerprint, Locator


@runtime_checkable
class PageAdapter(Protocol):
    """执行器需要的最小页面能力。"""

    async def goto(self, url: str) -> None: ...

    async def text(self) -> str: ...

    async def url(self) -> str: ...

    async def fill(self, locator: Locator, value: str) -> None: ...

    async def click(self, locator: Locator) -> None: ...

    async def exists(self, locator: Locator) -> bool: ...

    async def count(self, locator: Locator) -> int: ...

    async def fingerprints(self) -> Sequence[ElementFingerprint]: ...


__all__ = ["PageAdapter"]
