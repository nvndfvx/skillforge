"""首次跑通：一条命令把 SkillForge 从环境自检走到"发布第一个技能"。

用法（在普通 PowerShell 窗口，仓库根目录下）：

    python scripts\\first_run.py
    python scripts\\first_run.py --only doctor   # 只跑某一环
    python scripts\\first_run.py --list          # 只看有哪些步骤

为什么要这个脚本：新手卡住的往往不是技术问题，而是"哪些行是命令、哪些行是输出"。
把这些步骤固化成一个程序，就不用再抄命令了。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "form_v1.html"

# 每一步：(键, 标题, 给你的指令, 命令参数)
STEPS: list[tuple[str, str, str, list[str]]] = [
    (
        "doctor",
        "① 环境自检",
        "不用你操作，看它自己跑。",
        ["doctor"],
    ),
    (
        "record-a",
        "② 录第一遍演示（会弹出 Chrome）",
        "浏览器里只做三件事：客户名称填『张三』、金额填『500』、点『提交』，\n"
        "    然后回到这个窗口按回车。别的都不要点。",
        ["record", "--url", "{{URL}}", "--out", "demos\\a.json", "--skill-id", "expense"],
    ),
    (
        "record-b",
        "③ 录第二遍（这次必须换参数）",
        "这次填『李四』和『800』，其余一样。点『提交』后回窗口按回车。\n"
        "    换参数是在告诉它『客户名和金额是变量』——不换的话会编出一个写死的假技能。",
        ["record", "--url", "{{URL}}", "--out", "demos\\b.json", "--skill-id", "expense"],
    ),
    (
        "compile",
        "④ 把两遍演示编成技能",
        "不用你操作。它会打印参数和步骤数。",
        [
            "compile",
            "--trace-a", "demos\\a.json", "--trace-b", "demos\\b.json",
            "--out", "skill.json", "--skill-id", "expense", "--name", "报销单录入",
        ],
    ),
    (
        "review",
        "⑤ 网页人审（会自动打开浏览器）",
        "页面里：勾『这串步骤就是我要的流程』→ 两条断言保持『对』→\n"
        "    precheck 输入框不动 → 点『提交人审结果』。",
        ["review", "--skill", "skill.json", "--serve"],
    ),
    (
        "golden",
        "⑥ 建回归基准集",
        "不用你操作。会生成 12 条候选用例。",
        ["golden", "suggest", "--store", "store", "--skill-id", "expense",
         "--skill", "skill.json", "--limit", "12", "--apply"],
    ),
    (
        "run-golden",
        "⑦ 真浏览器跑批这 12 条",
        "不用你操作，大约 1-2 分钟。",
        ["run-golden", "--store", "store", "--skill", "skill.json",
         "--start-url", "{{URL}}", "--out", "scores.json"],
    ),
    (
        "release",
        "⑧ 过闸发布",
        "不用你操作。发布成功会打印『已发布 expense v1.0.0』。",
        ["release", "--store", "store", "--skill", "skill.json", "--scores", "scores.json"],
    ),
    (
        "status",
        "⑨ 看最终状态",
        "不用你操作。",
        ["status", "--store", "store", "--skill-id", "expense"],
    ),
]


def page_url() -> str:
    """file:// + 原始路径。

    不用 Path.as_uri()：它会把中文百分号编码成 %E8%93%9D…，
    录进技能里的起始页会变成一串乱码，人看不懂也不好排查。Chrome 认原始中文。
    """
    return "file:///" + FIXTURE.as_posix().lstrip("/")


def render(args: list[str]) -> list[str]:
    url = page_url()
    return [a.replace("{{URL}}", url) for a in args]


def banner(index: int, total: int, title: str, note: str) -> None:
    print(flush=True)
    print("=" * 66, flush=True)
    print(f" 第 {index}/{total} 步 · {title}", flush=True)
    print("=" * 66, flush=True)
    if note:
        print(f"  你要做的：{note}", flush=True)
        print(flush=True)


def run_step(args: list[str], *, soft: bool = False) -> int:
    cmd = [sys.executable, "-m", "skillforge", *args]
    print(f"  $ skillforge {' '.join(args[:6])}{' …' if len(args) > 6 else ''}", flush=True)
    print(flush=True)
    code = subprocess.call(cmd, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "vendor")})
    if code != 0 and not soft:
        print(flush=True)
        print(f"  这一步失败了（退出码 {code}）。", flush=True)
        print("  把上面这一屏（从『第 N 步』到最后）整段发我，不要只发最后一行。", flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="首次跑通 SkillForge")
    parser.add_argument("--only", default="", help="只跑某一步（用 --list 看键名）")
    parser.add_argument("--from", dest="start_at", default="", help="从某一步开始")
    parser.add_argument("--list", action="store_true", help="只列出步骤")
    parser.add_argument("--yes", action="store_true", help="不逐段暂停确认")
    args = parser.parse_args(argv)

    if args.list:
        for key, title, _, _ in STEPS:
            print(f"  {key:12s} {title}")
        return 0

    if not FIXTURE.exists():
        print(f"找不到示例页面：{FIXTURE}")
        print("请确认你在仓库根目录下运行（skillforge 目录里应有 tests/fixtures/）。")
        return 1

    todo = STEPS
    if args.only:
        todo = [s for s in STEPS if s[0] == args.only]
        if not todo:
            print(f"没有这一步：{args.only}（用 --list 看可选值）")
            return 1
    elif args.start_at:
        keys = [s[0] for s in STEPS]
        if args.start_at not in keys:
            print(f"没有这一步：{args.start_at}")
            return 1
        todo = STEPS[keys.index(args.start_at):]

    print()
    print("SkillForge 首次跑通")
    print(f"  仓库：{ROOT}")
    print(f"  页面：{page_url()}")
    print(f"  共 {len(todo)} 步")

    for index, (key, title, note, raw) in enumerate(todo, start=1):
        banner(index, len(todo), title, note)
        if not args.yes and index > 1:
            try:
                input("  按回车开始这一步（Ctrl+C 退出）… ")
            except (EOFError, KeyboardInterrupt):
                print("\n  已退出。下次可以接着跑：")
                print(f"    python scripts\\first_run.py --from {key}")
                return 1

        code = run_step(render(raw))
        if code != 0:
            print()
            print(f"  停在『{key}』。修好后从这里继续：")
            print(f"    python scripts\\first_run.py --from {key}")
            return code

    print()
    print("=" * 66)
    print("  全部完成：你已经有一个过了回归闸的技能。")
    print("=" * 66)
    print("  之后每天用：")
    print("    .\\sf batch --store store --skill skill.json --cases cases.json --out report.json --resume")
    print("  页面改版了：")
    print("    .\\sf heal --store store --skill skill.json --start-url <地址>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
