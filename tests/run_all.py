"""一次跑完所有离线测试。

    python tests/run_all.py

不含真浏览器测试（test_real_browser.py 在没有浏览器时会自动 SKIP，
在沙箱里也会 SKIP，所以一并跑不会失败）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 父进程自己也要 UTF-8：Windows CI 的 stdout 是 cp1252，
# 打印「全部通过」这种中文会直接抛 UnicodeEncodeError，把成功伪装成失败。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def _child_env() -> dict[str, str]:
    """强制子进程用 UTF-8。

    Windows CI 的控制台是 cp1252，打印中文会抛 UnicodeEncodeError ——
    这会让「本该 SKIP 或本该失败」的测试以编码错误的形式崩掉，把真正的失败信息盖住。
    实测：不设这个，test_real_browser 在 Windows runner 上直接 UnicodeEncodeError 退出。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def main() -> int:
    files = sorted(p for p in HERE.glob("test_*.py") if p.name != "run_all.py")
    failed: list[str] = []
    for path in files:
        proc = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_child_env(),
        )
        lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
        summary = lines[-1] if lines else "(无输出)"
        mark = "OK  " if proc.returncode == 0 else "FAIL"
        print(f"{mark} {path.name:24s} {summary}")
        if proc.returncode != 0:
            failed.append(path.name)
            tail = "\n".join(lines[-25:])
            if tail:
                print("\n".join("     " + ln for ln in tail.splitlines()))
            if proc.stderr.strip():
                print("     stderr:", proc.stderr.strip()[-1500:])

    print()
    if failed:
        print(f"{len(failed)} 个测试文件失败：{failed}")
        return 1
    print(f"全部通过（{len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
