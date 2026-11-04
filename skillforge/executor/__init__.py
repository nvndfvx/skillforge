"""executor 层对外出口。"""

from .adapter import PageAdapter
from .candidates import collect_locator_candidates
from .driver import RunResult, StepResult, execute, render, resolve_locator, run_step
from .idempotency import BLOCKED, PROCEED, SKIP, IdempotencyDecision, decide
from .validator import AssertionResult, evaluate

__all__ = [
    "BLOCKED",
    "PROCEED",
    "SKIP",
    "AssertionResult",
    "IdempotencyDecision",
    "PageAdapter",
    "RunResult",
    "StepResult",
    "collect_locator_candidates",
    "decide",
    "evaluate",
    "execute",
    "render",
    "resolve_locator",
    "run_step",
]
