"""测试替身：不需要浏览器的 PageAdapter 实现。

放在包里（不是 tests/）是为了让 `examples/` 和 `tests/` 共用同一份，
避免两处 FakePage 行为不一致。
"""
from __future__ import annotations

from typing import Sequence

from .schema.types import ElementFingerprint, Locator

DEFAULT_RECEIPT = "提交成功 单号 R-20240115 已创建"


class FakePage:
    """脚本化的假页面：只实现 PageAdapter 协议，行为完全由构造参数决定。"""

    def __init__(
        self,
        *,
        url: str = "http://erp.local/expense/new",
        text: str = "报销单填写中",
        known_names: Sequence[str] = ("客户名称", "提交"),
        known_css: Sequence[str] = (),
        fingerprints: Sequence[ElementFingerprint] = (),
        on_submit_text: str = DEFAULT_RECEIPT,
        on_submit_url: str = "http://erp.local/expense/success",
        required_fields: Sequence[str] = (),
    ) -> None:
        self._url = url
        self._text = text
        self._names = set(known_names)
        self._css = set(known_css)
        self._fps = list(fingerprints)
        self._on_submit_text = on_submit_text
        self._on_submit_url = on_submit_url
        # 模拟"必填校验"：缺字段时提交会失败。
        # 用来验证「删掉一步」这种补丁会被 Golden Set 抓出来。
        self._required = tuple(required_fields)
        self.fields: dict[str, str] = {}
        self.calls: list[tuple] = []

    async def goto(self, url: str) -> None:
        self._url = url
        self.calls.append(("goto", url))

    async def text(self) -> str:
        return self._text

    async def url(self) -> str:
        return self._url

    async def fill(self, locator: Locator, value: str) -> None:
        key = locator.name or locator.css or "?"
        self.fields[key] = value
        self.calls.append(("fill", key, value))

    async def click(self, locator: Locator) -> None:
        name = locator.name or locator.css or ""
        self.calls.append(("click", name))
        if "提交" in name:
            self._url = self._on_submit_url
            missing = [f for f in self._required if not self.fields.get(f)]
            if missing:
                self._text = "提交失败：缺少必填 " + "、".join(missing)
            else:
                self._text = self._on_submit_text

    async def exists(self, locator: Locator) -> bool:
        if locator is None:
            return False
        if locator.name and locator.name in self._names:
            return True
        if locator.css and locator.css in self._css:
            return True
        return False

    async def count(self, locator: Locator) -> int:
        return 1 if await self.exists(locator) else 0

    async def fingerprints(self) -> list[ElementFingerprint]:
        return self._fps


__all__ = ["DEFAULT_RECEIPT", "FakePage"]
