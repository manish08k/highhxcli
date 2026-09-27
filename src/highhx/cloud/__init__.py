"""HighhX platform: accounts, plans and the cloud services behind the CLI.

The CLI is the developer's interface; the HighhX platform (see ``server/``)
provides accounts, authentication, Pro subscriptions, AI access, usage and
agent-session sync. Every non-AI command keeps working without an account.
"""

from highhx.cloud.account import Account, CloudAccount
from highhx.cloud.plans import PLANS, Plan, plan_for

__all__ = ["PLANS", "Account", "CloudAccount", "Plan", "plan_for"]
