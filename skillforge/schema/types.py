"""基础类型：枚举 / 定位器 / 元素指纹 / 动作元组 / 原始轨迹。

纯 stdlib，无第三方依赖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_WS = re.compile(r"\s+")
_LABEL_TAIL = re.compile(r"[:：*＊\s]+$")


def norm_text(value: Any, limit: int = 200) -> str:
    """归一化文本：压缩空白 → 去尾部冒号/必填星号 → 截断。"""
    if value is None:
        return ""
    text = _WS.sub(" ", str(value)).strip()
    text = _LABEL_TAIL.sub("", text).strip()
    return text[:limit]


def norm_name(value: Any) -> str:
    """归一化到「动作元组」用的字段名（大小写不敏感）。"""
    return norm_text(value, 80).lower()


class ActionKind(str, Enum):
    GOTO = "goto"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    SUBMIT = "submit"
    WAIT = "wait"
    READ_ENTITY = "read_entity"
    RUN = "run"  # CLI 域：跑一条命令


class WriteEffect(str, Enum):
    """地基④：写副作用标记。非 none 的步骤必须带幂等键 + precheck。"""

    NONE = "none"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    # 无法判定 → 保守按写操作对待。默认方向决定安全性：
    # 误判成写 = 多要一次幂等校验（可接受）；误判成读 = 重复提交（事故）。
    UNKNOWN = "unknown"


class AssertionKind(str, Enum):
    """地基①：断言分级。business 进 Golden Set 当判据；technical 只当环境探针。"""

    BUSINESS = "business"
    TECHNICAL = "technical"


class AnchorKind(str, Enum):
    """锚点⑤：断言锚在业务实体上，而不是 UI 文案上。"""

    BUSINESS_ENTITY = "business_entity"
    UI_TEXT = "ui_text"


@dataclass(frozen=True)
class Locator:
    """定位器三选一：可访问名 / CSS / 纯文本。"""

    role: str | None = None
    name: str | None = None
    css: str | None = None

    def key(self) -> str:
        parts: list[str] = []
        if self.role:
            parts.append(f"role={self.role}")
        if self.name:
            parts.append(f"name={norm_name(self.name)}")
        if self.css:
            parts.append(f"css={self.css}")
        return "|".join(parts) or "<empty>"

    def is_empty(self) -> bool:
        return not (self.role or self.name or self.css)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "name": self.name, "css": self.css}


@dataclass
class ElementFingerprint:
    """地基②：元素指纹。必须在录制那一刻存下来，事后补不回来。"""

    role: str = ""
    accessible_name: str = ""
    text_norm: str = ""
    attrs_subset: dict[str, str] = field(default_factory=dict)
    neighbor_text: str = ""
    bbox_rel: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    frame_url: str = ""
    # H3：同一个 URL 可以挂多个 frame，只记 url 会让指纹撞车。
    # 形态如 "0.1.2"（root 为 ""），由宿主在注入时写入页面。
    frame_path: str = ""
    captured_version: str = ""

    # 指纹模糊匹配合并权重（见 v3 文档第 3 节）
    W_ROLE = 0.30
    W_NAME = 0.30
    W_NEIGHBOR = 0.20
    W_TEXT = 0.10
    W_BBOX = 0.10

    def similarity(self, other: "ElementFingerprint") -> float:
        """加权相似度，返回 [0, 1]。用于 locator 失效后的候选元素重匹配。"""
        if other is None:
            return 0.0
        score = 0.0
        if self.role and self.role == other.role:
            score += self.W_ROLE
        if norm_name(self.accessible_name) == norm_name(other.accessible_name) and norm_name(
            self.accessible_name
        ):
            score += self.W_NAME
        if norm_name(self.neighbor_text) and norm_name(self.neighbor_text) == norm_name(
            other.neighbor_text
        ):
            score += self.W_NEIGHBOR
        if norm_text(self.text_norm) and norm_text(self.text_norm) == norm_text(other.text_norm):
            score += self.W_TEXT
        score += self.W_BBOX * self._bbox_close(self.bbox_rel, other.bbox_rel)
        # frame 不同 → 几乎不可能是同一个元素，强惩罚而不是加分
        if self.frame_path and other.frame_path and self.frame_path != other.frame_path:
            score *= 0.3
        return round(score, 4)

    @staticmethod
    def _bbox_close(
        a: tuple[float, float, float, float], b: tuple[float, float, float, float]
    ) -> float:
        """相对位置接近度 [0,1]。1 = 完全重合。"""
        if not a or not b or len(a) != 4 or len(b) != 4:
            return 0.0
        dist = sum(abs(x - y) for x, y in zip(a, b))
        return max(0.0, 1.0 - dist)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "accessible_name": self.accessible_name,
            "text_norm": self.text_norm,
            "attrs_subset": dict(self.attrs_subset),
            "neighbor_text": self.neighbor_text,
            "bbox_rel": list(self.bbox_rel),
            "frame_url": self.frame_url,
            "frame_path": self.frame_path,
            "captured_version": self.captured_version,
        }


@dataclass(frozen=True)
class ActionTuple:
    """序列对齐用的最小动作描述（地基③）。故意不含 value/selector，避免污染相似度。"""

    action: str
    role: str
    name_norm: str
    has_value: bool
    tab: int = 0  # 对齐时也要考虑"这一步在哪个标签页"


@dataclass
class RawAction:
    """录制得到的一条原始动作。"""

    seq: int
    action: str
    role: str = ""
    name: str = ""
    value: Any = None
    selector: str = ""
    url: str = ""
    tab: int = 0  # 第几个标签页：0=初始页，1,2…=之后点开的
    frame_url: str = ""
    fingerprint: ElementFingerprint | None = None
    before_text: str = ""          # 文本摘要，不是全 DOM（快照体积控制）
    after_text: str = ""           # settle 之后才填（快照时机）
    pause_before_ms: int = 0       # 停顿时长：免费的「判断成本」信号
    t_ms: int = 0
    # event = 浏览器事件录到的；snapshot_diff = 表单快照差集补录的（H4）
    source: str = "event"

    def to_tuple(self) -> ActionTuple:
        return ActionTuple(
            action=self.action,
            role=self.role or "",
            name_norm=norm_name(self.name),
            has_value=self.value is not None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "action": self.action,
            "role": self.role,
            "name": self.name,
            "value": self.value,
            "selector": self.selector,
            "url": self.url,
            "tab": self.tab,
            "frame_url": self.frame_url,
            "fingerprint": self.fingerprint.to_dict() if self.fingerprint else None,
            "before_text": self.before_text,
            "after_text": self.after_text,
            "pause_before_ms": self.pause_before_ms,
            "t_ms": self.t_ms,
            "source": self.source,
        }


@dataclass
class RawTrace:
    """一遍演示的完整轨迹。"""

    trace_id: str
    started_at: str
    url: str
    actions: list[RawAction] = field(default_factory=list)
    # 每个动作 settle 之后的全表单快照（H4 补录用），与 actions 一一对应
    form_snapshots: list[list[dict[str, Any]]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "url": self.url,
            "actions": [a.to_dict() for a in self.actions],
            "form_snapshots": self.form_snapshots,
            "meta": dict(self.meta),
        }