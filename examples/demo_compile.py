"""端到端示例（不需要浏览器）：两遍演示 → 对齐 → 编译出 Skill 草稿。

跑法：
    python examples/demo_compile.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，打印中文 JSON 会乱码；统一成 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from skillforge.compiler.distiller import distill  # noqa: E402
from skillforge.observer.aligner import align_actions  # noqa: E402
from skillforge.schema.types import RawAction  # noqa: E402

BASE = "http://erp.local/expense/new"
SUCCESS = "http://erp.local/expense/success"


def act(
    idx: int,
    action: str,
    name: str = "",
    value: str | None = None,
    role: str = "",
    url: str = "",
    before: str = "",
    after: str = "",
    pause: int = 0,
) -> RawAction:
    return RawAction(
        seq=idx,
        action=action,
        name=name,
        value=value,
        role=role,
        url=url,
        before_text=before,
        after_text=after,
        pause_before_ms=pause,
    )


def demo(customer: str, amount: str) -> list[RawAction]:
    return [
        act(0, "goto", role="document", url=BASE),
        act(1, "fill", "客户名称", customer, role="textbox", url=BASE),
        act(2, "fill", "金额", amount, role="textbox", url=BASE, pause=4200),
        act(
            3,
            "click",
            "提交",
            role="button",
            url=SUCCESS,
            before="报销单填写中",
            after="提交成功 单号 R-20240115 已创建",
        ),
    ]


def main() -> int:
    a, b = demo("张三", "500"), demo("李四", "800")
    report = align_actions(a, b)
    print("=== 对齐报告 ===")
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))

    result = distill(report, a, skill_id="expense-create", name="报销单录入")
    print("\n=== Skill 草稿 ===")
    print(json.dumps(result.skill.to_dict(), ensure_ascii=False, indent=2))

    print("\n=== 结构错误（draft 允许存在） ===")
    for e in result.errors:
        print(" E:", e)

    print("\n=== 发布阻断（必须清零才能上线） ===")
    for w in result.skill.publish_blockers():
        print(" B:", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
