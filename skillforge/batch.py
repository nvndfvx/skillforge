"""批量执行：一次跑一批业务单据，失败隔离 + 断点续跑。

和 `golden/runner.py` 的分工（两者共用执行器，但语义完全不同）：

| | run_golden | run_batch |
|---|---|---|
| 问的问题 | 技能**有没有变差** | 今天这一批**做成了几张** |
| 输入 | 固定回归基准集 | 任意单据清单（可能几百条） |
| 产物 | CaseScore → 喂单调性闸 | 逐条结果 → 交给业务 |
| 失败处置 | 一条失败就可能否决发版 | 隔离，继续跑下一条 |

**一条失败绝不能中断整批** —— 这是批量执行和跑测试最本质的区别。
"""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from .executor.adapter import PageAdapter
from .executor.driver import execute
from .schema.models import Skill

PageFactory = Callable[[Any], "PageAdapter | Awaitable[PageAdapter]"]
PageTeardown = Callable[[PageAdapter], "None | Awaitable[None]"]

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class BatchItem:
    """一条要干的活。params 直接喂给技能当入参。"""

    item_id: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatchItem":
        return cls(
            item_id=str(data.get("item_id") or data.get("case_id") or ""),
            params=dict(data.get("params") or {}),
        )


@dataclass
class BatchResult:
    item_id: str
    ok: bool
    status: str = STATUS_OK
    detail: str = ""
    duration_ms: int = 0
    failure_layer: str = ""
    step_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "failure_layer": self.failure_layer,
            "step_id": self.step_id,
        }


@dataclass
class BatchReport:
    results: list[BatchResult] = field(default_factory=list)

    # -- 统计（skipped 不计入通过率，否则续跑会把分母做小、显得变好了）------
    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_SKIPPED)

    @property
    def attempted(self) -> int:
        return sum(1 for r in self.results if r.status != STATUS_SKIPPED)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_OK)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_FAILED)

    @property
    def rate(self) -> float:
        return round(self.passed / self.attempted, 4) if self.attempted else 0.0

    def failures(self) -> list[BatchResult]:
        return [r for r in self.results if r.status == STATUS_FAILED]

    def by_layer(self) -> dict[str, int]:
        """失败按层归类：环境/参数/跳过/其他。用来判断该修代码还是该修数据。"""
        layers: dict[str, int] = {}
        for r in self.failures():
            layers[r.failure_layer or "unknown"] = layers.get(r.failure_layer or "unknown", 0) + 1
        return layers

    def checkpoint(self) -> dict[str, str]:
        """续跑用：只记成功的，失败的下次还要重试。"""
        return {r.item_id: r.status for r in self.results if r.status == STATUS_OK}

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "attempted": self.attempted,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "rate": self.rate,
            "by_layer": self.by_layer(),
            "results": [r.to_dict() for r in self.results],
        }


async def run_batch(
    skill: Skill,
    items: Sequence[BatchItem],
    page_factory: PageFactory,
    *,
    checkpoint: dict[str, str] | None = None,
    limit: int | None = None,
    interval_ms: int = 0,
    stop_after_failures: int | None = None,
    teardown: PageTeardown | None = None,
) -> BatchReport:
    """批量执行。任何一条失败都不会中断整批（除非显式设了 stop_after_failures）。"""
    report = BatchReport()
    done = dict(checkpoint or {})
    attempted = 0

    for item in items:
        if limit is not None and attempted >= limit:
            break
        if done.get(item.item_id) == STATUS_OK:
            report.results.append(
                BatchResult(item.item_id, True, status=STATUS_SKIPPED, detail="上次已成功，续跑跳过")
            )
            continue

        attempted += 1
        started = time.perf_counter()
        page: Any = None
        try:
            page = page_factory(item)
            if inspect.isawaitable(page):
                page = await page
            run = await execute(skill, item.params, page, run_id=f"batch::{item.item_id}")
        except Exception as exc:  # noqa: BLE001 - 单条炸了不能拖垮整批
            report.results.append(
                BatchResult(
                    item.item_id,
                    False,
                    status=STATUS_FAILED,
                    detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    failure_layer="executor",
                )
            )
        else:
            failed = run.failed_step()
            report.results.append(
                BatchResult(
                    item.item_id,
                    run.ok,
                    status=STATUS_OK if run.ok else STATUS_FAILED,
                    detail="" if run.ok else (failed.detail or failed.status),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    failure_layer="" if run.ok else (failed.failure_layer or failed.status),
                    step_id=None if failed is None else failed.step_id,
                )
            )
        finally:
            if teardown is not None and page is not None:
                try:
                    closed = teardown(page)
                    if inspect.isawaitable(closed):
                        await closed
                except Exception:  # noqa: BLE001 - 关页面失败不该影响结果
                    pass

        if (
            stop_after_failures is not None
            and report.failed >= stop_after_failures
        ):
            break
        if interval_ms > 0:
            import asyncio

            await asyncio.sleep(interval_ms / 1000)

    return report


def load_items(data: Any) -> list[BatchItem]:
    """接受两种写法：裸数组，或 {"items": [...]}。"""
    raw = data.get("items") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError("cases 文件应为数组，或 {\"items\": [...]}")
    items = [BatchItem.from_dict(row) for row in raw]
    if not items:
        raise ValueError("清单是空的")
    seen: set[str] = set()
    for item in items:
        if not item.item_id:
            raise ValueError("每条都要有 item_id")
        if item.item_id in seen:
            raise ValueError(f"item_id 重复：{item.item_id}")
        seen.add(item.item_id)
    return items


__all__ = [
    "BatchItem",
    "BatchReport",
    "BatchResult",
    "load_items",
    "run_batch",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_SKIPPED",
]
