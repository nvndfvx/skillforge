"""一次跑完所有离线测试。

    python tests/run_all.py

不含真浏览器测试（test_real_browser.py 在没有浏览器时会自动 SKIP，
在沙箱里也会 SKIP，所以一并跑不会失败）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


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
