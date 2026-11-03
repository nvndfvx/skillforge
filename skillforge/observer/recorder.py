"""Playwright 录制器（地基② 指纹 / 地基① 断言的采集端 / 地基④ 写副作用标记）。

两个刻意的设计（都是踩坑换来的）：

1. **`_on_event` 是同步的**，只把事件塞进队列，不做任何页面 IO。
   原因：`expose_function` 的回调在页面 JS 里是同步阻塞的，回调里再回头调
   `page.evaluate` 属于重入，真实浏览器上有死锁风险。所以 settle 挪到 `flush()`，
   由录制方在每个用户动作之后显式调用。

2. **playwright 不在导入期依赖**：这里只按鸭子类型调 page，schema 与对齐器因此
   可以完全离线测试。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..schema.types import (
    ElementFingerprint,
    RawAction,
    RawTrace,
    WriteEffect,
    norm_name,
)
from .js_capture import capture_script
from .reconcile import FormField, merge_synthesized, reconcile_form_snapshots

# 用按钮文案粗判「这一步是不是写操作」——录制时就把 write_effect 标出来，
# 免得等到编译期才让人回忆「哪一步会写库」。
_DELETE_WORDS = ("删除", "移除", "delete", "remove")
_UPDATE_WORDS = ("修改", "更新", "编辑", "update", "edit")
_CREATE_WORDS = (
    "提交",
    "保存",
    "确认",
    "确定",
    "创建",
    "新增",
    "生成",
    "submit",
    "save",
    "create",
    "confirm",
    "add",
)
_SAFE_WORDS = (
    "取消",
    "关闭",
    "返回",
    "搜索",
    "查询",
    "筛选",
    "上一步",
    "下一步",
    "上一页",
    "下一页",
    "重置",
    "cancel",
    "close",
    "back",
    "search",
    "query",
    "filter",
    "reset",
    "next",
    "prev",
)


def infer_write_effect(action: str, name: str) -> WriteEffect:
    """从动作类型 + 元素文案推断写副作用等级。

    判定不出来时返回 UNKNOWN 而不是 NONE —— 这个默认方向是有意的：
    误判成写 = 多要一次幂等校验（只是麻烦）；误判成读 = 重复提交（是事故）。
    """
    if action != "click":
        return WriteEffect.NONE
    low = norm_name(name)
    if not low:
        return WriteEffect.UNKNOWN
    if any(w in low for w in _DELETE_WORDS):
        return WriteEffect.DELETE
    if any(w in low for w in _UPDATE_WORDS):
        return WriteEffect.UPDATE
    if any(w in low for w in _CREATE_WORDS):
        return WriteEffect.CREATE
    if any(w in low for w in _SAFE_WORDS):
        return WriteEffect.NONE
    return WriteEffect.UNKNOWN


def _frame_paths(page: Any) -> dict[int, str]:
    """给每个 frame 算一个稳定身份，形如 "0.1"（root 为 ""）。

    H3：同一个 URL 可以挂多个 frame，只靠 url 会让指纹撞车，
    按指纹重匹配 locator 时会匹配到另一个 frame 里的元素。
    """
    frames = list(getattr(page, "frames", []) or [])
    by_id = {id(f): f for f in frames}
    children: dict[int, list[Any]] = {}
    roots: list[Any] = []
    for f in frames:
        parent = getattr(f, "parent_frame", None)
        if parent is None or id(parent) not in by_id:
            roots.append(f)
        else:
            children.setdefault(id(parent), []).append(f)

    out: dict[int, str] = {}

    def walk(frame: Any, prefix: str) -> None:
        out[id(frame)] = prefix
        for i, child in enumerate(children.get(id(frame), [])):
            walk(child, f"{prefix}.{i}" if prefix else str(i))

    for root in roots:
        walk(root, "")
    return out


def _make_dispatch(page_key: int):
    """绑定到页面的稳定回调：按 page 查当前 recorder。

    为什么需要这层间接：`expose_function` 不允许在同一 page 上注册同名函数。
    而「同一页面录两遍演示」是常态（两次演示就是为了对齐）。
    所以注册一次，回调里查表转发——后续 recorder 覆盖表项即可，不用重新注册。
    """

    def handler(payload: Any) -> None:
        recorder = _RECORDERS.get(page_key)
        if recorder is not None:
            recorder._on_event(payload)

    return handler


_RECORDERS: dict[int, "TraceRecorder"] = {}


class TraceRecorder:
    """录制一遍演示。

    用法：
        rec = TraceRecorder(page)
        await rec.start()
        ... 用户做一个动作 ...
        await rec.flush()          # ← 每个动作之后要 flush，否则 after_text 会串味
        ...
        trace = await rec.stop()
    """

    def __init__(
        self,
        page: Any,
        *,
        trace_id: str | None = None,
        settle_timeout_ms: int = 800,
        settle_poll_ms: int = 120,
        pause_threshold_ms: int = 3000,
        version: str = __version__,
    ) -> None:
        self.page = page
        self.trace_id = trace_id or f"trace-{int(time.time())}"
        self.settle_timeout_ms = int(settle_timeout_ms)
        self.settle_poll_ms = int(settle_poll_ms)
        self.pause_threshold_ms = int(pause_threshold_ms)
        self.version = version

        self._pending: list[dict] = []
        self._actions: list[RawAction] = []
        self._form_snapshots: list[list[FormField]] = []
        self._baseline: list[FormField] = []
        self._started_at: str | None = None
        self._last_event_at = time.monotonic()
        self._running = False
        self._flushes = 0

    # -- 生命周期 -----------------------------------------------------------
    async def start(self) -> None:
        # 先登记当前 recorder，再（幂等地）注册转发回调
        _RECORDERS[id(self.page)] = self
        try:
            await self.page.expose_function(
                "__skillforgeLog", _make_dispatch(id(self.page))
            )
        except Exception:
            # 同一 page 已注册过：旧的 dispatch 会按 key 查到新的 recorder，直接复用
            pass
        script = capture_script(self.version)
        await self.page.add_init_script(script=script)
        # 已存在的 frame 也要注入（init_script 只对之后创建的生效）。
        # 先写 frame_path 再注入，否则第一批事件的指纹没有 frame 身份（H3）。
        paths = _frame_paths(self.page)
        for frame in list(getattr(self.page, "frames", []) or []):
            path = paths.get(id(frame), "")
            try:
                await frame.evaluate("window.__skillforgeFramePath = " + json.dumps(path))
                await frame.evaluate(script)
            except Exception:
                continue
        self._running = True
        self._started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._last_event_at = time.monotonic()
        # 录制开始时的表单基线：没有它，页面自带的预填值会被误判成用户填的
        self._baseline = await self._form_snapshot()

    async def flush(self, grace_ms: int = 150) -> int:
        """把缓冲事件定型：settle 之后补 after_text 与表单快照。返回定型条数。

        grace_ms 是给绑定消息留的余量：页面侧 emit 之后，回调是异步送达的，
        紧接着 flush 有可能抢在消息到达之前跑完。
        """
        if not self._pending and grace_ms:
            await asyncio.sleep(grace_ms / 1000.0)
        if not self._pending:
            return 0
        pending, self._pending = self._pending, []
        after_text = await self._settle()
        snapshot = await self._form_snapshot()
        for ev in pending:
            self._actions.append(self._to_action(ev, after_text))
            self._form_snapshots.append(snapshot)
        self._merge_tail_noise()
        self._flushes += 1
        return len(pending)

    async def stop(self) -> RawTrace:
        self._running = False
        await self.flush()
        _RECORDERS.pop(id(self.page), None)
        reconciled = reconcile_form_snapshots(
            self._actions,
            self._form_snapshots,
            version=self.version,
            baseline=self._baseline,
        )
        actions = merge_synthesized(self._actions, reconciled)
        return RawTrace(
            trace_id=self.trace_id,
            started_at=self._started_at
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            url=self._current_url(),
            actions=actions,
            form_snapshots=[[f.to_dict() for f in snap] for snap in self._form_snapshots],
            meta={
                "captured_version": self.version,
                "settle_timeout_ms": self.settle_timeout_ms,
                "flush_count": self._flushes,
                "snapshot_reconciled": reconciled.total,
                "reconcile_warnings": reconciled.warnings,
                "known_limitations": [
                    "after_text 在 flush 时取；同一批里多个事件会共用同一个快照，"
                    "所以录制时每个动作后都要 flush。",
                    "t_ms 来自页面 performance.now()，跨 frame 不可比；排序以 seq 为准。",
                    "form_snapshots 与 actions 的下标在补录后不再一一对应。",
                    "add_init_script 无法预知之后新建 iframe 的 frame_path。",
                ],
            },
        )

    async def save(self, path: Path, trace: RawTrace | None = None) -> Path:
        trace = trace or await self.stop()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(trace.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    # -- 事件（必须同步：回调里不能重入页面） --------------------------------
    def _on_event(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        now = time.monotonic()
        payload["pause_before_ms"] = int((now - self._last_event_at) * 1000)
        self._last_event_at = now
        self._pending.append(payload)

    # -- 内部 ---------------------------------------------------------------
    def _to_action(self, payload: dict, after_text: str) -> RawAction:
        fp_raw = payload.get("fingerprint") or {}
        fingerprint = (
            ElementFingerprint(
                role=fp_raw.get("role", ""),
                accessible_name=fp_raw.get("accessible_name", ""),
                text_norm=fp_raw.get("text_norm", ""),
                attrs_subset=dict(fp_raw.get("attrs_subset") or {}),
                neighbor_text=fp_raw.get("neighbor_text", ""),
                bbox_rel=tuple(fp_raw.get("bbox_rel") or (0.0, 0.0, 0.0, 0.0)),
                frame_url=fp_raw.get("frame_url", ""),
                frame_path=fp_raw.get("frame_path", "") or str(payload.get("frame_path") or ""),
                captured_version=fp_raw.get("captured_version", self.version),
            )
            if fp_raw
            else None
        )
        return RawAction(
            seq=len(self._actions),
            action=str(payload.get("event", "unknown")),
            role=(fingerprint.role if fingerprint else ""),
            name=(fingerprint.accessible_name if fingerprint else ""),
            value=payload.get("value"),
            selector="",  # 精确 locator 留给 compiler 用指纹反查，录制期不猜
            url=self._current_url(),
            frame_url=(fingerprint.frame_url if fingerprint else ""),
            fingerprint=fingerprint,
            before_text=str(payload.get("before_text") or ""),
            after_text=after_text,
            pause_before_ms=int(payload.get("pause_before_ms") or 0),
            t_ms=int(payload.get("t_ms") or 0),
        )

    def _merge_tail_noise(self) -> None:
        """合并 500ms 内对同一元素的连续点击（双击/误触）。"""
        if len(self._actions) < 2:
            return
        prev, cur = self._actions[-2], self._actions[-1]
        if (
            prev.action == "click"
            and cur.action == "click"
            and prev.name
            and norm_name(prev.name) == norm_name(cur.name)
            and (cur.t_ms - prev.t_ms) < 500
        ):
            self._actions.pop()
            if self._form_snapshots:
                self._form_snapshots.pop()
            cur.seq = prev.seq

    # -- settle / 快照 ------------------------------------------------------
    async def _settle(self) -> str:
        """等网络与 DOM 文本双静默，再取「操作之后」的页面摘要。

        这是断言生成正确性的前提：事件一发生就快照，录到的是操作**前**的状态。
        """
        try:
            await self.page.wait_for_load_state("networkidle", timeout=self.settle_timeout_ms)
        except Exception:
            pass

        deadline = time.monotonic() + self.settle_timeout_ms / 1000.0
        prev: str | None = None
        stable = 0
        while time.monotonic() < deadline:
            cur = await self._safe_digest()
            if cur is not None and cur == prev:
                stable += 1
                if stable >= 2:
                    return cur
            else:
                stable = 0
            if cur is not None:
                prev = cur
            await asyncio.sleep(self.settle_poll_ms / 1000.0)
        return prev or ""

    async def _safe_digest(self) -> str | None:
        try:
            return await self.page.evaluate(
                "window.__skillforgeTextDigest ? window.__skillforgeTextDigest() : null"
            )
        except Exception:
            return None

    async def _form_snapshot(self) -> list[FormField]:
        """扫一遍全表单（含所有 frame），用于补录不触发 change 的字段。"""
        out: list[FormField] = []
        for frame in list(getattr(self.page, "frames", []) or []):
            try:
                raw = await frame.evaluate(
                    "window.__skillforgeFormSnapshot ? window.__skillforgeFormSnapshot() : []"
                )
            except Exception:
                continue
            for item in raw or []:
                if isinstance(item, dict):
                    out.append(
                        FormField(
                            role=str(item.get("role") or ""),
                            accessible_name=str(item.get("accessible_name") or ""),
                            name=str(item.get("name") or ""),
                            value=str(item.get("value") or ""),
                            frame_url=str(item.get("frame_url") or ""),
                            frame_path=str(item.get("frame_path") or ""),
                        )
                    )
        return out

    def _current_url(self) -> str:
        try:
            return str(getattr(self.page, "url", "") or "")
        except Exception:
            return ""


__all__ = ["TraceRecorder", "infer_write_effect"]
