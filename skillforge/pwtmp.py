"""给 playwright 找一个**可写**的临时目录。

为什么需要：playwright 启动浏览器前会在 `TMP`/`TEMP` 下建
`playwright-artifacts-XXXXX` 目录。系统 Temp 不可写时（ACL 异常、受控文件夹访问、
磁盘配额、或是被安全软件锁住），启动会直接失败：

    BrowserType.launch: EPERM: operation not permitted, mkdtemp 'C:\\Users\\...\\Temp\\...'

这个错误看起来像"没权限开浏览器"，实际只是临时目录用不了 —— 换一个就能跑。
所以这里先探一次，能写就不动，不能写就退到工作区的 `.tmp` 下。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _usable(path: str | Path) -> bool:
    try:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        probe = Path(tempfile.mkdtemp(prefix="sf-probe-", dir=str(target)))
        probe.rmdir()
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_playwright_temp(*, quiet: bool = True) -> str:
    """把 TMP/TEMP 指到一个**确定可写**的目录，返回该路径。

    为什么不是"系统 Temp 能写就不动"：playwright 建临时目录的是它的
    **node 子进程**，不是 Python 自己。Python 探测成功 ≠ node 能写 ——
    实测过这种情况（Python 能 mkdtemp，node 报 EPERM）。

    所以这里不做探测式判断，直接用一个我们自己的目录：
    优先 `$SKILLFORGE_PWTMP`，否则工作区下的 `.tmp/pw`。
    """
    override = os.environ.get("SKILLFORGE_PWTMP")
    target = Path(override) if override else Path.cwd() / ".tmp" / "pw"

    if not _usable(target):
        fallback = Path(tempfile.gettempdir()) / "skillforge-pw"
        if _usable(fallback):
            target = fallback
        else:
            raise RuntimeError(
                f"找不到可写的临时目录（试过 {target} 和 {fallback}）。\n"
                "手动指定一个再重试：$env:SKILLFORGE_PWTMP='D:\\some\\writable\\dir'"
            )

    os.environ["TMP"] = str(target)
    os.environ["TEMP"] = str(target)
    if not quiet:
        print(f"[playwright 临时目录] {target}")
    return str(target)


__all__ = ["ensure_playwright_temp"]
