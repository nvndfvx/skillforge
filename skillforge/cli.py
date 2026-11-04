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


def _finish_review(skill, out: Path) -> int:
    """收尾：状态由三道勾推导 → 写盘 → 打印结论。"""
    checklist = skill.review_checklist()
    skill.state = "reviewed" if all(checklist.values()) else "draft"
    blockers = skill.publish_blockers()
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

    if getattr(args, "export_html", ""):
        from .webui import dump_form

        path = dump_form(skill, args.export_html)
        print(f"表单已导出：{path}")
        print("  静态页只能看、不能提交；要真提交用 --serve")
        return 0

    if getattr(args, "serve", False):
        from .webui import serve

        out = Path(args.out or args.skill)
        save_skill(skill, out)  # 先把当前内容落盘，页面里看到的就是这一版
        session = serve(skill, out, port=args.port, open_browser=not args.no_browser)
        if not session.submitted:
            print("没有提交，技能文件保持原样。")
            return 1
        return _finish_review(session.skill, out)

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


def cmd_doctor(args: argparse.Namespace) -> int:
    """环境自检：把每一环拆开验，说清哪一环没通、该怎么办。"""
    from .doctor import FAIL, HEADER, format_checks, run_checks, summary_line, worst

    print(HEADER + "\n")
    checks = run_checks(channel=args.channel, deep=not args.shallow)
    print(format_checks(checks))
    level = worst(checks)
    print()
    print(summary_line(checks))
    if level == FAIL:
        print("（按上面带 → 的提示处理；还不行就把整屏发我）")
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """按 skill.domain 分派：browser 走 playwright，cli 走子进程。"""
    skill = load_skill(args.skill)
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        print(f"--params 不是合法 JSON：{exc}")
        return 1

    blockers = skill.publish_blockers()
    errors = skill.errors()

    if skill.domain == "cli":
        from .cli_domain import preview, run_cli_skill

        if errors:
            _print_list("技能结构问题", errors)
            return 1
        print(f"CLI 域：{skill.skill_id} v{skill.version}")
        for line in preview(skill):
            print(line)
        print()
        result = asyncio.run(
            run_cli_skill(skill, params, cwd=args.cwd or None, timeout_s=args.timeout)
        )
        for step in result.steps:
            mark = {"ok": "v", "failed": "x", "skipped": "-", "blocked": "!"}.get(step.status, "?")
            print(f"  {mark} step{step.step_id} {step.status:8s} {step.detail[:100]}")
        print(f"\n结果：{'成功' if result.ok else '失败'}")
        return 0 if result.ok else 1

    # browser 域
    if errors:
        _print_list("技能结构问题", errors)
        return 1
    if blockers:
        _print_list("发布阻断（browser 域需要人审完才能跑）", blockers)
        return 1
    try:
        from .executor.playwright_page import launch_page
    except Exception as exc:  # noqa: BLE001
        print(f"需要 playwright：{type(exc).__name__}（pip install \"skillforge-engine[browser]\"）")
        return 1

    from .executor.driver import execute

    params.setdefault("start_url", args.start_url) if args.start_url else None
    if args.start_url:
        _override_start_url(skill, args.start_url)
    async def flow():
        async with launch_page(channel=args.channel, profile_dir=args.profile or None) as page:
            return await execute(skill, params, page, run_id=args.run_id or "run")

    try:
        result = asyncio.run(flow())
    except Exception as exc:  # noqa: BLE001
        print(f"执行失败：{type(exc).__name__}: {str(exc)[:200]}")
        return 1

    for step in result.steps:
        mark = {"ok": "v", "failed": "x", "skipped": "-", "blocked": "!"}.get(step.status, "?")
        print(f"  {mark} step{step.step_id} {step.status:8s} {(step.detail or '')[:100]}")
    print(f"\n结果：{'成功' if result.ok else '失败'}")
    return 0 if result.ok else 1


