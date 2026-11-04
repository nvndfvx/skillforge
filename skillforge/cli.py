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


def cmd_review(args: argparse.Namespace) -> int:
    skill = load_skill(args.skill)

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

        results: list[dict[str, Any]] = []
        for case in cases:
            params = dict(case.params)
            params.setdefault("start_url", args.start_url)
            async with launch_page(channel=args.channel) as page:
                run = await execute(skill, params, page, run_id=f"golden::{case.case_id}")
            failed = run.failed_step()
            results.append(
                {
                    "case_id": case.case_id,
                    "passed": run.ok,
                    "detail": (failed.detail or failed.status) if failed else "",
                }
            )
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
    p.set_defaults(func=cmd_run_golden)

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
