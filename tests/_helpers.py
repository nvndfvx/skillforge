"""测试公用小工具：把临时目录放在工作区里。

为什么不用 `tempfile.TemporaryDirectory`：
  1. 它内部用 `mkdtemp(dir, 0o700)` 建目录，这种目录在当前沙箱里**写不进去**
     （`PermissionError` WinError 5），而同样的路径用普通 `mkdir` 就正常；
  2. 沙箱也拒绝删除，退出时的 rmtree/rdir 会再抛一次错。
所以这里自己建普通目录、且退出时不清理。CI 上不该清理也不是问题。

另外默认的 `C:\\Users\\...\\Temp\\dsh-*` 也是被拒的，所以必须放在工作区。
"""
from __future__ import annotations

import contextlib
import sys
import uuid
from pathlib import Path
from typing import Iterator

# Windows 控制台默认不是 UTF-8（CI 上是 cp1252），打印中文会抛 UnicodeEncodeError。
# 导入本模块的测试自动获得 UTF-8 stdout。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
_TMP_ROOT = ROOT / ".tmp"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def temp_dir() -> Iterator[Path]:
    path = _TMP_ROOT / ("t" + uuid.uuid4().hex[:10])
    path.mkdir(parents=True, exist_ok=True)
    yield path


__all__ = ["ROOT", "temp_dir"]
