"""SkillForge —— 观察 → 蒸馏 → 沙箱执行 → 异常自愈（纵向调优）。

核心层零第三方依赖（纯 stdlib），保证「今天就能跑、能测」；
只有真正控制浏览器的层（executor/playwright_page.py）需要 playwright。
"""

__version__ = "0.7.1"