def cmd_flow(args: argparse.Namespace) -> int:
    """多技能编排：先查 → 再下单 → 再通知。"""
    from .flow import Flow

    store = SkillStore(args.store)
    try:
        flow = Flow.from_dict(json.loads(Path(args.flow).read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"编排文件读不了：{exc}")
        return 1

    skills: dict[str, Any] = {}
    missing: list[str] = []
    for step in flow.steps:
        if step.skill_id in skills or step.skill_id in missing:
            continue
        skill = store.load_skill(step.skill_id)
        if skill is None:
            missing.append(step.skill_id)
        else:
            skills[step.skill_id] = skill
    if missing:
        print(f"库里没有这些技能的已发布版本：{missing}")
        print("  先给它们 release，编排才能引用 —— 不允许拿草稿编排")
        return 1

    problems = flow.validate(skills)
    if problems:
        _print_list("编排有问题", problems)
        return 1

    if args.action == "validate":
        print(f"编排 OK：{flow.flow_id}（{len(flow.steps)} 步，技能 {sorted(skills)}）")
        for index, step in enumerate(flow.steps, start=1):
            flag = "  [可失败]" if step.optional else ""
            print(
                f"  {index}. {step.step_id} → {step.skill_id} "
                f"v{skills[step.skill_id].version}{flag}"
            )
            if step.params:
                print(f"       params={json.dumps(step.params, ensure_ascii=False)}")
        return 0

    if not args.out:
        print("run 需要 --out <报告路径>")
        return 1

    try:
        flow_params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        print(f"--params 不是合法 JSON：{exc}")
        return 1

    from playwright.async_api import async_playwright

    from .executor.playwright_page import PlaywrightPage
    from .flow import run_flow

    async def flow_it():
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel=args.channel, headless=True)
            try:

                async def factory(step):
                    return PlaywrightPage(await browser.new_page())

                async def teardown(adapter):
                    await adapter.page.close()

                return await run_flow(
                    flow,
                    skills,
                    factory,
                    params=flow_params,
                    flow_run_id=args.run_id or f"flow::{flow.flow_id}",
                    teardown=teardown,
                    interval_ms=args.interval_ms,
                )
            finally:
                await browser.close()

    try:
        result = asyncio.run(flow_it())
    except Exception as exc:  # noqa: BLE001
        print(f"编排执行失败：{type(exc).__name__}: {str(exc)[:200]}")
        print("若报 No module named 'playwright'，先装：pip install \"skillforge[browser]\"")
        return 1

    Path(args.out).write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n编排 {flow.flow_id}：")
    for step in result.steps:
        mark = {"ok": "v", "failed": "x", "skipped": "-"}[step.status]
        extra = f"  {step.detail[:90]}" if step.detail else ""
        outputs = step.outputs.get("entity")
        tail = f"  → 产出 {outputs}" if outputs else ""
        print(f"  {mark} {step.step_id:12s} {step.skill_id:12s} {step.status:8s}{tail}{extra}")

    done = [s.step_id for s in result.done]
    if result.ok:
        print(f"\n完成：{len(done)} 步全成功")
        return 0
    print(f"\n中止于 {result.aborted_at or '（可失败步骤）'}")
    print(f"  已完成：{done}")
    print(f"  已失败：{[s.step_id for s in result.failed]}")
    print(f"  未执行：{[s.step_id for s in result.skipped]}")
    print("  注意：浏览器里的写操作**没有回滚**，已经做成的那几步请人工确认后再决定怎么办")
    print(f"  报告 → {args.out}")
    return 1


