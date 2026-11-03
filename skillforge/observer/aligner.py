"""序列对齐器（地基③）：把两遍演示对齐，**用差异推导变量，而不是让 LLM 猜**。

算法：动作元组 + Needleman–Wunsch（编辑距离式动态规划）。
不做 DOM diff —— DOM 太大、噪音太多，diff 出来一片红，没法用。

输出四类结论：
  对齐 + value 不同  → 变量（source=aligned_diff）
  对齐 + target 不同 → 定位器不稳定（编译期就提前预警）
  一侧多出步骤       → 条件分支候选 / 误触噪声
  完全一致           → 常量

关键防护（H1，见 README）：若两次演示没有刻意的参数差异，对齐会把所有入参判成常量，
产出一个「换参数就崩」的假技能。这里会显式报警。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..schema.models import slug
from ..schema.types import ActionTuple, RawAction, RawTrace, norm_name, norm_text

MIN_SIM = 0.45
GAP = -0.7
NO_MATCH_SCORE = -2.0

# 相似度权重：动作类型最重要，字段名其次，role 辅助，有无 value 只做微调
W_ACTION = 0.40
W_ROLE = 0.20
W_NAME = 0.30
W_HASVALUE = 0.10


# --------------------------------------------------------------------------- #
# 相似度与对齐
# --------------------------------------------------------------------------- #
def similarity(a: ActionTuple, b: ActionTuple) -> float:
    """两个动作元组的相似度，[0, 1]。"""
    score = 0.0
    if a.action == b.action:
        score += W_ACTION
    if a.role and a.role == b.role:
        score += W_ROLE
    score += W_NAME * difflib.SequenceMatcher(None, a.name_norm, b.name_norm).ratio()
    if a.has_value == b.has_value:
        score += W_HASVALUE
    return round(score, 6)


def nw_align(
    seq_a: Sequence[ActionTuple],
    seq_b: Sequence[ActionTuple],
    *,
    min_sim: float = MIN_SIM,
    gap: float = GAP,
) -> list[tuple[str, int | None, int | None, float]]:
    """Needleman–Wunsch 对齐。

    返回 op 列表，每项 (kind, i, j, sim)：
      kind = "match" | "del"（A 独有） | "ins"（B 独有）
    """
    n, m = len(seq_a), len(seq_b)
    if n == 0 and m == 0:
        return []

    sims = [[similarity(seq_a[i], seq_b[j]) for j in range(m)] for i in range(n)]
    NEG = float("-inf")
    dp = [[NEG] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + gap
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + gap

    def match_score(s: float) -> float:
        # 低于阈值的配对不给「打折分」，直接判为不该对齐（比两个 gap 更差）
        return (s * 2.0 - 1.0) if s >= min_sim else NO_MATCH_SCORE

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = dp[i - 1][j - 1] + match_score(sims[i - 1][j - 1])
            cand = dp[i - 1][j] + gap
            if cand > best:
                best = cand
            cand = dp[i][j - 1] + gap
            if cand > best:
                best = cand
            dp[i][j] = best

    ops: list[tuple[str, int | None, int | None, float]] = []
    i, j = n, m
    eps = 1e-9
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            s = sims[i - 1][j - 1]
            if abs(dp[i][j] - (dp[i - 1][j - 1] + match_score(s))) < eps:
                ops.append(("match", i - 1, j - 1, s))
                i -= 1
                j -= 1
                continue
        if i > 0 and abs(dp[i][j] - (dp[i - 1][j] + gap)) < eps:
            ops.append(("del", i - 1, None, 0.0))
            i -= 1
            continue
        if j > 0 and abs(dp[i][j] - (dp[i][j - 1] + gap)) < eps:
            ops.append(("ins", None, j - 1, 0.0))
            j -= 1
            continue
        # 数值兜底，理论上不可达
        if i > 0:
            ops.append(("del", i - 1, None, 0.0))
            i -= 1
        else:
            ops.append(("ins", None, j - 1, 0.0))
            j -= 1

    ops.reverse()
    return ops


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
@dataclass
class Variable:
    label: str
    suggested_name: str
    values: tuple[Any, Any]
    action: str
    step_index_a: int
    step_index_b: int
    source: str = "aligned_diff"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "suggested_name": self.suggested_name,
            "values": list(self.values),
            "action": self.action,
            "step_index_a": self.step_index_a,
            "step_index_b": self.step_index_b,
            "source": self.source,
        }


@dataclass
class UnstableLocator:
    label: str
    selector_a: str
    selector_b: str
    step_index_a: int
    step_index_b: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "selector_a": self.selector_a,
            "selector_b": self.selector_b,
            "step_index_a": self.step_index_a,
            "step_index_b": self.step_index_b,
        }


@dataclass
class AlignedPair:
    index_a: int
    index_b: int
    similarity: float
    value_changed: bool
    target_changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_a": self.index_a,
            "index_b": self.index_b,
            "similarity": round(self.similarity, 4),
            "value_changed": self.value_changed,
            "target_changed": self.target_changed,
        }


@dataclass
class AlignmentReport:
    pairs: list[AlignedPair] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    unstable: list[UnstableLocator] = field(default_factory=list)
    extra_in_a: list[int] = field(default_factory=list)
    extra_in_b: list[int] = field(default_factory=list)
    constant_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "pairs": [p.to_dict() for p in self.pairs],
            "variables": [v.to_dict() for v in self.variables],
            "unstable_locators": [u.to_dict() for u in self.unstable],
            "extra_in_a": list(self.extra_in_a),
            "extra_in_b": list(self.extra_in_b),
            "constant_count": self.constant_count,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _label_of(action: RawAction) -> str:
    """给变量取人类可读的字段名：优先字段标签，其次指纹的可访问名/邻接文案。"""
    label = norm_text(action.name, 80)
    if label:
        return label
    fp = action.fingerprint
    if fp is not None:
        for cand in (fp.accessible_name, fp.neighbor_text, fp.text_norm):
            text = norm_text(cand, 80)
            if text:
                return text
    return ""


UNSTABLE_FP_SIM = 0.80


def _valuediff(a: RawAction, b: RawAction) -> bool:
    return norm_text(a.value, 400) != norm_text(b.value, 400)


def _targetdiff(a: RawAction, b: RawAction) -> bool:
    """定位器不稳定判定。

    优先指纹相似度，selector 只作辅助信号。为什么不能只看 selector：
      - 录制期我们不生成精确 selector（留给 compiler），selector 为空时永远判不出不稳定；
      - selector 相同而指纹不同 = 元素被换掉了，会漏判；
      - selector 不同而指纹相同 = 只是 id 变了、元素没变，会误报。
    写这一版时的自我修正：最初只用 selector 比较，上面的三个问题同时存在。
    """
    fa, fb = a.fingerprint, b.fingerprint
    if fa is not None and fb is not None and fa.similarity(fb) < UNSTABLE_FP_SIM:
        return True
    sa, sb = (a.selector or "").strip(), (b.selector or "").strip()
    if sa and sb and sa != sb:
        return True
    return False


def align_actions(
    actions_a: Sequence[RawAction],
    actions_b: Sequence[RawAction],
    *,
    min_sim: float = MIN_SIM,
    gap: float = GAP,
) -> AlignmentReport:
    """对齐两遍演示的动作序列并分类。"""
    report = AlignmentReport()
    tuples_a = [a.to_tuple() for a in actions_a]
    tuples_b = [b.to_tuple() for b in actions_b]
    ops = nw_align(tuples_a, tuples_b, min_sim=min_sim, gap=gap)

    for kind, i, j, sim in ops:
        if kind == "del":
            report.extra_in_a.append(int(i))
            continue
        if kind == "ins":
            report.extra_in_b.append(int(j))
            continue

        ra, rb = actions_a[int(i)], actions_b[int(j)]
        value_changed = _valuediff(ra, rb)
        target_changed = _targetdiff(ra, rb)
        report.pairs.append(
            AlignedPair(
                index_a=int(i),
                index_b=int(j),
                similarity=sim,
                value_changed=value_changed,
                target_changed=target_changed,
            )
        )

        if target_changed:
            report.unstable.append(
                UnstableLocator(
                    label=_label_of(ra) or _label_of(rb),
                    selector_a=ra.selector,
                    selector_b=rb.selector,
                    step_index_a=int(i),
                    step_index_b=int(j),
                )
            )

        if value_changed:
            label = _label_of(ra) or _label_of(rb)
            report.variables.append(
                Variable(
                    label=label,
                    suggested_name=slug(label, fallback=f"param_{int(i)}"),
                    values=(ra.value, rb.value),
                    action=ra.action,
                    step_index_a=int(i),
                    step_index_b=int(j),
                )
            )
        elif not value_changed and not target_changed:
            report.constant_count += 1

        # 一边有值一边没值：结构不一致，多半是漏录或归一化失败
        if (ra.value is None) != (rb.value is None):
            report.warnings.append(
                f"step a#{i}/b#{j}「{_label_of(ra) or '未命名'}」一边有值一边无值，"
                "可能漏录或动作归一化失败，需人工核对。"
            )

    _post_checks(report, actions_a, actions_b)
    return report


def _post_checks(
    report: AlignmentReport,
    actions_a: Sequence[RawAction],
    actions_b: Sequence[RawAction],
) -> None:
    # H1：没有变量 = 两次演示没换参数 → 对齐会把入参判成常量 → 出假技能
    if not report.variables:
        report.warnings.append(
            "【严重】两次演示未产生任何变量差异：对齐会把所有入参判成常量，"
            "产出的技能在第一次换参数时就崩。请让第二次演示刻意更换客户名/金额/日期等入参后重录。"
        )

    # 变量重名：同一字段标签出现两次，参数绑定有歧义
    names = [v.suggested_name for v in report.variables]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        report.warnings.append(
            f"参数名重复：{dup}。同一标签在页面上出现多次，需按位置/指纹消歧后再命名。"
        )

    # 变量无标签：无法生成可读参数名
    unnamed = [v for v in report.variables if not v.label]
    if unnamed:
        report.warnings.append(f"有 {len(unnamed)} 个变量没有字段标签，无法确定参数语义。")

    # 演示长度差异过大：很可能不是同一流程
    la, lb = len(actions_a), len(actions_b)
    if la and lb and abs(la - lb) / max(la, lb) > 0.5:
        report.warnings.append(
            f"两次演示长度差异过大（{la} vs {lb} 步）：可能不是同一流程，"
            "或其中一遍混入了无关操作，建议重录。"
        )

    # 有未对齐步骤：可能是条件分支，也可能是误触，必须人判断
    if report.extra_in_a or report.extra_in_b:
        report.warnings.append(
            f"存在未对齐步骤（A 独有 {len(report.extra_in_a)} 步 / B 独有 "
            f"{len(report.extra_in_b)} 步）：它们是条件分支还是误触，需人工判定。"
        )

    # settle 竞态：相邻动作的 after_text 完全一致，说明快照可能串味（H2）
    for side, actions in (("A", actions_a), ("B", actions_b)):
        for prev, cur in zip(actions, actions[1:]):
            if prev.after_text and prev.after_text == cur.after_text:
                report.warnings.append(
                    f"演示 {side} 的 step#{prev.seq} 与 step#{cur.seq} 的 after_text 完全相同："
                    "可能是 settle 窗口内连点导致快照串味，也可能是这两步确实不改文本。"
                    "生成断言时请人工确认。"
                )

    if not report.pairs:
        report.errors.append("两遍演示没有任何可对齐的动作，无法编译技能。")


def align_traces(trace_a: RawTrace, trace_b: RawTrace, **kw: Any) -> AlignmentReport:
    return align_actions(trace_a.actions, trace_b.actions, **kw)
