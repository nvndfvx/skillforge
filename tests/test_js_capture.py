"""注入脚本的语法校验。

为什么必须有这个测试：`js_capture.py` 里的 JS 是从第一版起就带着语法错误的
（`"__VERSION__"` 被替换成 `""0.3.0""`），而其余离线测试只检查"字符串里有 frame_path"，
**永远发现不了语法错**。只有真浏览器 evaluate 才会报 SyntaxError。

用 node --check 做纯语法校验，离线可跑。
    python tests/test_js_capture.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _helpers import temp_dir  # noqa: E402

from skillforge.observer.js_capture import capture_script  # noqa: E402

_NODE_CANDIDATES = (
    os.environ.get("DSH_NODE"),
    r"D:\dsh-home\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe",
)


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for cand in _NODE_CANDIDATES:
        if cand and Path(cand).exists():
            return cand
    return None


def test_version_is_baked_in_without_double_quotes():
    script = capture_script("1.2.3")
    assert 'captured_version: "1.2.3"' in script, "版本号必须带引号地嵌进去"
    assert '""1.2.3""' not in script, "出现了双引号嵌套 → JS 语法错误"
    assert "__VERSION__" not in script, "占位符没被替换"


def test_no_leftover_placeholders():
    script = capture_script("9.9.9")
    for token in ("__VERSION__", "undefined_placeholder"):
        assert token not in script


def test_capture_script_is_valid_js():
    node = find_node()
    if node is None:
        print("   (skip) 未找到 node，无法做语法校验")
        return

    script = capture_script("1.2.3")
    with temp_dir() as tmp:
        path = Path(tmp) / "capture.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, timeout=60
        )
    assert proc.returncode == 0, f"JS 语法错误：\n{proc.stderr}"


def test_collect_candidates_present():
    script = capture_script("1.0.0")
    for needle in (
        "__skillforgeTextDigest",
        "__skillforgeFormSnapshot",
        "__skillforgeCollectCandidates",
        "__skillforgeLog",
    ):
        assert needle in script, needle


def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_")]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
