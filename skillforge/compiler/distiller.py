"""蒸馏器：对齐报告 → Skill 草稿。

职责边界（刻意克制）：
  - 只做「确定性的转换」：变量→参数、动作→步骤、指纹→定位器、写副作用打标。
  - **不发明**：precheck 查什么、业务规则对不对，编译器猜不出来，留给人。
  - 产物永远是 draft，断言全是 llm_proposed——发布闸在 `publish_blockers()`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..observer.aligner import AlignmentReport
from ..observer.recorder import infer_write_effect
from ..schema.models import (
    EntityCheck,
    Assertion,
    Parameter,
    Precondition,
    Skill,
    Step,
    extract_entity_candidates,
)
from ..schema.types import (
    ActionKind,
    AnchorKind,
    AssertionKind,
    Locator,
    RawAction,
    WriteEffect,
    norm_text,
)

_ACTION_MAP = {
    "goto": ActionKind.GOTO,
    "click": ActionKind.CLICK,
    "fill": ActionKind.FILL,
    "select": ActionKind.SELECT,
}

_DEFAULT_PAUSE_THRESHOLD_MS = 3000


@dataclass
class DistillResult:
    skill: Skill
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill.to_dict(),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# 纯函数小工具
# --------------------------------------------------------------------------- #
def entity_regex(token: str) -> str:
    """把具体单号泛化成正则：R-20240115 → R-\\d{6,}。

    位数用**下界**而不是精确值：单号长度会随年份/流水号增长，
    钉死 `{8}` 会在明年变成假阴性，让 Healer 去修一个其实没坏的技能。
    """
    out: list[str] = []
    i = 0
    while i < len(token):
        ch = token[i]
        if ch.isdigit():
            j = i
            while j < len(token) and token[j].isdigit():
                j += 1
            width = j - i
            lower = max(4, width - 2)
            out.append(r"\d{" + str(lower) + ",}")
            i = j
        elif ch.isalnum() or ch in "-_":
            out.append(re.escape(ch))
            i += 1
        else:
            i += 1
    return "".join(out)


def infer_type(values: Sequence[Any]) -> str:
    vals = [str(v) for v in values if v is not None]
    if not vals:
        return "string"
    if all(re.fullmatch(r"-?\d+", v) for v in vals):
        return "int"
    date_like = (
        r"\d{4}-\d{1,2}-\d{1,2}",
        r"\d{4}/\d{1,2}/\d{1,2}",
        r"\d{4}年\d{1,2}月\d{1,2}日",
    )
    if all(any(re.fullmatch(p, v) for p in date_like) for v in vals):
        return "date"
    return "string"


def _locator_of(action: RawAction) -> Locator:
    fp = action.fingerprint
    if fp is not None and (fp.role or fp.accessible_name):
        return Locator(role=fp.role or None, name=fp.accessible_name or None)
    if action.selector:
        return Locator(css=action.selector)
    if action.name:
        return Locator(name=action.name)
    return Locator()


def _template(name: str) -> str:
    return "{{" + name + "}}"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
# 业务实体号在真实系统里的常见形态。宁可多抓一种，也不要漏。
ID_SHAPES: tuple[str, ...] = (
    r"[A-Z]{1,5}[-_]?\d{4,}",                      # R-20247710 / TASK_12345
    r"\d{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}",        # 类 UUID 前缀
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
    r"\b\d{6,}\b",                                  # 纯长数字
)


def id_token(text: str) -> str:
    """从一段文本里揪出"像业务实体号"的那一段。"""
    for shape in ID_SHAPES:
        match = re.search(shape, text or "")
        if match:
            return match.group(0)
    return ""


def infer_entity_checks(
    steps: list[Step], param_examples: dict[str, str], warnings: list[str]
) -> int:
    """给跨标签页的步骤自动补上「关联性断言」。

    逻辑（只用录制里已有的证据，不猜业务规则）：
    1. 这个步骤在 tab>0（点开的新页面）
    2. 找到点开它的那一步 click，取出被点元素名字里的实体号（如「任务 R-20240115 待审核」→ R-20240115）
    3. 看有没有哪个参数的值也是这个形态 —— 有就挂上：运行时"我传的是 R-xxx，这一页必须是 R-xxx"
    4. 找不到参数就不硬凑（生成了假判据比没有更糟），只给警告要人审补

    返回补上的条数。
    """
    added = 0
    for index, step in enumerate(steps):
        if int(getattr(step, "tab", 0) or 0) == 0 or step.entity_check is not None:
            continue

        opener = None
        for prev in reversed(steps[:index]):
            if (
                int(getattr(prev, "tab", 0) or 0) < int(step.tab or 0)
                and prev.action is ActionKind.CLICK
            ):
                opener = prev
                break
        if opener is None:
            continue

        token = id_token(opener.target.name or "") or id_token(str(opener.value or ""))
        if not token:
            warnings.append(
                f"step {step.step_id} 是点开新页面后的动作，但点开它的元素名字里"
                "看不出业务实体号（如单号/任务号）—— 关联断言没生成，人审要补。"
            )
            continue

        pattern = entity_regex(token)
        matched = [name for name, example in param_examples.items() if re.search(pattern, example or "")]
        if len(matched) == 1:
            param_name = matched[0]
            step.entity_check = EntityCheck(
                pattern=pattern,
                param=param_name,
                note=f"自动推断：由「{token}」同形匹配到参数「{param_name}」",
            )
            # 顺手把「点哪一行」也参数化：单号写死在定位器里，这份技能就只能
            # 处理那一单，换一条记录就得重录 —— 那就谈不上复用了。
            opener_name = opener.target.name or ""
            if token in opener_name:
                import dataclasses

                opener.target = dataclasses.replace(
                    opener.target, name=opener_name.replace(token, "{{" + param_name + "}}")
                )
                warnings.append(
                    f"step {opener.step_id} 的定位文本已参数化："
                    f"「{opener_name}」→「{opener.target.name}」，换记录只需改参数。"
                )
            added += 1
        else:
            hint = "多个参数都像" if matched else "没有任何参数带着这个实体号"
            warnings.append(
                f"step {step.step_id} 打开了「{opener.target.name}」，实体号形如「{token}」，"
                f"但{hint} —— 关联断言没生成（宁可没有也不放假判据），人审要补上。"
            )
    return added


def distill(
    report: AlignmentReport,
    actions_a: Sequence[RawAction],
    *,
    skill_id: str,
    name: str = "",
    description: str = "",
    version: str = "1.0.0",
    pause_threshold_ms: int = _DEFAULT_PAUSE_THRESHOLD_MS,
) -> DistillResult:
    warnings: list[str] = []

    # ---- 1. 变量 → 参数（按步骤顺序，保证可复现） ----
    # H9：snapshot_diff 补录的字段多半是「联动填充」（选客户→自动带出地址）。
    # 它不是人填的，不该被参数化，也不该被主动回填。
    snapshot_diff_steps = {
        pair.index_a
        for pair in report.pairs
        if actions_a[pair.index_a].source == "snapshot_diff"
    }
    var_by_step: dict[int, tuple[str, Any]] = {}
    parameters: list[Parameter] = []
    used_names: set[str] = set()
    for var in sorted(report.variables, key=lambda v: v.step_index_a):
        if var.step_index_a in snapshot_diff_steps:
            warnings.append(
                f"字段「{var.label or var.suggested_name}」是程序赋值/联动字段，"
                "两次演示值不同但它不是入参，已跳过参数化。"
            )
            continue
        base = var.suggested_name or f"param_{var.step_index_a}"
        pname = base
        n = 2
        while pname in used_names:
            pname = f"{base}_{n}"
            n += 1
        used_names.add(pname)
        parameters.append(
            Parameter(
                name=pname,
                type=infer_type(var.values),
                required=True,
                source="aligned_diff",
                example=var.values[0],
            )
        )
        var_by_step[var.step_index_a] = (pname, var.values[0])
        if var.label and pname != var.label:
            warnings.append(f"参数「{var.label}」重名，已改名为「{pname}」。")

    if not parameters:
        warnings.append(
            "【严重】对齐没有产出任何变量：编译结果会把所有入参硬编码。"
            "请换参数重录第二遍演示（见 MVP 验收第 3 条）。"
        )

    # ---- 2. 对齐后的动作 → 步骤 ----
    fragile_steps = {u.step_index_a for u in report.unstable}
    steps: list[Step] = []
    for step_id, pair in enumerate(report.pairs, start=1):
        raw = actions_a[pair.index_a]
        is_snapshot = pair.index_a in snapshot_diff_steps

        if is_snapshot:
            # H9：编成只读校验，不主动回填。人审确认后再升级成 fill。
            action_kind = ActionKind.READ_ENTITY
        else:
            action_kind = _ACTION_MAP.get(raw.action)
            if action_kind is None:
                warnings.append(
                    f"未知动作类型「{raw.action}」，按 click 处理：{_locator_of(raw).key()}"
                )
                action_kind = ActionKind.CLICK

        value: Any = raw.value
        if action_kind is ActionKind.GOTO:
            # goto 的目标放在 value 里（Step 没有独立 url 字段）
            value = raw.url or ""
        elif not is_snapshot and pair.index_a in var_by_step:
            value = _template(var_by_step[pair.index_a][0])

        write_effect = infer_write_effect(raw.action, raw.name)
        step = Step(
            step_id=step_id,
            action=action_kind,
            target=_locator_of(raw),
            tab=int(getattr(raw, "tab", 0) or 0),
            value=value,
            write_effect=write_effect,
            fingerprint=raw.fingerprint,
            fragile=pair.index_a in fragile_steps,
            note="snapshot_diff" if raw.source == "snapshot_diff" else "",
        )
        if is_snapshot:
            warnings.append(
                f"step {step_id}「{raw.name or raw.value}」是程序赋值/联动字段，"
                "已编成只读校验（READ_ENTITY），**不会主动回填**。"
                "若确认这里确实需要人工填写，请人审时升级成 fill 并加参数。"
            )
        if write_effect is not WriteEffect.NONE:
            step.idempotency_key = "{{flow_run_id}}::step" + str(step_id)
            warnings.append(
                f"step {step_id} 是写操作（{write_effect.value}），已生成幂等键占位，"
                "但 precheck 未生成——编译器不知道「怎么查这张单是否已存在」，必须人工补。"
            )
        steps.append(step)

    if not steps:
        return DistillResult(
            skill=Skill(skill_id=skill_id, name=name, description=description, version=version),
            warnings=warnings + ["没有任何可编译的步骤。"],
            errors=["编译失败：步骤为空。"],
        )

    # ---- 3. 断言：最后一屏的业务回执 ----
    last_raw = actions_a[report.pairs[-1].index_a]
    last_step = steps[-1]
    candidates = extract_entity_candidates(last_raw.before_text, last_raw.after_text)
    if candidates:
        token = candidates[0]
        last_step.assertion = Assertion(
            assertion_id=f"{skill_id}-st{last_step.step_id}-entity",
            kind=AssertionKind.BUSINESS,
            anchor=AnchorKind.BUSINESS_ENTITY,
            operator="entity_exists",
            target=last_step.target,
            expected=entity_regex(token),
            confirmed_by="llm_proposed",
            source="observed",
        )
        if len(candidates) > 1:
            warnings.append(
                f"最后一屏发现多个候选单号 {candidates}，只取了第一个，请人审时确认。"
            )
    else:
        warnings.append(
            "【严重】最后一屏没有发现新的业务单号，business 断言无法自动生成。"
            "请人工指定「成功的证据是什么」（单号/流水号），否则 Golden Set 没有真值。"
        )

    # ---- 4. 技术断言（环境探针）：URL 变化 ----
    first_url = next((a.url for a in actions_a if a.url), "")
    if last_raw.url and first_url and last_raw.url != first_url:
        last_step.technical_assertion = Assertion(
            assertion_id=f"{skill_id}-st{last_step.step_id}-url",
            kind=AssertionKind.TECHNICAL,
            anchor=AnchorKind.UI_TEXT,
            operator="url_match",
            target=None,
            expected=last_raw.url,
            confirmed_by="llm_proposed",
            source="observed",
        )

    # ---- 5. 前置条件：起始页 ----
    preconditions: list[Precondition] = []
    start_url = next((a.url for a in actions_a if a.url), "")
    if start_url:
        preconditions.append(Precondition(kind="on_page", locator_or_path=start_url))

    # ---- 6. 人审优先清单：停顿时长 = 判断成本 ----
    costly = [
        {"step_id": sid, "pause_before_ms": actions_a[pair.index_a].pause_before_ms}
        for sid, pair in enumerate(report.pairs, start=1)
        if actions_a[pair.index_a].pause_before_ms >= pause_threshold_ms
    ]
    if costly:
        warnings.append(
            f"有 {len(costly)} 步停顿超过 {pause_threshold_ms}ms，说明这些步人做了判断，"
            "人审时优先看它们（见 meta.review_priority）。"
        )

    meta: dict[str, Any] = {
        "variables": [v.to_dict() for v in report.variables],
        "fragile_step_ids": sorted(
            sid for sid, pair in enumerate(report.pairs, start=1) if pair.index_a in fragile_steps
        ),
        "unstable_locators": [u.to_dict() for u in report.unstable],
        "align_warnings": list(report.warnings),
        "distill_warnings": list(warnings),
        "review_priority": costly,
        "snapshot_diff_steps": [s.step_id for s in steps if s.note == "snapshot_diff"],
        "known_limitations": [
            "precheck（查重查询）无法自动生成，必须人工补。",
            "编译器只从录制里取证据，学不到业务规则。",
        ],
    }

    # 跨页步骤必须能自证"停的是哪条记录"，否则点错行也会显示提交成功
    cross_tab = [s for s in steps if int(getattr(s, "tab", 0) or 0) > 0]
    if cross_tab:
        # 先用录制里的证据自动补（能推出来就不用麻烦人）
        param_examples = {name: str(example) for name, example in var_by_step.values()}
        added = infer_entity_checks(steps, param_examples, warnings)
        if added:
            warnings.append(
                f"已自动为 {added} 个跨页步骤生成关联断言（同形实体号 + 参数匹配）。"
                "人审请确认：这些步骤确实只应作用于该实体号的记录。"
            )
        missing = [s.step_id for s in steps if int(getattr(s, "tab", 0) or 0) > 0 and s.entity_check is None]
        if missing:
            warnings.append(
                f"跨页步骤 {missing} 没有关联性断言（entity_check）："
                "跑批时无法证明点开的是同一条记录，人审必须补上（给出实体号形态 + 用哪个参数比对）。"
            )

    skill = Skill(
        skill_id=skill_id,
        name=name or skill_id,
        description=description,
        version=version,
        parameters=parameters,
        preconditions=preconditions,
        steps=steps,
        meta=meta,
        state="draft",
    )
    return DistillResult(skill=skill, warnings=warnings, errors=skill.errors())


__all__ = ["DistillResult", "distill", "entity_regex", "infer_type"]
