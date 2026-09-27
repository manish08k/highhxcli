"""HighhX actions: the deterministic automation engine shared by Free and Pro.

    intent / workflow step / agent proposal → action (catalog) → policy (risk, approval)
      → gate (confirmation bound to the action, audit) → executor (timeout, cancellation,
        idempotent retries) → verification → events + history

See :mod:`highhx.actions.catalog`, :mod:`highhx.actions.executor` and :mod:`highhx.actions.policy`.
"""