def cmd_canary(args: argparse.Namespace) -> int:
    """灰度发布：候选版本先接一小部分真实请求。"""
    from .canary import CanaryBook, CanaryState

    store = SkillStore(args.store)
    book = CanaryBook(store.root)
    skill_id = args.skill_id

    if args.action == "start":
        if not args.candidate:
            print("start 需要 --candidate <候选技能.json>")
            return 1
        if args.mode in ("param", "segment") and not args.field:
            print(f"--mode {args.mode} 需要 --field <参数名>，例如 --field 客户编号")
            return 1
        segments = [s.strip() for s in (args.segments or "").split(",") if s.strip()]
        if args.mode == "segment" and not segments:
            print("--mode segment 需要 --segments a,b,c（放行哪些分群）")
            return 1
        if args.mode != "segment" and not 0 < args.weight < 1:
            print("--weight 必须在 (0,1) 之间，例如 0.1")
            return 1
        current = store.current_record(skill_id)
        if current is None:
            print("库里没有已发布版本，先 release 一版再开灰度")
            return 1
        try:
            candidate = load_skill(args.candidate)
        except Exception as exc:  # noqa: BLE001
            print(f"候选技能读不了：{type(exc).__name__}: {exc}")
            return 1
        if candidate.version == current.version:
            print(f"候选版本号和当前版本一样（{current.version}），改一下 version 再开灰度")
            return 1
        if candidate.publish_blockers():
            _print_list("候选技能还有发布阻断（先 review）", candidate.publish_blockers())
            return 1

        book.candidate_path(skill_id).parent.mkdir(parents=True, exist_ok=True)
        book.candidate_path(skill_id).write_text(
            json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        state = CanaryState(
            skill_id=skill_id,
            candidate_version=candidate.version,
            weight=args.weight,
            min_samples=args.min_samples,
            mode=args.mode,
            field=args.field,
            segments=segments,
        )
        state.created_at = state.to_dict()["created_at"]
        book.save(state)
        print(f"灰度已开启：{current.version}（稳定） ← {int(args.weight * 100)}% → {candidate.version}（候选）")
        print(f"  路由方式：{state.describe()}")
        print(f"  样本门槛：候选满 {args.min_samples} 条才给结论")
        print(f"  下一步：skillforge batch --canary ... 然后 skillforge canary status")
        return 0

    state = book.load(skill_id)

    if args.action == "abort":
        if state is None:
            print("当前没有灰度")
            return 0
        book.clear(skill_id)
        print(f"灰度已撤销，回收了 {state.candidate_version} 的 {int(state.weight * 100)}% 流量")
        return 0

    if state is None:
        print("当前没有灰度（用 canary start 开启）")
        return 0

    current = store.current_record(skill_id)
    stable_version = current.version if current else "-"

    if args.action == "status":
        verdict = state.verdict(stable_version)
        print(json.dumps(
            {
                "skill_id": skill_id,
                "candidate_version": state.candidate_version,
                "weight": state.weight,
                "samples": state.samples,
                **verdict,
            },
            ensure_ascii=False,
            indent=2,
        ))
        print(f"\n结论：{verdict['decision']} —— {verdict['reason']}")
        return 0

    # promote
    verdict = state.verdict(stable_version)
    print(f"灰度结论：{verdict['decision']} —— {verdict['reason']}")
    if not verdict["enough_samples"] and not args.force:
        print(f"候选样本不足（{verdict['candidate']['samples']}/{state.min_samples}），"
              f"再跑几批，或加 --force 强行提升")
        return 1
    if verdict["decision"] == "abort" and not args.force:
        print("候选更差，拒绝提升（确实要提升就加 --force）")
        return 1
    if not args.scores:
        print("promote 仍需过发布闸：请提供 --scores <scores.json>")
        print("  灰度只证明「真实数据上没更差」，闸才证明「基准集上没退化」")
        return 1

    candidate = load_skill(book.candidate_path(skill_id))
    scores = _load_scores(args.scores)
    try:
        record = store.release(candidate, scores)
    except GateRejected as exc:
        print("发布闸拒绝，灰度保持原样：")
        _print_list("原因", exc.gate.reasons)
        return 1

    book.clear(skill_id)
    print(f"已提升 {candidate.skill_id} v{record.version}（通过率 {record.rate:.3f}），灰度已结束")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """批量干活：失败隔离 + 断点续跑。browser 域要真浏览器，cli 域不需要。"""
    from .batch import load_items, run_batch

    store = SkillStore(args.store)
    skill = load_skill(args.skill)
    _override_start_url(skill, args.start_url)
    is_cli = skill.domain == "cli"

    try:
        items = load_items(json.loads(Path(args.cases).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"清单读不了：{exc}")
        return 1

    ckpt_path = store.root / skill.skill_id / "batch.json"
    checkpoint: dict[str, str] = {}
    if args.resume and ckpt_path.exists():
        try:
            checkpoint = json.loads(ckpt_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("断点文件坏了，忽略，从头跑")
        else:
            print(f"续跑：{len(checkpoint)} 条上次已成功，会跳过")

    async def flow():
        from .canary import CanaryBook

        book = CanaryBook(store.root)
        state = book.load(skill.skill_id) if args.canary else None
        candidate = None
        if state is not None:
            candidate = load_skill(book.candidate_path(skill.skill_id))
            print(
                f"灰度路由：{skill.version}（稳定）← {int(state.weight * 100)}% → "
                f"{candidate.version}（候选），按 item_id 粘性分桶"
            )

        def resolver(item):
            return candidate if state.routes(item.item_id, item.params) else skill

        if is_cli:
            from .cli_domain import make_runner

            report = await run_batch(
                skill,
                items,
                None,
                checkpoint=checkpoint,
                limit=args.limit or None,
                interval_ms=args.interval_ms,
                stop_after_failures=args.stop_after_failures or None,
                resolver=resolver if state is not None else None,
                runner=make_runner(cwd=args.cwd or None, timeout_s=args.timeout),
            )
        else:
            from playwright.async_api import async_playwright

            from .executor.playwright_page import PlaywrightPage

            async with async_playwright() as p:
                browser = await p.chromium.launch(channel=args.channel, headless=True)
                try:

                    async def factory(item):
                        return PlaywrightPage(await browser.new_page())

                    async def teardown(adapter):
                        await adapter.page.close()

                    report = await run_batch(
                        skill,
                        items,
                        factory,
                        checkpoint=checkpoint,
                        limit=args.limit or None,
                        interval_ms=args.interval_ms,
                        stop_after_failures=args.stop_after_failures or None,
                        teardown=teardown,
                        resolver=resolver if state is not None else None,
                    )
                finally:
                    await browser.close()

        if state is not None:
            for result in report.results:
                if result.status == "skipped":
                    continue
                state.record(result.version or skill.version, result.ok)
            book.save(state)
            verdict = state.verdict(skill.version)
            print(f"\n灰度累计：{state.samples}")
            print(f"灰度结论：{verdict['decision']} —— {verdict['reason']}")
        return report

    try:
        report = asyncio.run(flow())
    except Exception as exc:  # noqa: BLE001
        print(f"批量执行失败：{type(exc).__name__}: {str(exc)[:200]}")
        print("若报 No module named 'playwright'，先装：pip install \"skillforge[browser]\"")
        return 1

    Path(args.out).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    merged = dict(checkpoint)
    merged.update(report.checkpoint())
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"\n批完成：共 {report.total} 条，成功 {report.passed}，"
        f"失败 {report.failed}，跳过 {report.skipped}，通过率 {report.rate:.2%}"
    )
    if args.canary:
        print(f"按版本：{report.by_version()}")
    if report.failed:
        print(f"失败分布（该修代码还是该修数据）：{report.by_layer()}")
        for result in report.failures()[:10]:
            print(f"  x {result.item_id} [{result.failure_layer}] {result.detail[:100]}")
        if report.failed > 10:
            print(f"  …… 还有 {report.failed - 10} 条，见报告")
    print(f"报告 → {args.out}")
    return 0 if report.failed == 0 else 1


def cmd_record(args: argparse.Namespace) -> int:
    """打开一个真浏览器，让人操作一遍，录成 Trace.json。

    注意：`input()` 会阻塞事件循环，而页面事件是靠绑定回调送进来的 ——
    所以必须 `asyncio.to_thread(input, ...)`，否则你在浏览器里点半天，一条都录不到。
    """
    from .executor.playwright_page import launch_page
    from .observer.recorder import TraceRecorder

    profile = args.profile or None

    async def flow() -> int:
        # 走 launch_page：它统一处理临时目录，并支持 --profile 复用登录状态
        async with launch_page(
            channel=args.channel, headless=args.headless, profile_dir=profile
        ) as adapter:
            page = adapter.page
            await page.goto(args.url, wait_until="domcontentloaded")
            if profile:
                print(f"\n使用持久化配置：{profile}（登过一次就不用再登）")
            recorder = TraceRecorder(
                page, settle_timeout_ms=args.settle, context=getattr(page, "context", None)
            )
            await recorder.start()
            print(f"\n开始录制：{args.url}")
            print("  从现在起，在浏览器里把你的操作走一遍。")
            if not profile:
                print("  ！如果这里被跳到登录页：登录动作也会被录进技能里。")
                print("    正式录之前先加 --profile <目录> 登一次，再重新录。")
            print("  ！两次演示要故意换参数（客户名/金额…），否则变量会被判成常量。")
            print("  ！点开新标签页也没关系：后续动作会记在正确的标签页上。")

            # 每 400ms 定型一批：否则所有动作共用结束时那一次 settle，
            # 录到的 after_text 全一样，"操作前/后文本"就失去意义（断言会编错）。
            async def _auto_flush() -> None:
                while True:
                    await asyncio.sleep(0.4)
                    try:
                        await recorder.flush(grace_ms=0)
                    except Exception:  # noqa: BLE001
                        return

            flusher = asyncio.create_task(_auto_flush())
            try:
                await asyncio.to_thread(input, "\n操作完成后，回到这个窗口按回车结束录制 > ")
            finally:
                flusher.cancel()
            trace = await recorder.stop()

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(trace.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已保存 {len(trace.actions)} 个动作 → {out}")
        if trace.meta.get("snapshot_reconciled"):
            print(f"  其中 {trace.meta['snapshot_reconciled']} 个字段是从表单快照补录的")
        print(f"\n下一步：再录一遍（换参数），然后\n"
              f"  python -m skillforge compile --trace-a {out} --trace-b <第二遍> "
              f"--out skill.json --skill-id {args.skill_id}")
        return 0

    try:
        return asyncio.run(flow())
    except Exception as exc:  # noqa: BLE001
        print(f"录制失败：{type(exc).__name__}: {str(exc)[:200]}")
        print("若报 No module named 'playwright'，先装：pip install \"skillforge[browser]\"")
        return 1


def cmd_heal(args: argparse.Namespace) -> int:
    """失败 → 三层定位 → 自动打补丁 → 过闸发布（或转人工）。"""
    from .executor.playwright_page import launch_page
    from .healer.patcher import heal

    store = SkillStore(args.store)
    skill = load_skill(args.skill)
    _override_start_url(skill, args.start_url)
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
        async with launch_page(channel=args.channel, profile_dir=args.profile or None) as page:
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
                async with launch_page(channel=args.channel, profile_dir=args.profile or None) as case_page:
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


def _override_start_url(skill, start_url: str) -> None:
    """命令行显式给了 --start-url，就覆盖技能里的起始页。

    编译产物里的起始页是录制时的字面量。跑批时换环境（测试站/改版页）
    却改不动它，等于 --start-url 形同虚设 —— 所以这里直接覆盖。
    """
    if not start_url:
        return
    for precondition in skill.preconditions:
        if precondition.kind == "on_page":
            precondition.locator_or_path = start_url


def cmd_run_golden(args: argparse.Namespace) -> int:
    store = SkillStore(args.store)
    skill = load_skill(args.skill)
    _override_start_url(skill, args.start_url)
    cases = store.cases(skill.skill_id)
    if not cases:
        print("Golden Set 是空的，先 golden add")
        return 1

    # CLI 域不需要浏览器：同一套基准集与闸，只是执行器换了
    if skill.domain == "cli":
        from .cli_domain import make_runner

        runner = make_runner(cwd=args.cwd or None, timeout_s=args.timeout)

        async def run_cli() -> list[dict[str, Any]]:
            book = SignalBook(store.root, threshold=args.threshold)
            results: list[dict[str, Any]] = []
            escalated: list[Any] = []
            for case in cases:
                run = await runner(skill, dict(case.params))
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

    else:
        try:
            from .executor.tabbed import launch_tabbed as launch_page
        except Exception as exc:  # noqa: BLE001
            print(f"需要 playwright：{type(exc).__name__}（先跑 scripts/bootstrap_playwright.py）")
            return 1

        async def run_cli() -> list[dict[str, Any]]:
            from .executor.driver import execute

            book = SignalBook(store.root, threshold=args.threshold)
            results: list[dict[str, Any]] = []
            escalated: list[Any] = []
            for case in cases:
                params = dict(case.params)
                params.setdefault("start_url", args.start_url)
                async with launch_page(channel=args.channel, profile_dir=args.profile or None) as page:
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

    run_all = run_cli

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
    p.add_argument("--serve", action="store_true",
                   help="起本地网页做人审（纯标准库，只绑 127.0.0.1）")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="--serve 时不自动开浏览器")
    p.add_argument("--export-html", default="",
                   help="把表单导出成 HTML（只读，不能提交）")
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
    p.add_argument("--cwd", default="", help="cli 域：命令的工作目录")
    p.add_argument("--timeout", type=int, default=60, help="cli 域：单条命令超时秒数")
    p.add_argument("--profile", default="", help="持久化浏览器配置目录（要登录的系统用）")
    p.add_argument("--threshold", type=int, default=5,
                   help="technical 探针连续失败多少次升格告警")
    p.set_defaults(func=cmd_run_golden)

    p = sub.add_parser("doctor", help="环境自检：Python / playwright / 浏览器 / 目录")
    p.add_argument("--channel", default="chrome", help="用哪个浏览器内核（chrome / msedge）")
    p.add_argument("--shallow", action="store_true", help="不真开浏览器（快，但少验一环）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="跑一次技能（按 skill.domain 自动分派 browser / cli）")
    p.add_argument("--skill", required=True)
    p.add_argument("--params", default="", help="入参 JSON")
    p.add_argument("--start-url", default="", help="browser 域：覆盖起始页")
    p.add_argument("--channel", default="chrome")
    p.add_argument("--run-id", default="")
    p.add_argument("--profile", default="", help="持久化浏览器配置目录（要登录的系统用）")
    p.add_argument("--cwd", default="", help="cli 域：命令的工作目录")
    p.add_argument("--timeout", type=int, default=60, help="cli 域：单条命令超时秒数")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("flow", help="多技能编排：上一步的产出当下一步的入参")
    p.add_argument("action", choices=["run", "validate"])
    p.add_argument("--flow", required=True, help="编排文件 JSON")
    p.add_argument("--store", required=True)
    p.add_argument("--params", default="", help="flow 级参数 JSON")
    p.add_argument("--out", default="", help="run 用：报告输出路径")
    p.add_argument("--run-id", default="", help="run 用：整条流的运行标识")
    p.add_argument("--interval-ms", type=int, default=0)
    p.add_argument("--channel", default="chrome")
    p.set_defaults(func=cmd_flow)

    p = sub.add_parser("canary", help="灰度发布：候选版本先接一小部分真实请求")
    p.add_argument("action", choices=["start", "status", "promote", "abort"])
    p.add_argument("--store", required=True)
    p.add_argument("--skill-id", required=True)
    p.add_argument("--candidate", default="", help="start 用：候选技能 json 路径")
    p.add_argument("--weight", type=float, default=0.1, help="候选接多少比例（0~1）")
    p.add_argument("--mode", choices=["item", "param", "segment"], default="item",
                   help="分流维度：item=按单据 / param=按参数（同一用户黏住）/ segment=只放行白名单")
    p.add_argument("--field", default="", help="--mode param/segment 用：看哪个参数，如 客户编号")
    p.add_argument("--segments", default="", help="--mode segment 用：放行哪些值，逗号分隔")
    p.add_argument("--min-samples", type=int, default=20, help="候选满多少条才给结论")
    p.add_argument("--scores", default="", help="promote 用：过发布闸的 scores.json")
    p.add_argument("--force", action="store_true", help="无视样本量/结论强行提升")
    p.set_defaults(func=cmd_canary)

    p = sub.add_parser("batch", help="批量执行一批单据（失败隔离 + 断点续跑，需要真浏览器）")
    p.add_argument("--store", required=True)
    p.add_argument("--skill", required=True)
    p.add_argument("--cases", required=True,
                   help="清单 JSON：[{item_id, params}] 或 {items: [...]}")
    p.add_argument("--out", required=True, help="逐条报告输出路径")
    p.add_argument("--start-url", default="", help="覆盖技能里的起始页")
    p.add_argument("--limit", type=int, default=0, help="本次最多跑几条（0=不限）")
    p.add_argument("--interval-ms", type=int, default=0, help="每条之间停多少毫秒")
    p.add_argument("--stop-after-failures", type=int, default=0, help="失败到 N 条就停（0=不停）")
    p.add_argument("--resume", action="store_true", help="跳过上次已成功的条目")
    p.add_argument("--cwd", default="", help="cli 域：命令的工作目录")
    p.add_argument("--timeout", type=int, default=60, help="cli 域：单条命令超时秒数")
    p.add_argument("--profile", default="", help="持久化浏览器配置目录（要登录的系统用）")
    p.add_argument("--canary", action="store_true",
                   help="按灰度权重把条目路由到候选版本（需先 canary start）")
    p.add_argument("--channel", default="chrome")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("record", help="打开浏览器录一遍演示（需要 playwright）")
    p.add_argument("--url", required=True, help="演示开始时的页面地址")
    p.add_argument("--out", required=True, help="Trace.json 输出路径")
    p.add_argument("--skill-id", default="demo")
    p.add_argument("--settle", type=int, default=600, help="每个动作后等页面稳定的毫秒数")
    p.add_argument("--channel", default="chrome")
    p.add_argument("--profile", default="", help="持久化浏览器配置目录（要登录的系统用：登录一次，以后都带这个身份）")
    p.add_argument("--headless", action="store_true", help="无头模式（一般人不用开）")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("heal", help="失败 → 自动打补丁 → 过闸发布（需要真浏览器）")
    p.add_argument("--store", required=True)
    p.add_argument("--skill", required=True)
    p.add_argument("--start-url", required=True)
    p.add_argument(
        "--params", default="", help="这次失败运行的入参 JSON；默认用第一条 Golden 用例"
    )
    p.add_argument("--channel", default="chrome")
    p.add_argument("--dry-run", action="store_true", help="只试补丁、不落库")
    p.add_argument("--profile", default="", help="持久化浏览器配置目录（要登录的系统用）")
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
