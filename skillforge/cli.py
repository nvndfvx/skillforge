"""命令行入口：把闭环的每一段变成可执行命令。

    python -m skillforge compile  --trace-a a.json --trace-b b.json --out skill.json
    python -m skillforge review   --skill skill.json --steps-reviewed --confirm-assertions --precheck "R-\\d{6,}"
    python -m skillforge golden   add --store ./store --skill-id expense --case-id c1 --params '{"客户名称":"张三"}'
    python -m skillforge release  --store ./store --skill skill.json --scores scores.json
    python -m skillforge rollback --store ./store --skill-id expense
    python -m skillforge status   --store ./store --skill-id expense
    python -m skillforge run-golden --store ./store --skill skill.json --start-url file:///... --out scores.json

只有 run-golden 需要 playwright（真浏览器），其余纯 stdlib。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .compiler.distiller import distill
from .golden.suggest import suggest_cases
from .observer.aligner import align_actions
from .registry import GateRejected, SkillStore, load_skill, save_skill
from .schema.models import Assertion, CaseScore, GoldenCase
from .schema.serde import raw_trace_from_dict
from .schema.types import AnchorKind, AssertionKind
from .signals import SignalBook


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _read_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_scores(path: Path | str) -> list[CaseScore]:
    data = _read_json(path)
    items = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise SystemExit(f"scores 文件格式不对：{path}")
    out: list[CaseScore] = []
    for item in items:
        if isinstance(item, str):
            out.append(CaseScore(case_id=item, passed=True))
        else:
            out.append(
                CaseScore(
                    case_id=str(item.get("case_id") or ""),
                    passed=bool(item.get("passed")),
                    detail=str(item.get("detail") or ""),
                )
            )
    return out


def _print_list(title: str, items: Sequence[str]) -> None:
    if not items:
        return
    print(f"{title}：")
    for item in items:
        print(f"  - {item}")


def _print_escalations(escalated: Sequence[Any]) -> None:
    if not escalated:
        return
    print("\n⚠ technical 探针连续失败已升格（环境可能真的变了）：")
    for rec in escalated:
        print(f"  {rec.assertion_id}  连续 {rec.consecutive} 次  {rec.last_detail[:80]}")
    print("  建议：先确认环境是否变更，别急着改技能；确认后 python -m skillforge signals --reset")


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_compile(args: argparse.Namespace) -> int:
    trace_a = raw_trace_from_dict(_read_json(args.trace_a))
    trace_b = raw_trace_from_dict(_read_json(args.trace_b))
    report = align_actions(trace_a.actions, trace_b.actions)
    result = distill(
        report,
        trace_a.actions,
        skill_id=args.skill_id,
        name=args.name or args.skill_id,
    )
    save_skill(result.skill, args.out)

    print(f"编译完成 → {args.out}")
    print(f"  参数 {[p.name for p in result.skill.parameters]}")
    print(f"  步骤 {len(result.skill.steps)}  状态 {result.skill.state}")
    _print_list("对齐预警", report.warnings)
    _print_list("编译预警", result.warnings)
    _print_list("结构错误（draft 允许存在）", result.errors)
    blockers = result.skill.publish_blockers()
    _print_list("发布阻断（人审要清零）", blockers)
    return 0


def _ask(prompt: str, *, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "是", "1", "对")


def _review_interactive(skill, args: argparse.Namespace) -> int:
    """交互式人审：把三道勾从「记参数」变成「逐条看、逐条答」。

    为什么值得单独做：三道勾是整套系统的安全闸，但之前只能靠
    `--steps-reviewed --confirm-assertions --precheck "..."` 盲打，
    人根本不知道自己在确认什么。闸越关键，越不能靠背参数。
    """
    print(f"技能 {skill.skill_id} v{skill.version}  当前状态 {skill.state}\n")

    print("=== ① 步骤清单：确认顺序是不是你要的流程 ===")
    for step in skill.steps:
        flag = "  [写操作]" if step.is_write else ""
        target = step.target.name or step.target.css or "-"
        print(f"  {step.step_id}. {step.action.value:6s} 「{target}」 value={step.value!r}{flag}")
    if _ask("\n这串步骤就是你要的流程、顺序也对？"):
        skill.meta["steps_reviewed"] = True
    else:
        skill.meta["steps_reviewed"] = False
        print("  → 未通过：顺序或步骤有问题，先改 skill.json 再来")

    print("\n=== ② 断言：逐条确认 ===")
    for step in skill.steps:
        for assertion in (step.assertion, step.technical_assertion):
            if assertion is None:
                continue
            kind = "业务" if assertion.kind is AssertionKind.BUSINESS else "技术"
            print(f"  step{step.step_id} [{kind}] {assertion.operator} {assertion.expected!r}")
            if _ask("    这条断言对不对？", default=True):
                assertion.confirmed_by = "human"
            else:
                assertion.confirmed_by = "llm_proposed"

    print("\n=== ③ 写操作的 precheck：怎么判断这单已经存在 ===")
    for step in skill.steps:
        if not step.is_write or step.precheck is not None:
            continue
        default = args.precheck or r"R\-\d{6,}"
        try:
            answer = input(
                f"  step{step.step_id}「{step.target.name or '-'}」查重正则"
                f"（直接回车用 {default}，输入 - 表示先空着）> "
            ).strip()
        except EOFError:
            answer = ""
        if answer == "-":
            print("    → 留空：发布闸会拦住，直到你补上")
            continue
        step.precheck = Assertion(
            assertion_id=f"precheck-{step.step_id}",
            kind=AssertionKind.TECHNICAL,
            anchor=AnchorKind.UI_TEXT,
            operator="entity_exists",
            expected=answer or default,
            confirmed_by="human",
        )
        print(f"    → 已写入 precheck：{step.precheck.expected}")

    checklist = skill.review_checklist()
    skill.state = "reviewed" if all(checklist.values()) else "draft"
    blockers = skill.publish_blockers()

    out = Path(args.out or args.skill)
    save_skill(skill, out)

    print("\n人审三道勾：")
    for label, ok in checklist.items():
        print(f"  [{'x' if ok else ' '}] {label}")
    print(f"状态：{skill.state}")
    _print_list("还剩的发布阻断", blockers)
    if not blockers:
        print("→ 可以 release 了")
    return 0 if not blockers else 1


def cmd_review(args: argparse.Namespace) -> int:
    skill = load_skill(args.skill)

    if getattr(args, "interactive", False):
        return _review_interactive(skill, args)

    if args.steps_reviewed:
        skill.meta["steps_reviewed"] = True
    if args.confirm_assertions:
        for step in skill.steps:
            if step.assertion is not None:
                step.assertion.confirmed_by = "human"
            if step.technical_assertion is not None:
                step.technical_assertion.confirmed_by = "human"
    if args.precheck:
        for step in skill.steps:
            if step.is_write and step.precheck is None:
                step.precheck = Assertion(
                    assertion_id=f"precheck-{step.step_id}",
                    kind=AssertionKind.TECHNICAL,
                    anchor=AnchorKind.UI_TEXT,
                    operator="entity_exists",
                    expected=args.precheck,
                    confirmed_by="human",
                )

    # 状态由三道勾**推导**，不是独立闸。
    # 踩过的坑：publish_blockers() 里包含「状态不是 reviewed」这一条，
    # 如果再用它的结果决定要不要置 reviewed，就会自己锁自己，永远进不去。
    checklist = skill.review_checklist()
    skill.state = "reviewed" if all(checklist.values()) else "draft"
    blockers = skill.publish_blockers()

    out = Path(args.out or args.skill)
    save_skill(skill, out)

    print("人审三道勾：")
    for label, ok in checklist.items():
        print(f"  [{'x' if ok else ' '}] {label}")
    print(f"状态：{skill.state}")
    _print_list("还剩的发布阻断", blockers)
    if not blockers:
        print("→ 可以 release 了")
    return 0 if not blockers else 1


def cmd_golden(args: argparse.Namespace) -> int:
    store = SkillStore(args.store)
    if args.action == "list":
        cases = store.cases(args.skill_id)
        print(f"Golden Set：{len(cases)} 条")
        for case in cases:
            print(f"  {case.case_id:12s} origin={case.origin:13s} params={case.params}")
        return 0

    if args.action == "suggest":
        if not args.skill:
            print("suggest 需要 --skill（要从参数定义生成用例）")
            return 1
        skill = load_skill(args.skill)
        cases = suggest_cases(skill, limit=args.limit)
        print(f"生成 {len(cases)} 条候选 —— origin=manual_edge，**必须人确认才能当真值**")
        for case in cases:
            print(f"  {case.case_id:24s} {case.params}")
        if not args.apply:
            print("\n（只是候选，没有写库。确认后加 --apply 落盘）")
            return 0
        added = 0
        for case in cases:
            try:
                store.add_case(args.skill_id, case)
                added += 1
            except ValueError as exc:
                print(f"  跳过 {case.case_id}：{exc}")
        print(f"\n已写入 {added} 条，当前共 {len(store.cases(args.skill_id))} 条")
        return 0

    params = json.loads(args.params or "{}")
    case = GoldenCase(
        case_id=args.case_id,
        params=params,
        origin=args.origin or "demo",
        created_from_version=args.from_version or "1.0.0",
    )
    try:
        cases = store.add_case(args.skill_id, case)
    except ValueError as exc:
        print(f"失败：{exc}")
        return 1
    print(f"已加入 {args.case_id}，当前 {len(cases)} 条")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    store = SkillStore(args.store)
    skill = load_skill(args.skill)
    blockers = skill.publish_blockers()
    if blockers:
        print("拒绝发布：技能还没过人审闸")
        _print_list("阻断项", blockers)
        return 1

    scores = _load_scores(args.scores)
    try:
        record, gate = store.release(skill, scores, note=args.note or "")
    except GateRejected as exc:
        print("拒绝发布：单调性闸没过，**库里没有任何改动**")
        _print_list("原因", exc.gate.reasons)
        return 1

    print(f"已发布 {skill.skill_id} v{record.version}")
    print(f"  通过率 {record.rate:.3f}（上一版 {gate.old_rate:.3f}）")
    print(f"  用例 {len(record.cases)} 条")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    store = SkillStore(args.store)
    try:
        record = store.rollback(args.skill_id, to=args.to)
    except ValueError as exc:
        print(f"回滚失败：{exc}")
        return 1
    print(f"已回滚到 v{record.version}（通过率 {record.rate:.3f}）")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = SkillStore(args.store)
    status = store.status(args.skill_id)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def cmd_signals(args: argparse.Namespace) -> int:
    """H14：看 technical 探针的连续失败情况（只喊，不改技能）。"""
    store = SkillStore(args.store)
    book = SignalBook(store.root, threshold=args.threshold)

    if args.reset:
        count = book.reset(args.skill_id, args.assertion or None)
        print(f"已清零 {count} 条信号")
        return 0

    report = book.report(args.skill_id)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["escalated"]:
        print(f"\n⚠ {len(report['escalated'])} 条探针已升格：环境可能真的变了。")
        return 1 if args.fail_on_escalated else 0
    return 0


def cmd_heal(args: argparse.Namespace) -> int:
    """失败 → 三层定位 → 自动打补丁 → 过闸发布（或转人工）。"""
    from .executor.playwright_page import launch_page
    from .healer.patcher import heal

    store = SkillStore(args.store)
    skill = load_skill(args.skill)
    cases = store.cases(skill.skill_id)
    if not cases:
        print("Golden Set 是空的：没有判据就不该自动改，先 golden add / golden suggest")
        return 1

    params = json.loads(args.params) if args.params else dict(cases[0].params)
    params.setdefault("start_url", args.start_url)

    baseline: list[CaseScore] = []
    current = store.current_record(skill.skill_id)
    if args.dry_run:
        if current is None:
            print("--dry-run 需要库里有已发布版本当基线，否则无法过闸")
            return 1
        baseline = [
            CaseScore(case_id=str(c["case_id"]), passed=bool(c["passed"]))
            for c in current.cases
        ]

    async def flow() -> int:
        from .executor.candidates import collect_locator_candidates
        from .executor.driver import execute

        book = SignalBook(store.root, threshold=args.signal_threshold)

        # 1) 先在真页面上跑一次，拿到「真实的失败」
        async with launch_page(channel=args.channel) as page:
            failed_run = await execute(skill, params, page, run_id="heal::probe")
            # H14：探测运行也计入探针信号
            _print_escalations(book.record_run(skill, failed_run))
            if failed_run.ok:
                print("这次运行是成功的，没有问题可修。")
                return 0
            failed_step = failed_run.failed_step()
            print(f"探测运行失败：step {failed_step.step_id}（{failed_step.action}）"
                  f" → {failed_step.failure_layer or failed_step.status}")
            print(f"  {failed_step.detail}")
            # 2) 从**失败时的这个页面**收集候选定位器
            candidates = await collect_locator_candidates(page, skill)
        if candidates:
            print(f"候选定位器：{ {k: [c.key() for c in v] for k, v in candidates.items()} }")
        else:
            print("候选定位器：无（指纹也认不出来了）")

        # 3) 每个候选补丁都要在 Golden Set 上过闸
        async def golden_for(candidate_skill) -> list[CaseScore]:
            scores: list[CaseScore] = []
            for case in cases:
                case_params = dict(case.params)
                case_params.setdefault("start_url", args.start_url)
                async with launch_page(channel=args.channel) as case_page:
                    run = await execute(
                        candidate_skill, case_params, case_page, run_id=f"heal::{case.case_id}"
                    )
                failed = run.failed_step()
                scores.append(
                    CaseScore(
                        case.case_id,
                        run.ok,
                        (failed.detail or failed.status) if failed else "",
                    )
                )
            return scores

        result = await heal(
            skill,
            failed_run,
            run_golden=golden_for,
            store=None if args.dry_run else store,
            baseline_scores=baseline if args.dry_run else None,
            locator_candidates=candidates,
            note="cli heal",
        )

        print("\n补丁尝试：")
        for attempt in result.attempts:
            mark = "收下" if attempt.accepted else ("试用" if attempt.applied else "跳过")
            line = f"  [{mark}] {attempt.operator_id:18s}"
            if attempt.applied:
                line += f" 通过率 {attempt.rate:.2f}（{len(attempt.scores)} 条）"
                if attempt.gate is not None and not attempt.gate.passed:
                    line += " → 闸拒：" + "；".join(attempt.gate.reasons)[:80]
                else:
                    line += f" {attempt.detail}"
            else:
                line += f" {attempt.reason or attempt.detail}"
            print(line)

        if result.accepted:
            where = "（dry-run，未落库）" if args.dry_run else f"并已发布 v{result.version}"
            print(f"\n结果：补丁被收下{where}")
            return 0
        print("\n结果：未修复，需人工介入")
        for reason in result.reasons[-1:]:
            print(f"  {reason}")
        return 1

    try:
        return asyncio.run(flow())
    except Exception as exc:  # noqa: BLE001
        print(f"heal 执行失败：{type(exc).__name__}: {str(exc)[:200]}")
        print("如果报 EPERM / WinError 5 / No module named 'playwright'，"
              "说明当前环境起不了浏览器，请在普通终端里跑并先装上 playwright。")
        return 1


def cmd_run_golden(args: argparse.Namespace) -> int:
    try:
        from .executor.playwright_page import launch_page
    except Exception as exc:  # noqa: BLE001
        print(f"需要 playwright：{type(exc).__name__}（先跑 scripts/bootstrap_playwright.py）")
        return 1

    store = SkillStore(args.store)
    skill = load_skill(args.skill)
    cases = store.cases(skill.skill_id)
    if not cases:
        print("Golden Set 是空的，先 golden add")
        return 1

    async def run_all() -> list[dict[str, Any]]:
        from .executor.driver import execute

        book = SignalBook(store.root, threshold=args.threshold)
        results: list[dict[str, Any]] = []
        escalated: list[Any] = []
        for case in cases:
            params = dict(case.params)
            params.setdefault("start_url", args.start_url)
            async with launch_page(channel=args.channel) as page:
                run = await execute(skill, params, page, run_id=f"golden::{case.case_id}")
            escalated.extend(book.record_run(skill, run))
            failed = run.failed_step()
            results.append(
                {
                    "case_id": case.case_id,
                    "passed": run.ok,
                    "detail": (failed.detail or failed.status) if failed else "",
                }
            )
        _print_escalations(escalated)
        return results

    try:
        results = asyncio.run(run_all())
    except Exception as exc:  # noqa: BLE001
        print(f"跑批失败：{type(exc).__name__}: {str(exc)[:200]}")
        print("如果报 EPERM / WinError 5，说明当前沙箱不允许启动浏览器，请在普通终端里跑。")
        return 1

    Path(args.out).write_text(
        json.dumps({"cases": results}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed = sum(1 for r in results if r["passed"])
    print(f"跑完 {len(results)} 条，通过 {passed} 条 → {args.out}")
    return 0 if passed == len(results) else 1


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillforge",
        description="观察 → 蒸馏 → 沙箱执行 → 异常自愈（纵向调优版）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("compile", help="把两遍演示轨迹编译成 Skill 草稿")
    p.add_argument("--trace-a", required=True)
    p.add_argument("--trace-b", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--skill-id", required=True)
    p.add_argument("--name", default="")
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("review", help="打人审三道勾")
    p.add_argument("--skill", required=True)
    p.add_argument("--steps-reviewed", action="store_true", help="① 步骤顺序已人审")
    p.add_argument("--confirm-assertions", action="store_true", help="② 断言已人审")
    p.add_argument("--precheck", default="", help="③ 写操作的查重正则，如 'R-\\d{6,}'")
    p.add_argument("--interactive", "-i", action="store_true",
                   help="逐条交互确认（推荐：闸越关键越别靠背参数）")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("golden", help="管理 Golden Set（只增不删）")
    p.add_argument("action", choices=("add", "list", "suggest"))
    p.add_argument("--store", required=True)
    p.add_argument("--skill-id", required=True)
    p.add_argument("--skill", default="", help="suggest 用：从哪个 Skill 的参数生成用例")
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--apply", action="store_true", help="suggest 用：确认后落盘")
    p.add_argument("--case-id", default="")
    p.add_argument("--params", default="{}")
    p.add_argument("--origin", default="demo", choices=("demo", "healed_failure", "manual_edge"))
    p.add_argument("--from-version", default="1.0.0")
    p.set_defaults(func=cmd_golden)

    p = sub.add_parser("release", help="过闸发布（没过闸库里不会有任何改动）")
    p.add_argument("--store", required=True)
    p.add_argument("--skill", required=True)
    p.add_argument("--scores", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("rollback", help="回滚到上一版（或指定版本）")
    p.add_argument("--store", required=True)
    p.add_argument("--skill-id", required=True)
    p.add_argument("--to", default=None)
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("status", help="看当前版本与发布记录")
    p.add_argument("--store", required=True)
    p.add_argument("--skill-id", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("run-golden", help="在真浏览器上跑 Golden Set，产出 scores.json")
    p.add_argument("--store", required=True)
    p.add_argument("--skill", required=True)
    p.add_argument("--start-url", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--channel", default="chrome")
    p.add_argument("--threshold", type=int, default=5,
                   help="technical 探针连续失败多少次升格告警")
    p.set_defaults(func=cmd_run_golden)

    p = sub.add_parser("heal", help="失败 → 自动打补丁 → 过闸发布（需要真浏览器）")
    p.add_argument("--store", required=True)
    p.add_argument("--skill", required=True)
    p.add_argument("--start-url", required=True)
    p.add_argument(
        "--params", default="", help="这次失败运行的入参 JSON；默认用第一条 Golden 用例"
    )
    p.add_argument("--channel", default="chrome")
    p.add_argument("--dry-run", action="store_true", help="只试补丁、不落库")
    p.add_argument("--signal-threshold", type=int, default=5,
                   help="technical 探针连续失败多少次升格告警")
    p.set_defaults(func=cmd_heal)

    p = sub.add_parser("signals", help="看 technical 探针的连续失败（H14，只喊不改）")
    p.add_argument("--store", required=True)
    p.add_argument("--skill-id", required=True)
    p.add_argument("--threshold", type=int, default=5)
    p.add_argument("--reset", action="store_true", help="人工确认后清零")
    p.add_argument("--assertion", default="", help="只清某一条信号")
    p.add_argument("--fail-on-escalated", action="store_true",
                   help="有升格信号时返回非零（方便接 CI）")
    p.set_defaults(func=cmd_signals)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
