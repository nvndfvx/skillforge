"""离线测试：发版助手（判断要不要发版 / 版本号怎么走）。

    python tests/test_release_tool.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

try:
    import release  # noqa: E402
except ImportError:  # scripts/ 不随包分发（CI 的「装完再跑」那一步就没有它）
    release = None  # type: ignore[assignment]


def test_doc_only_change_does_not_need_release():
    """只改文档就别打 tag —— 源码 push 已经生效，打 tag 只会多一个空版本。"""
    required, triggers = release.needs_release(["README.md", "docs/usage.md", "CHANGELOG.md"])
    assert required is False
    assert triggers == []


def test_package_code_change_needs_release():
    required, triggers = release.needs_release(["skillforge/cli.py", "README.md"])
    assert required is True
    assert triggers == ["skillforge/cli.py"]


def test_pyproject_change_needs_release():
    """改 pyproject 会改打包元数据（依赖、入口点），必须重新出包。"""
    required, triggers = release.needs_release(["pyproject.toml"])
    assert required is True
    assert triggers == ["pyproject.toml"]


def test_tests_and_ci_alone_do_not_need_release():
    required, _ = release.needs_release(
        ["tests/test_cli.py", ".github/workflows/ci.yml", "scripts/release.py"]
    )
    assert required is False


def test_latest_tag_selection():
    # 确保按语义版本排序（0.10.0 要大于 0.9.0，字典序会排错）
    versions = ["v0.6.0", "v0.6.1", "v0.7.0"]

    def key(name):
        import re

        return tuple(int(x) for x in re.findall(r"\d+", name)) or (0,)

    assert sorted(versions, key=key)[-1] == "v0.7.0"
    assert sorted(["v0.9.0", "v0.10.0"], key=key)[-1] == "v0.10.0"


def test_next_version():
    assert release.next_version("0.7.0", "patch") == "0.7.1"
    assert release.next_version("0.7.0", "minor") == "0.7.1".replace("0.7.1", "0.8.0")
    assert release.next_version("0.7.0", "major") == "1.0.0"
    assert release.next_version("0.9.9", "patch") == "0.9.10"


def test_suggest_bump_prefers_minor_for_features():
    assert release.suggest_bump(["feat: 加了 CLI 域"], ["skillforge/cli_domain.py"]) == "minor"
    assert release.suggest_bump(["fix: 修乱码"], ["skillforge/cli_domain.py"]) == "patch"
    assert release.suggest_bump(["chore: 整理"], ["skillforge/cli.py"]) == "patch"
    # 不带 feat 的提交一律按 patch —— 判断只看提交信息，不从路径猜
    assert release.suggest_bump(["随便写"], ["skillforge/webui.py"]) == "patch"


def test_decide_doc_only():
    verdict = release.decide("0.7.0", ["README.md"], ["docs: 改 README"])
    assert verdict["needs_release"] is False
    assert verdict["next"] == "0.7.0"
    assert verdict["bump"] == "none"


def test_decide_with_code_change():
    verdict = release.decide("0.7.0", ["skillforge/flow.py"], ["feat: 并行分支"])
    assert verdict["needs_release"] is True
    assert verdict["next"] == "0.8.0"


def test_decide_respects_explicit_bump():
    verdict = release.decide(
        "0.7.0", ["skillforge/flow.py"], ["feat: x"], bump="patch"
    )
    assert verdict["next"] == "0.7.1"


def test_version_roundtrip_on_real_file():
    """在真实 __init__.py 上读一次，确认单一来源可用（不改文件）。"""
    version = release.read_version()
    assert version.count(".") == 2, version
    assert all(part.isdigit() for part in version.split(".")), version


def main() -> int:
    if release is None:
        print("SKIP scripts/release.py 不在（scripts/ 不随包分发）—— 只在源码树里跑")
        return 0

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
