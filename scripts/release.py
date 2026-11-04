"""发版助手：判断这次改动要不要发版，并按规范 bump 版本。

为什么要它：`git push` 只更新源码，**不会**更新别人下载的 wheel ——
那个只有打 tag 才会重建。忘了打 tag 的症状很隐蔽：代码是对的，下载物是旧的，
谁装谁踩坑（v0.6.1 就踩过：Release 里不含 cli_domain.py）。

用法：
    python scripts/release.py                 # 只看要不要发版
    python scripts/release.py --apply         # 需要发版就改版本号
    python scripts/release.py --apply --bump minor
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "skillforge" / "__init__.py"

# 这些路径一变，别人装到的行为就可能变 → 必须发版
CODE_PREFIXES = ("skillforge/",)
CODE_FILES = ("pyproject.toml",)

# 这些变了不影响装到的东西 → 不用发版
DOC_ONLY_PREFIXES = (
    "docs/",
    "examples/",
    "scripts/",
    ".github/",
    "tests/",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "push.cmd",
    ".gitignore",
    ".gitattributes",
)


def read_version(init_path: Path = INIT) -> str:
    match = re.search(r'__version__ = "([^"]+)"', init_path.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"{init_path} 里找不到 __version__")
    return match.group(1)


def write_version(new: str, init_path: Path = INIT) -> None:
    text = init_path.read_text(encoding="utf-8")
    init_path.write_text(
        re.sub(r'__version__ = "[^"]+"', f'__version__ = "{new}"', text, count=1),
        encoding="utf-8",
    )


def needs_release(paths: list[str]) -> tuple[bool, list[str]]:
    """有包代码改了就要发版；纯文档不用。返回 (是否发版, 触发文件)。"""
    triggers = [
        p for p in paths
        if p.startswith(CODE_PREFIXES) or p in CODE_FILES
    ]
    return bool(triggers), sorted(triggers)


def next_version(current: str, kind: str) -> str:
    major, minor, patch = (int(x) for x in current.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"未知的 bump 类型：{kind}")


def suggest_bump(messages: list[str], paths: list[str]) -> str:
    """有 `feat` 提交就算 minor，否则 patch。

    判断只看 commit message —— 别想着从路径猜"是不是新模块"，那分不清新文件和老文件
    （试过：`skillforge/cli.py` 一改就被误判成新功能）。所以提交信息里要写清 feat / fix。
    """
    joined = " ".join(messages)
    if re.search(r"^\s*feat(\(|:)", joined, re.MULTILINE):
        return "minor"
    return "patch"


def decide(current: str, paths: list[str], messages: list[str], bump: str = "auto") -> dict:
    required, triggers = needs_release(paths)
    if not required:
        return {
            "needs_release": False,
            "trigger": [],
            "bump": "none",
            "current": current,
            "next": current,
            "reason": "改动不涉及包代码（文档/示例/CI/测试），别人装到的行为没变",
        }
    kind = suggest_bump(messages, paths) if bump == "auto" else bump
    return {
        "needs_release": True,
        "trigger": triggers,
        "bump": kind,
        "current": current,
        "next": next_version(current, kind),
        "reason": f"包代码变了（{len(triggers)} 个文件），需要重新出包",
    }


# --------------------------------------------------------------------------- #
# git 交互（只读）
# --------------------------------------------------------------------------- #
def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{proc.stderr.strip()[:200]}")
    return proc.stdout


def last_tag() -> str:
    tags = [t for t in _git("tag", "--list").splitlines() if t.strip()]
    if not tags:
        return ""
    # 按语义版本排序，不用 git 的字典序
    def key(name: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", name)) or (0,)

    return sorted(tags, key=key)[-1]


def changed_since(tag: str) -> list[str]:
    """已提交的改动 + 工作区改动（含未跟踪的新文件）。

    只看 `git diff` 会漏掉刚写还没提交的文件 —— 那样工具会告诉你"不用发版"，
    而你正在加一个新模块。这个坑踩过一次。
    """
    rev = f"{tag}..HEAD" if tag else "HEAD"
    committed = [line.strip() for line in _git("diff", "--name-only", rev).splitlines()]
    dirty = [
        line[3:].strip()
        for line in _git("status", "--porcelain").splitlines()
        if len(line) > 3
    ]
    return sorted({path for path in committed + dirty if path})


def commit_messages_since(tag: str) -> list[str]:
    rev = f"{tag}..HEAD" if tag else "HEAD"
    return [line for line in _git("log", "--format=%s", rev).splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="判断要不要发版，并按规范 bump")
    parser.add_argument("--since", default="", help="从哪个 tag 起算（默认最近一个）")
    parser.add_argument("--bump", choices=["auto", "major", "minor", "patch"], default="auto")
    parser.add_argument("--apply", action="store_true", help="真的改 __version__")
    args = parser.parse_args(argv)

    current = read_version()
    tag = args.since or last_tag()
    paths = changed_since(tag)
    messages = commit_messages_since(tag)

    verdict = decide(current, paths, messages, args.bump)
    print(f"当前版本 : {current}")
    print(f"起算 tag : {tag or '（无 tag）'}")
    print(f"改动文件 : {len(paths)} 个")
    for path in verdict["trigger"][:12]:
        print(f"  · {path}")
    if len(verdict["trigger"]) > 12:
        print(f"  · …… 还有 {len(verdict['trigger']) - 12} 个")

    if not verdict["needs_release"]:
        print(f"\n结论：**不用发版** —— {verdict['reason']}")
        print("      （源码/README 的更新靠 push 就够了，别为它打 tag）")
        return 0

    print(f"\n结论：**要发版**（{verdict['bump']}）→ {verdict['current']} ⇒ {verdict['next']}")
    if not args.apply:
        print("\n加 --apply 就改版本号；改完按下面三步发：")
    else:
        write_version(verdict["next"])
        print(f"\n已把 __version__ 改成 {verdict['next']}。接下来：")
    print(f"  git add -A && git commit -m 'release: v{verdict['next']}'")
    print(f"  git tag v{verdict['next']} && git push origin main && git push origin v{verdict['next']}")
    print("  （release.yml 会校验 tag 与版本号一致 → 打包 → 建 Release）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
