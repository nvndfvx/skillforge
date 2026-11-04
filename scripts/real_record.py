"""录真实系统的向导：登录一次 → 录两遍 → 自动给出可读摘要。

用法（普通 PowerShell，仓库根目录）：

    python scripts\\real_record.py --url "https://你的系统/页面" --skill-id vamaudit

三步：
  ① 只登录（不录）—— 让浏览器配置里留下登录态
  ② 录第一遍 —— 正常做完你那件事
  ③ 录第二遍 —— **换另一条记录**做同样的事

录完自动调用 inspect_trace 打印摘要 + 两遍差异，你把它发我就行。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "demos" / "real"
DEFAULT_PROFILE = Path("D:/sf-profile")


def call(args: list[str]) -> int:
    cmd = [sys.executable, "-m", "skillforge", *args]
    return subprocess.call(cmd, cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "vendor")})


def banner(index: int, title: str, note: str) -> None:
    print(flush=True)
    print("=" * 68, flush=True)
    print(f" {index}/3 · {title}", flush=True)
    print("=" * 68, flush=True)
    print(f" {note}", flush=True)
    print(flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="录真实系统（登录 + 两遍演示）")
    parser.add_argument("--url", required=True, help="要录的页面地址")
    parser.add_argument("--skill-id", required=True, help="技能 id，如 vamaudit")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE), help="浏览器配置目录（存登录态）")
    parser.add_argument("--channel", default="chrome")
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    v1 = OUT_DIR / "v1.json"
    v2 = OUT_DIR / "v2.json"
    login = OUT_DIR / "login.json"

    common = ["--profile", args.profile, "--channel", args.channel]

    banner(1, "只登录（这一遍录的东西会丢掉）",
           "浏览器打开后：在登录页登录进去，等页面加载到你要的那个界面，回窗口按回车。")
    if call(["record", "--url", args.url, "--out", str(login), "--skill-id", args.skill_id, *common]) != 0:
        print("登录这一步失败了，先解决它。")
        return 1

    banner(2, "录第一遍",
           "已经带着登录态打开了。把你要自动化的那件事完整做一遍（点开 → 操作 → 提交），回窗口按回车。")
    if call(["record", "--url", args.url, "--out", str(v1), "--skill-id", args.skill_id, *common]) != 0:
        print("第一遍录制失败。")
        return 1

    banner(3, "录第二遍（必须换一条记录）",
           "同样的事再做一遍，但这回选**另一条任务/另一个对象**。回窗口按回车。")
    if call(["record", "--url", args.url, "--out", str(v2), "--skill-id", args.skill_id, *common]) != 0:
        print("第二遍录制失败。")
        return 1

    print(flush=True)
    print("=" * 68, flush=True)
    print(" 录完了。下面是自动摘要（把这个发我，我来判断关联断言该怎么定）", flush=True)
    print("=" * 68, flush=True)
    subprocess.call([sys.executable, str(ROOT / "scripts" / "inspect_trace.py"), str(v1),
                     "--compare", str(v2)], cwd=ROOT)

    print(flush=True)
    print("下一步（我来做）：读这两份轨迹，定出「跨页关联断言」的正则和参数，写进技能。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
