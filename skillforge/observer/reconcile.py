"""H4：程序赋值字段的补录（表单快照差集）。

问题：日期选择器、自动完成、部分组件库是**用 JS 直接写 value**，不触发 `change`。
这类字段整个漏录 → 技能少一步 → 一跑就错。

做法：每个动作 settle 之后扫一遍全表单，把「值变了但没录到对应动作」的字段补成 fill。
纯 stdlib，可离线测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..schema.types import ElementFingerprint, RawAction, norm_name, norm_text


@dataclass
class FormField:
    """表单快照里的一个字段。"""

    role: str = ""
    accessible_name: str = ""
    name: str = ""
    value: str = ""
    frame_url: str = ""
    frame_path: str = ""

    def label(self) -> str:
        return self.accessible_name or self.name

    def key(self) -> tuple[str, str, str]:
        return (self.frame_path, self.role, norm_name(self.label()))

    def to_fingerprint(self, version: str = "") -> ElementFingerprint:
        return ElementFingerprint(
            role=self.role,
            accessible_name=self.label(),
            text_norm=norm_text(self.value, 80),
            frame_url=self.frame_url,
            frame_path=self.frame_path,
            captured_version=version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "accessible_name": self.accessible_name,
            "name": self.name,
            "value": self.value,
            "frame_url": self.frame_url,
            "frame_path": self.frame_path,
        }


@dataclass
class ReconcileResult:
    """synthesized 的 key = 插在第 i 个已录动作**之后**（-1 表示最前面）。"""

    synthesized: dict[int, list[RawAction]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.synthesized.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "drifted_field_count": self.total,
            "positions": {str(k): len(v) for k, v in self.synthesized.items()},
            "warnings": list(self.warnings),
        }


def _coerce(item: Any) -> FormField:
    if isinstance(item, FormField):
        return item
    if isinstance(item, dict):
        return FormField(
            role=str(item.get("role") or ""),
            accessible_name=str(item.get("accessible_name") or ""),
            name=str(item.get("name") or ""),
            value=str(item.get("value") or ""),
            frame_url=str(item.get("frame_url") or ""),
            frame_path=str(item.get("frame_path") or ""),
        )
    raise TypeError(f"无法识别的表单字段：{type(item)!r}")


def _loosely_equal(a: str, b: str) -> bool:
    """字段标签的宽松比较：录到的标签可能是快照标签的片段。"""
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def reconcile_form_snapshots(
    recorded: Sequence[RawAction],
    snapshots: Sequence[Sequence[Any]],
    *,
    version: str = "",
    baseline: Sequence[Any] | None = None,
) -> ReconcileResult:
    """把「值变了但没录到动作」的字段补成 fill 动作。

    recorded[i] 与 snapshots[i] 一一对应（snapshot 取自该动作 settle 之后）。
    baseline 是**开始录制时的表单状态**：不给的话，页面自带的预填值会被误判成用户填的。
    """
    result = ReconcileResult()
    prev: dict[tuple[str, str, str], FormField] = {}
    if baseline:
        prev = {f.key(): f for f in (_coerce(x) for x in baseline)}

    for i, snap in enumerate(snapshots):
        fields = [_coerce(x) for x in (snap or [])]
        cur = {f.key(): f for f in fields}

        rec = recorded[i] if i < len(recorded) else None
        rec_label = ""
        if rec is not None and rec.action in ("fill", "select"):
            rec_label = rec.name or ""

        for key, f in cur.items():
            old = prev.get(key)
            changed = old is None or old.value != f.value
            if not changed or not f.value:
                continue
            # 录制已经覆盖了这个字段
            if rec_label and _loosely_equal(rec_label, f.label()):
                continue
            action = RawAction(
                seq=0,
                action="select" if f.role in ("combobox", "listbox") else "fill",
                role=f.role,
                name=f.label(),
                value=f.value,
                frame_url=f.frame_url,
                fingerprint=f.to_fingerprint(version),
                source="snapshot_diff",
            )
            result.synthesized.setdefault(i, []).append(action)
            result.warnings.append(
                f"字段「{f.label() or f.name or '未命名'}」的值变了但没有 change 事件，"
                "已从表单快照补录（程序赋值/组件库写值）。"
            )

        prev = cur

    return result


def merge_synthesized(
    recorded: Iterable[RawAction], result: ReconcileResult
) -> list[RawAction]:
    """把补录动作按位置插进已录序列，并重排 seq。"""
    merged: list[RawAction] = []
    for i, act in enumerate(recorded):
        merged.append(act)
        merged.extend(result.synthesized.get(i, []))
    for idx, act in enumerate(merged):
        act.seq = idx
    return merged


__all__ = [
    "FormField",
    "ReconcileResult",
    "merge_synthesized",
    "reconcile_form_snapshots",
]
