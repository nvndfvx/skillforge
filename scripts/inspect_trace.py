"""读懂一份录制轨迹：它到底录到了什么。

为什么需要：录完的 JSON 有几千行，看不过来。这里把它压成人能读的摘要：
每步在哪个标签页、对什么元素做了什么、url 跳到哪、里面有哪些"像实体号的串"。

用法：
    python scripts\\inspect_trace.py demos\\real\\v1.json
    python scripts\\inspect_trace.py demos\\real\\v1.json --compare demos\\real\\v2.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# 真实系统里"业务实体号"常见的几种形态。宁可多抓，人工再看。
ID_PATTERNS: list[tuple[str, str]] = [
    ("带前缀-数字", r"\b[A-Z]{1,5}[-_]?\d{4,}\b"),
    ("UUID", r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    ("长数字", r"\b\d{6,}\b"),
]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def entity_candidates(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for label, pattern in ID_PATTERNS:
        found = sorted({m.group(0) for m in re.finditer(pattern, text or "")})
        if found:
            out[label] = found[:8]
    return out


def short(value: object, limit: int = 46) -> str:
    text = "" if value is None else str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def page_name(url: str) -> str:
    if not url:
        return "—"
    cleaned = url.split("?")[0]
    return cleaned.rsplit("/", 1)[-1] or cleaned


def describe(trace: dict) -> list[str]:
    actions = trace.get("actions", [])
    lines: list[str] = []
    lines.append(f"轨迹 {trace.get('trace_id', '?')}   开始 {trace.get('started_at', '?')}")
    lines.append(f"动作 {len(actions)} 个")

    tabs: dict[int, int] = {}
    for a in actions:
        tabs[int(a.get("tab", 0) or 0)] = tabs.get(int(a.get("tab", 0) or 0), 0) + 1
    if tabs:
        lines.append("标签页分布：" + "，".join(f"tab{t}={n}步" for t, n in sorted(tabs.items())))
    if any(t > 0 for t in tabs):
        lines.append("  → 有跨标签页操作（点开新页），关联断言必须锚在业务实体号上")
    else:
        lines.append("  → 全程单标签页")

    lines.append("")
    lines.append(f"{'序':>3} {'页':>3} {'动作':<10} {'元素':<28} {'值':<22} url")
    for a in actions:
        fp = a.get("fingerprint") or {}
        label = short(fp.get("accessible_name") or a.get("name") or "", 26)
        role = short(fp.get("role") or a.get("role") or "", 10)
        lines.append(
            f"{a.get('seq', '?'):>3} {a.get('tab', 0):>3} {short(a.get('action'), 10):<10} "
            f"{(role + ' ' + label).strip():<28} {short(a.get('value'), 22):<22} {page_name(a.get('url', ''))}"
        )

    # 把整页文本里像"实体号"的东西汇总出来
    blob = "\n".join(
        [str(a.get("before_text") or "") + "\n" + str(a.get("after_text") or "") for a in actions]
    )
    candidates = entity_candidates(blob)
    lines.append("")
    if candidates:
        lines.append("页面上像业务实体号的串（推断关联断言时要用）：")
        for label, found in candidates.items():
            lines.append(f"  {label}: {', '.join(found)}")
    else:
        lines.append("页面上没找到像实体号的串 —— 关联断言得靠人指定元素。")

    values = [a for a in actions if a.get("value") not in (None, "")]
    if values:
        lines.append("")
        lines.append(f"被填过值的元素 {len(values)} 个（这些通常是变量）：")
        for a in values:
            fp = a.get("fingerprint") or {}
            lines.append(f"  · {short(fp.get('accessible_name') or a.get('name'), 30)} = {short(a.get('value'), 30)}")

    warnings = (trace.get("meta") or {}).get("reconcile_warnings") or []
    if warnings:
        lines.append("")
        lines.append("录制警告：")
        for w in warnings[:5]:
            lines.append(f"  ! {short(w, 100)}")
    return lines


def compare(a_path: Path, b_path: Path) -> list[str]:
    """两遍对比：只在'换过参数'时才可能编出正确技能。"""
    a_actions = load(a_path).get("actions", [])
    b_actions = load(b_path).get("actions", [])
    lines = [f"两遍对比：{a_path.name}（{len(a_actions)} 步） vs {b_path.name}（{len(b_actions)} 步）"]

    if len(a_actions) != len(b_actions):
        lines.append(f"  ! 步数不同（{len(a_actions)} vs {len(b_actions)}）：多/少的动作会被当成差异，编译会报对齐问题")
    shared = min(len(a_actions), len(b_actions))
    diffs = 0
    for i in range(shared):
        a, b = a_actions[i], b_actions[i]
        for field in ("value", "tab"):
            if a.get(field) != b.get(field):
                diffs += 1
                lines.append(f"  第 {i + 1} 步 {field} 不同：{short(a.get(field), 30)} → {short(b.get(field), 30)}")
    if diffs == 0:
        lines.append("  ! 两遍完全一样 —— 会编出一个写死值的假技能，请重录第二遍并换一条任务")
    else:
        lines.append(f"  有 {diffs} 处不同，够编译器认出变量")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读录制轨迹")
    parser.add_argument("trace")
    parser.add_argument("--compare", default="", help="与另一份轨迹对比")
    args = parser.parse_args(argv)

    path = Path(args.trace)
    if not path.exists():
        print(f"找不到 {path}")
        return 1
    print("\n".join(describe(load(path))))
    if args.compare:
        other = Path(args.compare)
        if not other.exists():
            print(f"\n找不到 {other}")
            return 1
        print()
        print("\n".join(compare(path, other)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
