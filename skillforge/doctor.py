"""环境自检：一条命令看清"到底是哪一环没通"。

为什么值得单独做：新手卡住时看到的是几十行 traceback，看不出是 Python 没装、
playwright 没装、浏览器起不来、还是路径不对。这里把每一环拆开单独验，
每条给一句"该怎么办"。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


def _check_python() -> Check:
    version = ".".join(str(x) for x in sys.version_info[:3])
    ok = sys.version_info >= (3, 10)
    return Check(
        "Python 版本",
        OK if ok else FAIL,
        f"{version}  ({sys.executable})",
        "" if ok else "需要 3.10 或更高。装：winget install Python.Python.3.12",
    )


def _check_package() -> Check:
    try:
        import skillforge

        return Check("skillforge 包", OK, f"{skillforge.__version__}  ({Path(skillforge.__file__).parent})")
    except Exception as exc:  # noqa: BLE001
        return Check("skillforge 包", FAIL, f"{type(exc).__name__}: {exc}", "在仓库根目录下运行")


def _find_playwright() -> tuple[bool, str]:
    spec = importlib.util.find_spec("playwright")
    if spec is None:
        return False, "没装"
    origin = spec.origin or ""
    return True, origin


def _check_playwright() -> Check:
    found, where = _find_playwright()
    if not found:
        return Check(
            "playwright（控制浏览器用）",
            FAIL,
            where,
            '装：pip install "skillforge-engine[browser]"  或者设置 PYTHONPATH 指向 vendor\\',
        )
    in_vendor = "vendor" in where.replace("/", "\\")
    return Check(
        "playwright（控制浏览器用）",
        OK,
        f"{'仓库 vendor 里' if in_vendor else where}",
    )


CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _check_browser_binary() -> Check:
    for path in CHROME_PATHS:
        if Path(path).exists():
            return Check("Chrome / Edge", OK, path)
    return Check(
        "Chrome / Edge",
        WARN,
        "常见路径没找到",
        "装个 Chrome，或用 --channel msedge 试试",
    )


async def _try_launch(channel: str) -> Check:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        return Check("真开一次浏览器", FAIL, f"导入失败：{type(exc).__name__}", "先解决上一条")

    from .pwtmp import ensure_playwright_temp

    moved = None
    try:
        moved = ensure_playwright_temp()
    except RuntimeError as exc:
        return Check("真开一次浏览器", FAIL, str(exc)[:200], "手动设 SKILLFORGE_PWTMP 到可写目录")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel=channel, headless=True)
            page = await browser.new_page()
            await page.goto("about:blank")
            await browser.close()
        detail = f"channel={channel}  临时目录={moved}"
        return Check("真开一次浏览器", OK, detail)
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {str(exc)[:160]}"
        lowered = detail.lower()
        if "mkdtemp" in lowered or "eperm" in lowered:
            hint = "临时目录不可写。设一个可写目录：$env:TMP='D:\\some\\writable\\dir'"
        elif "pipe" in lowered or "winerror 5" in lowered:
            hint = "当前是受限环境（沙箱/无桌面），换普通终端再试"
        else:
            hint = "检查 Chrome 是否可用；也可以试 --channel msedge"
        return Check("真开一次浏览器", FAIL, detail, hint)


def _check_workdir() -> Check:
    cwd = Path.cwd()
    probe = cwd / ".tmp"
    try:
        probe.mkdir(exist_ok=True)
        marker = probe / ".write-probe"
        marker.write_text("x", encoding="utf-8")
        marker.unlink(missing_ok=True)
        return Check("当前目录可写", OK, str(cwd))
    except Exception as exc:  # noqa: BLE001
        return Check("当前目录可写", FAIL, f"{type(exc).__name__}: {exc}", "换个能写的目录")


def _deep_launch_check(channel: str) -> Check:
    """真开一次浏览器。

    用自建事件循环 + 静默异常处理器：playwright 起不来时会往 stderr 抛
    "Task exception was never retrieved" 一串内部 traceback，会把自检表格冲得看不清。
    我们要的是"哪一环没通 + 怎么办"，不是它的栈。
    """
    import asyncio

    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda _loop, _ctx: None)
    try:
        return loop.run_until_complete(_try_launch(channel))
    finally:
        loop.close()


def run_checks(*, channel: str = "chrome", deep: bool = True) -> list[Check]:
    checks = [
        _check_python(),
        _check_package(),
        _check_playwright(),
        _check_browser_binary(),
        _check_workdir(),
    ]
    if deep:
        checks.append(_deep_launch_check(channel))
    return checks


SYMBOL = {OK: "[x]", WARN: "[!]", FAIL: "[ ]"}

HEADER = (
    "SkillForge 环境自检\n"
    "（下面每一行都是**程序打印的**，不用照着敲；要敲的命令都带 skillforge 前缀）"
)


def format_checks(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        lines.append(f"  {SYMBOL[check.status]} {check.name}")
        if check.detail:
            lines.append(f"        {check.detail}")
        if check.hint and check.status != OK:
            lines.append(f"        → {check.hint}")
    return "\n".join(lines)


def summary_line(checks: list[Check]) -> str:
    """一句话结论。用户不需要数方括号，看这一行就够。"""
    total = len(checks)
    passed = sum(1 for c in checks if c.status == OK)
    failed = [c.name for c in checks if c.status == FAIL]
    warned = [c.name for c in checks if c.status == WARN]

    if not failed and not warned:
        return f"结果：{total} 项全部通过 —— 可以开始录演示了"
    if not failed:
        return f"结果：{total} 项里 {passed} 项通过，{len(warned)} 项存疑（{'、'.join(warned)}）—— 可以先跑，注意着来"
    return (
        f"结果：{total} 项里 {passed} 项通过，{len(failed)} 项没通"
        f"（{'、'.join(failed)}）—— 先把这几项弄通"
    )


def worst(checks: list[Check]) -> str:
    if any(c.status == FAIL for c in checks):
        return FAIL
    if any(c.status == WARN for c in checks):
        return WARN
    return OK


__all__ = ["Check", "format_checks", "run_checks", "worst", "FAIL", "OK", "WARN"]
