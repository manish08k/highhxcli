"""Risk for plans: the executor's five levels, and the three classes people read.

    safe        read-only or reversible-by-looking: open an app or site, search, list, git status, scroll
    controlled  changes something, locally and recoverably: create a file, run a program, type, send keys
    high        destructive or outward-facing: delete, overwrite, deploy, push, dangerous commands

The level comes from the same place the executor gets it — the action's risk floor
(``ActionSpec.risk`` / ``risk_for``) raised by the safety classifier — so a plan never shows a
lower risk than execution will apply. The class adds what the level alone does not say: a
low-risk *write* or *exec* is still "controlled".
"""

from __future__ import annotations

from highhx.actions.policy import Risk
from highhx.safety.actions import ActionKind

CHANGING_KINDS = frozenset(
    {ActionKind.WRITE_FILE, ActionKind.EXEC, ActionKind.UI_TYPE, ActionKind.UI_SELECT, ActionKind.GIT}
)


def risk_class(level: Risk, kind: ActionKind | None = None) -> str:
    if level >= Risk.HIGH:
        return "high"
    if level >= Risk.MEDIUM:
        return "controlled"
    if kind is not None and kind in CHANGING_KINDS and level >= Risk.LOW:
        return "controlled"
    return "safe"


def worst(classes: list[str]) -> str:
    order = ("safe", "controlled", "high")
    return max(classes, key=order.index) if classes else "safe"
