"""The page scripts the Chrome provider injects, executed against a real DOM (jsdom).

Needs Node.js and jsdom: set HIGHHX_TEST_JSDOM to a node_modules directory containing jsdom
(e.g. `npm install jsdom` somewhere, then point at its node_modules). Skipped otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from highhx.computer.browser import ACT_JS, OBSERVE_JS

JSDOM = os.environ.get("HIGHHX_TEST_JSDOM")
pytestmark = pytest.mark.skipif(
    not (JSDOM and Path(JSDOM, "jsdom").is_dir() and shutil.which("node")),
    reason="set HIGHHX_TEST_JSDOM to a node_modules dir with jsdom (and install Node.js)",
)

HARNESS = r"""
const { JSDOM } = require(process.env.HIGHHX_TEST_JSDOM + '/jsdom');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const dom = new JSDOM(input.html, { runScripts: 'dangerously', pretendToBeVisual: true, url: 'https://shop.test/' });
const w = dom.window;
// jsdom has no layout engine: give rendered elements a size, hidden ones none.
w.Element.prototype.getBoundingClientRect = function () {
  const hidden = this.hidden || w.getComputedStyle(this).display === 'none';
  return { left: 1, top: 1, width: hidden ? 0 : 10, height: hidden ? 0 : 10 };
};
w.HTMLElement.prototype.scrollIntoView = function () {};
if (!w.CSS) w.CSS = { escape: s => s };
const result = { before: w.eval(input.observe), acts: [] };
for (const [id, action, arg] of input.acts) {
  result.acts.push(w.eval(`(${input.act})(${JSON.stringify(id)}, ${JSON.stringify(action)}, ${JSON.stringify(arg)})`));
}
result.after = w.eval(input.observe);
process.stdout.write(JSON.stringify(result));
"""

PAGE = """<!doctype html><title>Shop</title>
<h1>Search the shop</h1>
<form action="/results" method="get"><label for="q">Search</label><input id="q" name="q" type="search"><button>Go</button></form>
<form method="post" action="/login">
  <input type="email" name="email" aria-label="Email" value="a@b.c">
  <input type="password" name="pw" aria-label="Password" value="hunter2secret">
  <input name="cc" autocomplete="cc-number" placeholder="Card number" value="4242424242424242">
  <input type="submit" value="Sign in">
</form>
<label><input type="checkbox" name="news"> Newsletter</label>
<select aria-label="Size"><option value="s">Small</option><option value="l">Large</option></select>
<button class="btn btn-danger" onclick="document.title='deleted'">Delete account</button>
<a href="/help">Help</a>
<button style="display:none">Hidden</button>
"""


def run(acts: list[list[str]] | None = None) -> dict[str, Any]:
    payload = {"html": PAGE, "observe": OBSERVE_JS, "act": ACT_JS, "acts": acts or []}
    result = subprocess.run(
        ["node", "-e", HARNESS], input=json.dumps(payload), capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(result.stdout)


def by_name(observation: dict[str, Any], name: str) -> dict[str, Any]:
    return next(e for e in observation["elements"] if e["name"] == name)


def test_observation_is_semantic() -> None:
    before = run()["before"]
    roles = {(e["role"], e["name"]) for e in before["elements"]}
    assert {
        ("heading", "Search the shop"),
        ("searchbox", "Search"),
        ("button", "Go"),
        ("textbox", "Email"),
        ("textbox", "Password"),
        ("button", "Sign in"),
        ("checkbox", "Newsletter"),
        ("combobox", "Size"),
        ("button", "Delete account"),
        ("link", "Help"),
    } <= roles
    assert "Hidden" not in {e["name"] for e in before["elements"]}
    assert before["title"] == "Shop" and before["url"] == "https://shop.test/"


def test_secret_values_never_leave_the_page() -> None:
    before = run()["before"]
    assert by_name(before, "Password")["value"] == ""
    assert by_name(before, "Card number")["value"] == ""
    assert by_name(before, "Email")["value"] == "a@b.c"
    assert "hunter2secret" not in json.dumps(before) and "4242424242424242" not in json.dumps(before)


def test_structure_used_by_the_safety_policy() -> None:
    before = run()["before"]
    sign_in = by_name(before, "Sign in")["attributes"]
    assert sign_in["type"] == "submit" and sign_in["form_method"] == "post" and sign_in["form_has_password"] == "True"
    assert "btn-danger" in by_name(before, "Delete account")["attributes"]["class"]
    assert by_name(before, "Help")["attributes"]["href"] == "/help"
    assert by_name(before, "Go")["attributes"]["type"] == "submit"  # <button> defaults to submit


def test_actions_change_the_dom() -> None:
    before = run()["before"]
    checkbox = by_name(before, "Newsletter")["id"]
    size = by_name(before, "Size")["id"]
    delete = by_name(before, "Delete account")["id"]
    result = run(
        [
            [checkbox, "click", ""],
            [size, "select", "Large"],
            [delete, "click", ""],
            ["e999", "click", ""],
            [size, "select", "XXL"],
        ]
    )
    assert [a.get("found") for a in result["acts"]] == [True, True, True, False, True]
    assert result["acts"][4]["error"] == "no such option"
    after = result["after"]
    assert by_name(after, "Newsletter")["checked"] is True
    assert by_name(after, "Size")["value"] == "l"
    assert after["title"] == "deleted"
