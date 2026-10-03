"""The computer-use agent loop: Planner → Worker → Observer → Verifier → Reflector, with bounded
recovery, checkpoints, trajectory memory, tool routing and optional specialists. Every action goes
through the one ActionExecutor. See docs/AGENT_LOOP.md."""

from highhx.agent.loop.loop import AgentLoop, resume
from highhx.agent.loop.model import AgentTask, Decision, LoopResult, PlanItem, Status, StepIntent
from highhx.agent.loop.planner import AgentPlanner, ModelPlanner, ResolverPlanner, ScriptedPlanner
from highhx.agent.loop.routing import Route, ToolRouter, default_router

__all__ = [
    "AgentLoop",
    "AgentPlanner",
    "AgentTask",
    "Decision",
    "LoopResult",
    "ModelPlanner",
    "PlanItem",
    "ResolverPlanner",
    "Route",
    "ScriptedPlanner",
    "Status",
    "StepIntent",
    "ToolRouter",
    "default_router",
    "resume",
]
