"""All built-in top-level commands."""

from __future__ import annotations

import click


def all_commands() -> list[click.Command]:
    from highhx.commands.actions.events import events
    from highhx.commands.actions.main import actions
    from highhx.commands.actions.voice import voice
    from highhx.commands.automation.hook import hook
    from highhx.commands.automation.schedule import schedule
    from highhx.commands.automation.trigger import trigger
    from highhx.commands.automation.watch import watchers
    from highhx.commands.build.artifacts import artifacts
    from highhx.commands.build.build import build
    from highhx.commands.build.clean import clean
    from highhx.commands.build.package import package
    from highhx.commands.cloud.account import account
    from highhx.commands.cloud.agent import agent
    from highhx.commands.cloud.login import login, logout
    from highhx.commands.code.exec import exec_command
    from highhx.commands.code.fix import fix
    from highhx.commands.code.run import run
    from highhx.commands.code.script import script
    from highhx.commands.code.task import task
    from highhx.commands.code.watch import watch
    from highhx.commands.computer.do import do
    from highhx.commands.computer.main import computer
    from highhx.commands.computer_use.android import android
    from highhx.commands.computer_use.browser import browser
    from highhx.commands.computer_use.replay import replay
    from highhx.commands.computer_use.sandbox import sandbox
    from highhx.commands.computer_use.trajectories import trajectories
    from highhx.commands.computer_use.tui import tui
    from highhx.commands.database.main import db
    from highhx.commands.dependencies.main import deps
    from highhx.commands.deployment.deploy import deploy
    from highhx.commands.deployment.environments import environments
    from highhx.commands.deployment.rollback import rollback
    from highhx.commands.diagnostics.debug import debug
    from highhx.commands.diagnostics.diagnose import diagnose
    from highhx.commands.diagnostics.repair import repair
    from highhx.commands.diagnostics.runs import runs
    from highhx.commands.diagnostics.trace import trace
    from highhx.commands.docker.main import docker
    from highhx.commands.environment.main import env
    from highhx.commands.git.main import git
    from highhx.commands.integrations.mcp import mcp
    from highhx.commands.observability.audit import audit
    from highhx.commands.observability.history import history
    from highhx.commands.observability.logs import logs
    from highhx.commands.observability.report import report
    from highhx.commands.plugins.main import plugin
    from highhx.commands.project.check import check
    from highhx.commands.project.dev import dev
    from highhx.commands.project.doctor import doctor
    from highhx.commands.project.info import info
    from highhx.commands.project.init import init
    from highhx.commands.project.restart import restart
    from highhx.commands.project.start import start
    from highhx.commands.project.status import status
    from highhx.commands.project.stop import stop
    from highhx.commands.release.changelog import changelog
    from highhx.commands.release.publish import publish
    from highhx.commands.release.release import release
    from highhx.commands.release.version import version
    from highhx.commands.security.main import security
    from highhx.commands.services.main import services
    from highhx.commands.services.ports import ports
    from highhx.commands.team.config import config
    from highhx.commands.team.policy import policy
    from highhx.commands.team.profile import profile
    from highhx.commands.team.workspace import workspace
    from highhx.commands.testing.benchmark import benchmark
    from highhx.commands.testing.test import test
    from highhx.commands.workflows.main import workflow

    return [
        init,
        status,
        info,
        dev,
        start,
        stop,
        restart,
        check,
        doctor,
        run,
        exec_command,
        script,
        task,
        watch,
        fix,
        deps,
        test,
        benchmark,
        build,
        clean,
        package,
        artifacts,
        env,
        git,
        release,
        version,
        changelog,
        publish,
        deploy,
        rollback,
        environments,
        security,
        docker,
        services,
        ports,
        db,
        workflow,
        actions,
        events,
        voice,
        schedule,
        hook,
        trigger,
        watchers,
        logs,
        history,
        report,
        plugin,
        config,
        policy,
        workspace,
        profile,
        debug,
        trace,
        runs,
        diagnose,
        repair,
        agent,
        do,
        computer,
        browser,
        android,
        sandbox,
        replay,
        trajectories,
        tui,
        audit,
        login,
        logout,
        mcp,
        account,
    ]
