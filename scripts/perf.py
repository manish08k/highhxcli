"""Measure HighhX's runtime: executor, events, browser, screenshots, grounding, verification,
concurrent sessions, memory. Prints JSON. Real Chrome parts need a Chromium-family browser.

    python scripts/perf.py [--browser] [--rounds N]

Numbers are medians (and p95) over the rounds on this machine; they are measurements, not claims.
"""

from __future__ import annotations

import argparse
import json
import platform
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "n": len(samples),
        "median_ms": round(statistics.median(ordered) * 1000, 3),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))] * 1000, 3),
    }


def timed(fn: Any, rounds: int) -> list[float]:
    out = []
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        out.append(time.perf_counter() - started)
    return out


def executor_and_events(rounds: int) -> dict[str, Any]:
    from highhx.actions.executor import ActionExecutor
    from highhx.commands import App
    from highhx.core.context import Options
    from highhx.core.events import EventBus
    from highhx.observability.stream import EventRecorder
    from highhx.safety.actions import Actor
    from highhx.safety.gate import ActionGate, ApprovalMode

    root = Path(tempfile.mkdtemp(prefix="highhx-perf-"))
    (root / "a.txt").write_text("x\n" * 100)
    app = App(Options(interactive=False, yes=True), cwd=root)

    class Yes:
        interactive = True

        def ask_permission(self, *a: Any, **k: Any) -> str:
            return "yes"

        def confirm_action(self, *a: Any) -> bool:
            return True

    executor = ActionExecutor(
        app, ActionGate(app.engine, Yes(), source="perf", mode=ApprovalMode.ASK), actor=Actor.USER
    )
    plan = timed(lambda: executor.plan("filesystem.read", {"path": "a.txt"}), rounds)
    run = timed(lambda: executor.run("filesystem.read", {"path": "a.txt"}), rounds)
    bus = EventBus()
    EventRecorder.attach(bus)
    count = 20_000
    started = time.perf_counter()
    for i in range(count):
        bus.emit("action.completed", action="filesystem.read", seconds=0.001, i=i)
    elapsed = time.perf_counter() - started
    executor.close()
    app.close()
    return {
        "executor_plan": stats(plan),
        "executor_run_read": stats(run),
        "events_per_second": round(count / elapsed),
    }


def browser(rounds: int) -> dict[str, Any]:
    import http.server
    import threading

    from highhx.computer.browser import ChromeBrowser, find_browser
    from highhx.grounding import HybridGrounder, Target
    from highhx.perception.state import ComputerState
    from highhx.verification.declarative import VerificationContext, verify

    if not find_browser():
        return {"skipped": "no Chromium-family browser"}
    page = (
        "<!doctype html><title>Perf</title>"
        + "".join(f"<button>Button {i}</button>" for i in range(50))
        + "<p>Order total: 42</p>"
    )

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page.encode())

        def log_message(self, *a: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    base = Path(tempfile.mkdtemp(prefix="highhx-perf-browser-"))
    chrome = ChromeBrowser(base / "a", headless=True)
    try:
        started = time.perf_counter()
        chrome.start()
        cold_start = time.perf_counter() - started
        chrome.navigate(url)
        navigate = timed(lambda: chrome.navigate(url), rounds)
        observe = timed(chrome.observe, rounds)
        screenshot = timed(chrome.screenshot, rounds)
        observation = chrome.observe()
        state = (
            ComputerState.from_observation(observation, surface="browser")
            if hasattr(ComputerState, "from_observation")
            else None
        )
        grounding: list[float] = []
        verification: list[float] = []
        if state is not None:
            grounder = HybridGrounder()
            grounding = timed(
                lambda: grounder.ground(
                    state, Target.of("Button 37", "button"), strategies=("accessibility", "dom", "text")
                ),
                rounds,
            )
            verification = timed(lambda: verify({"text": "Order total"}, VerificationContext(after=state)), rounds)
        concurrent = [ChromeBrowser(base / f"c{i}", headless=True) for i in range(3)]
        started = time.perf_counter()
        for other in concurrent:
            other.navigate(url)
        three_sessions = time.perf_counter() - started
        for other in concurrent:
            other.stop()
        out: dict[str, Any] = {
            "browser_cold_start_ms": round(cold_start * 1000),
            "navigate": stats(navigate),
            "observe_dom": stats(observe),
            "screenshot": stats(screenshot),
            "three_more_browsers_started_and_loaded_ms": round(three_sessions * 1000),
        }
        if grounding:
            out["grounding"] = stats(grounding)
            out["verification"] = stats(verification)
        return out
    finally:
        chrome.stop()
        server.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--rounds", type=int, default=30)
    args = parser.parse_args()
    result: dict[str, Any] = {
        "machine": f"{platform.system()} {platform.release()} {platform.machine()}, Python {platform.python_version()}",
        "core": executor_and_events(args.rounds),
    }
    if args.browser:
        result["browser"] = browser(args.rounds)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["max_rss_mb"] = round(rss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
