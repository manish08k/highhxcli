"""The set of tools offered to the model for a session."""

from __future__ import annotations

from collections.abc import Iterable

from highhx.agent.model.base import ToolSpec
from highhx.agent.tools.base import Tool


def builtin_tools() -> list[Tool]:
    from highhx.agent.planner import ProposePlanTool, UpdatePlanTool
    from highhx.agent.tools import commands, computer, devops, files, git, project

    return [
        # Understand
        project.ProjectOverviewTool(),
        project.ListFilesTool(),
        project.ReadFileTool(),
        project.SearchCodeTool(),
        git.GitStatusTool(),
        git.GitDiffTool(),
        git.GitLogTool(),
        devops.HistoryTool(),
        devops.DoctorTool(),
        devops.DiagnoseTool(),
        devops.DependenciesTool(),
        devops.SecurityScanTool(),
        devops.DeployTargetsTool(),
        devops.WorkflowsTool(),
        # Plan & remember
        ProposePlanTool(),
        UpdatePlanTool(),
        devops.RememberTool(),
        # Change & verify
        files.EditFileTool(),
        files.WriteFileTool(),
        files.DeleteFileTool(),
        commands.RunTestsTool(),
        commands.RunChecksTool(),
        commands.RunFixTool(),
        commands.BuildTool(),
        commands.InstallDependenciesTool(),
        commands.RunCommandTool(),
        devops.RunWorkflowTool(),
        devops.RepairTool(),
        git.GitCommitTool(),
        git.GitBranchTool(),
        git.GitPushTool(),
        devops.DeployTool(),
        devops.RollbackTool(),
        # Computer use
        computer.ComputerObserveTool(),
        computer.ComputerActTool(),
        computer.BrowserOpenTool(),
        computer.AppOpenTool(),
    ]


class ToolRegistry:
    """Tools by name, in a stable order (a deterministic tool list keeps prompt caches warm)."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if not tool.name:
                raise ValueError(f"{type(tool).__name__} has no name")
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name {tool.name!r}")
            self._tools[tool.name] = tool

    @classmethod
    def for_session(
        cls, *, features: frozenset[str], read_only: bool = False, initialized: bool = True, extra: Iterable[Tool] = ()
    ) -> ToolRegistry:
        """Built-in tools the account's plan allows (plus ``extra``), minus changes in read-only mode."""
        selected = []
        for tool in [*builtin_tools(), *extra]:
            if tool.feature is not None and tool.feature not in features:
                continue
            if read_only and tool.mutating:
                continue
            if tool.requires_project and not initialized:
                continue
            selected.append(tool)
        return cls(selected)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self._tools.values()]
