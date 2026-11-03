"""compiler 层：把「对齐报告 + 原始轨迹」编译成 Skill 草稿。

输出永远是 `state="draft"`：断言全是 `llm_proposed`，必须人审两道勾才能变 reviewed。
"""
from .distiller import DistillResult, distill

__all__ = ["DistillResult", "distill"]
