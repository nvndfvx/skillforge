"""数据模型：Assertion / Step / Skill / GoldenCase / 单调性闸。

设计约束（来自 v3 定稿）：
  - 断言分级（business / technical）            —— 地基①
  - 断言锚业务实体而不是 UI 文案                —— 锚点⑤
  - 写操作必须带幂等键 + precheck               —— 地基④
  - Golden Set 只增不删，新版本不得退化         —— 单调性闸
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .types import (
    ActionKind,
    AnchorKind,
    AssertionKind,
    ElementFingerprint,
    Locator,
    WriteEffect,
    norm_text,
)

DEFAULT_TIMEOUT_MS = 5000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 断言
# --------------------------------------------------------------------------- #
@dataclass
class Assertion:
    assertion_id: str
    kind: AssertionKind
    anchor: AnchorKind
    operator: str  # entity_exists / contains / equals / visible / count_eq / url_match / attr_eq
    target: Locator | None = None
    expected: Any = None
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    confirmed_by: str = "llm_proposed"   # human | llm_proposed
    source: str = "observed"             # observed | healed_case | manual_edge
    last_verified: str | None = None
    risk_note: str | None = None         # 降级为 ui_text 时必须登记原因

    # -- 校验 ---------------------------------------------------------------
    def errors(self) -> list[str]:
        """硬错误：进 Golden Set 或上线前必须清零。"""
        out: list[str] = []
        if self.kind is AssertionKind.BUSINESS and self.anchor is not AnchorKind.BUSINESS_ENTITY:
            out.append(
                f"[{self.assertion_id}] business 断言锚在 ui_text 上；"
                "文案一变就判据漂移，文案出现也不代表业务落库。必须锚业务实体。"
            )
        if self.anchor is AnchorKind.BUSINESS_ENTITY and not (self.expected or self.risk_note):
            out.append(f"[{self.assertion_id}] 锚业务实体但没有 expected（单号/正则）。")
        if self.operator == "entity_exists" and not self.expected:
            out.append(f"[{self.assertion_id}] entity_exists 缺 expected。")
        if self.timeout_ms <= 0:
            out.append(f"[{self.assertion_id}] timeout_ms 必须为正。")
        return out

    def warnings(self) -> list[str]:
        """非阻塞提醒。"""
        out: list[str] = []
        if self.confirmed_by != "human":
            out.append(
                f"[{self.assertion_id}] 未经人确认（llm_proposed）：不得当真值，"
                "不得作为 Golden Set 判据。"
            )
        if self.risk_note:
            out.append(f"[{self.assertion_id}] 已登记风险降级：{self.risk_note}")
        return out

    @property
    def is_trusted(self) -> bool:
        """只有人确认过的断言可以当真值。"""
        return self.confirmed_by == "human" and not self.errors()

    def to_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "kind": self.kind.value,
            "anchor": self.anchor.value,
            "operator": self.operator,
            "target": self.target.to_dict() if self.target else None,
            "expected": self.expected,
            "timeout_ms": self.timeout_ms,
            "confirmed_by": self.confirmed_by,
            "source": self.source,
            "last_verified": self.last_verified,
            "risk_note": self.risk_note,
        }


# --------------------------------------------------------------------------- #
# 步骤
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    step_id: int
    action: ActionKind
    target: Locator
    value: Any = None
    write_effect: WriteEffect = WriteEffect.NONE
    idempotency_key: str | None = None
    precheck: Assertion | None = None
    assertion: Assertion | None = None
    # 技术断言（环境探针）：失败只说明环境变了，不判技能死。
    # 写成独立字段而不是塞进 meta，是为了让 Healer 的分流逻辑能直接读。
    technical_assertion: Assertion | None = None
    fingerprint: ElementFingerprint | None = None
    fragile: bool = False  # 两次演示 target 不同 → 编译期就知道会漂
    note: str = ""

    @property
    def is_write(self) -> bool:
        return self.write_effect is not WriteEffect.NONE

    def errors(self) -> list[str]:
        """地基④ 的硬约束在这里变成可执行检查。"""
        out: list[str] = []
        if self.is_write:
            if not self.idempotency_key:
                out.append(
                    f"[step {self.step_id}] 写操作（{self.write_effect.value}）缺 idempotency_key："
                    "超时重试会造成重复提交。"
                )
            if self.precheck is None:
                out.append(
                    f"[step {self.step_id}] 写操作缺 precheck："
                    "重试前必须先查状态（单是否已存在），否则等于赌博。"
                )
        if self.target is None or self.target.is_empty():
            # goto 不需要元素定位器，它需要的是 URL（放在 value 里）
            if self.action is not ActionKind.GOTO:
                out.append(f"[step {self.step_id}] 缺 target 定位器。")
        if self.action is ActionKind.GOTO and not self.value:
            out.append(f"[step {self.step_id}] goto 缺目标 URL（应放在 value 里）。")
        if self.action in (ActionKind.FILL, ActionKind.SELECT) and self.value is None:
            out.append(f"[step {self.step_id}] {self.action.value} 缺 value。")
        return out

    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.fragile:
            out.append(
                f"[step {self.step_id}] 编译期已标记 fragile（两次演示定位器不同）："
                "必须依赖指纹兜底，且优先做修复算子训练。"
            )
        if self.write_effect is WriteEffect.UNKNOWN:
            out.append(
                f"[step {self.step_id}] 写副作用未判定（unknown）：保守起见按写操作处理，"
                "必须带幂等键 + precheck。"
            )
        if self.assertion is None and self.action is not ActionKind.GOTO:
            out.append(f"[step {self.step_id}] 无断言：该步成功了系统也不知道。")
        elif self.assertion is not None and self.assertion.confirmed_by != "human":
            out.append(f"[step {self.step_id}] 断言未人确认。")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action": self.action.value,
            "target": self.target.to_dict(),
            "value": self.value,
            "write_effect": self.write_effect.value,
            "idempotency_key": self.idempotency_key,
            "precheck": self.precheck.to_dict() if self.precheck else None,
            "assertion": self.assertion.to_dict() if self.assertion else None,
            "technical_assertion": (
                self.technical_assertion.to_dict() if self.technical_assertion else None
            ),
            "fingerprint": self.fingerprint.to_dict() if self.fingerprint else None,
            "fragile": self.fragile,
            "note": self.note,
        }


# --------------------------------------------------------------------------- #
# 技能
# --------------------------------------------------------------------------- #
@dataclass
class Parameter:
    name: str
    type: str  # string / int / enum / date / path
    required: bool = True
    source: str = "aligned_diff"  # aligned_diff > domain_schema > manual（可信度递减）
    example: Any = None
    constraints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "source": self.source,
            "example": self.example,
            "constraints": dict(self.constraints),
        }


@dataclass
class Precondition:
    kind: str  # logged_in / on_page / data_file_present / feature_flag
    locator_or_path: str
    check: Assertion | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "locator_or_path": self.locator_or_path,
            "check": self.check.to_dict() if self.check else None,
        }


@dataclass
class Skill:
    skill_id: str
    name: str
    description: str = ""
    version: str = "1.0.0"
    parameters: list[Parameter] = field(default_factory=list)
    preconditions: list[Precondition] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    # 生命周期：draft（刚编译出来，允许断言未确认）
    #          → reviewed（人审过步骤与断言）
    #          → published（已过单调性闸）
    # v3 的状态机有 HUMAN_REVIEW，但没有任何字段承载它 —— 这是写校验代码时才暴露的洞。
    state: str = "draft"

    # -- 校验 ---------------------------------------------------------------
    def errors(self) -> list[str]:
        out: list[str] = []
        if not self.steps:
            out.append("技能没有任何步骤。")
        seen: set[int] = set()
        for s in self.steps:
            if s.step_id in seen:
                out.append(f"step_id 重复：{s.step_id}")
            seen.add(s.step_id)
            out.extend(s.errors())
        for p in self.parameters:
            if not p.name:
                out.append("存在无名参数。")
        names = [p.name for p in self.parameters]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            out.append(f"参数名重复：{sorted(dup)}")
        return out

    def renumber_steps(self) -> None:
        """按当前顺序重排 step_id，并让依赖 step_id 的字段跟上。

        人审 UI 允许删步骤 / 上下移动，这两件事都会让 step_id 出现空洞或重号。
        断言是**嵌在步骤里**的，所以不受影响；真正要重建的只有幂等键和 precheck 的 id。
        """
        for index, step in enumerate(self.steps, start=1):
            step.step_id = index
            key = step.idempotency_key
            if step.is_write and key and key.startswith("{{flow_run_id}}::"):
                step.idempotency_key = f"{{{{flow_run_id}}}}::{index}"
            if step.precheck is not None:
                step.precheck.assertion_id = f"precheck-{index}"

    def review_checklist(self) -> dict[str, bool]:
        """人审三道勾（v3.2）。只报状态，不做判断。"""
        trusted = [
            s.assertion
            for s in self.steps
            if s.assertion
            and s.assertion.kind is AssertionKind.BUSINESS
            and s.assertion.is_trusted
        ]
        prechecks_ok = all(s.precheck is not None for s in self.steps if s.is_write)
        return {
            "① 步骤顺序已人审": bool(self.meta.get("steps_reviewed")),
            "② 断言已人审": bool(trusted),
            "③ 写操作 precheck 已补": prechecks_ok,
        }

    def publish_blockers(self) -> list[str]:
        """发布闸：draft 允许带病存在，reviewed 之后才谈得上发布。

        分成两层是刻意的——编译产物天然是 draft（断言都是 llm_proposed），
        若用同一套严格校验，草稿根本存不下来。
        """
        out = self.errors()
        if self.state != "reviewed":
            out.append(f"技能状态为「{self.state}」：未经人审，不得发布。")
        trusted = [
            s.assertion
            for s in self.steps
            if s.assertion
            and s.assertion.kind is AssertionKind.BUSINESS
            and s.assertion.is_trusted
        ]
        if not trusted:
            out.append(
                "没有任何人确认过的 business 断言：Golden Set 无真值可用，"
                "整个闭环的失败信号不可信。"
            )
        if not self.meta.get("steps_reviewed"):
            out.append(
                "人审未完成：① 步骤顺序还没人工确认（见 6.2 天花板二：一致 ≠ 正确）。"
            )
        return out

    def warnings(self) -> list[str]:
        out: list[str] = []
        for s in self.steps:
            out.extend(s.warnings())
        if not self.preconditions:
            out.append("没有建模前置条件：Healer 无法区分「环境没就位」和「技能坏了」。")
        for p in self.parameters:
            if p.source == "manual":
                out.append(f"参数 {p.name} 来源为 manual（可信度最低），建议核对。")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "parameters": [p.to_dict() for p in self.parameters],
            "preconditions": [p.to_dict() for p in self.preconditions],
            "steps": [s.to_dict() for s in self.steps],
            "meta": dict(self.meta),
            "state": self.state,
        }


# --------------------------------------------------------------------------- #
# Golden Set 与单调性闸
# --------------------------------------------------------------------------- #
@dataclass
class GoldenCase:
    case_id: str
    params: dict[str, Any] = field(default_factory=dict)
    extra_assertions: list[Assertion] = field(default_factory=list)
    origin: str = "demo"  # demo | healed_failure | manual_edge
    created_from_version: str = "1.0.0"
    last_pass_version: str | None = None

    def errors(self) -> list[str]:
        out: list[str] = []
        for a in self.extra_assertions:
            out.extend(a.errors())
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "params": dict(self.params),
            "extra_assertions": [a.to_dict() for a in self.extra_assertions],
            "origin": self.origin,
            "created_from_version": self.created_from_version,
            "last_pass_version": self.last_pass_version,
        }


@dataclass
class CaseScore:
    case_id: str
    passed: bool
    detail: str = ""


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    old_rate: float = 0.0
    new_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "old_rate": round(self.old_rate, 4),
            "new_rate": round(self.new_rate, 4),
        }


def monotonic_gate(old: list[CaseScore], new: list[CaseScore]) -> GateResult:
    """单调性闸：新版本不得在 Golden Set 上退化。

    三条规则：
      ① Golden Set 只增不删：旧 case 必须全部出现在新跑批里；
      ② 无单点翻转：旧版通过、新版失败的 case 一律拒绝；
      ③ 通过率不得下降。
    """
    old_map = {c.case_id: c.passed for c in old}
    new_map = {c.case_id: c.passed for c in new}
    reasons: list[str] = []

    missing = sorted(cid for cid in old_map if cid not in new_map)
    if missing:
        reasons.append(f"Golden Set 缺项（只增不删被破坏）：{missing}")

    flipped = sorted(cid for cid, ok in old_map.items() if ok and not new_map.get(cid, False))
    if flipped:
        reasons.append(f"单点翻转（旧版通过、新版失败）：{flipped}")

    old_rate = (sum(old_map.values()) / len(old_map)) if old_map else 0.0
    new_rate = (sum(new_map.values()) / len(new_map)) if new_map else 0.0
    if new_rate + 1e-9 < old_rate:
        reasons.append(f"通过率下降：{old_rate:.3f} → {new_rate:.3f}")

    return GateResult(passed=not reasons, reasons=reasons, old_rate=old_rate, new_rate=new_rate)


def extract_entity_candidates(before_text: str, after_text: str) -> list[str]:
    """从「提交前后新出现的标识符」里抽取业务实体候选（单号/流水号）。

    锚点⑤ 要落地，系统必须能自己发现「什么才算真成功的证据」。
    这里是启发式：only（字母+数字）或（纯长数字）且 after 里新出现。
    """
    import re

    pattern = re.compile(r"\b(?:[A-Za-z]{1,4}[-_]?\d{4,}|\d{8,})\b")
    before = set(pattern.findall(before_text or ""))
    out: list[str] = []
    for token in pattern.findall(after_text or ""):
        if token in before:
            continue
        if token not in out:
            out.append(token)
    return out


def slug(name: str, fallback: str = "param") -> str:
    """把字段标签转成参数名（中文保留，其余转下划线）。"""
    text = norm_text(name, 40)
    if not text:
        return fallback
    cleaned = []
    for ch in text:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            cleaned.append(ch)
        elif ch in " -_/":
            cleaned.append("_")
    out = "".join(cleaned).strip("_").lower()
    return out or fallback
