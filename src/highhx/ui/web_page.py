"""The web console's single page (no external resources; the script runs under a per-response
CSP nonce). Everything from the runtime is inserted as text (``textContent``), never as HTML:
event payloads can contain page text."""

from __future__ import annotations

import json

_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>HighhX console</title>
<style>
:root { --bg:#0f1115; --panel:#171a21; --line:#272b36; --text:#e6e6e6; --dim:#9aa0ab; --ok:#4cc38a; --bad:#ef6461; --warn:#e5b45b; --accent:#7aa2f7; }
@media (prefers-color-scheme: light) { :root { --bg:#f5f6f8; --panel:#fff; --line:#dde1e7; --text:#1d2129; --dim:#5f6774; } }
* { box-sizing: border-box; } body { margin:0; font:14px/1.45 system-ui, sans-serif; background:var(--bg); color:var(--text); }
header { display:flex; gap:16px; align-items:center; padding:10px 16px; border-bottom:1px solid var(--line); background:var(--panel); position:sticky; top:0; }
header h1 { font-size:16px; margin:0; } .dim { color:var(--dim); } .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
main { display:grid; grid-template-columns: minmax(320px, 1fr) minmax(320px, 1.2fr); gap:12px; padding:12px; }
@media (max-width: 900px) { main { grid-template-columns: 1fr; } }
section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 12px; min-width:0; }
section h2 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; margin:0 0 8px; color:var(--dim); }
input, select, textarea, button { font:inherit; color:inherit; background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:6px 8px; }
button { cursor:pointer; } button:disabled { opacity:.55; cursor:progress; } button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
textarea { width:100%; min-height:70px; font-family: ui-monospace, monospace; font-size:12px; }
.row { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin:4px 0; }
ul { list-style:none; margin:0; padding:0; } li { padding:4px 0; border-bottom:1px solid var(--line); overflow-wrap:anywhere; }
.log { max-height:300px; overflow:auto; font-family: ui-monospace, monospace; font-size:12px; }
img.live { width:100%; border:1px solid var(--line); border-radius:6px; background:#000; min-height:120px; }
.pill { display:inline-block; padding:0 6px; border-radius:10px; border:1px solid var(--line); font-size:12px; }
.err { display:block; } .editor textarea { min-height:60px; }
:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; margin:0; }
</style></head><body>
<header><h1>HighhX</h1><span id="status" class="dim" role="status" aria-live="polite">connecting…</span><span id="caps" class="dim"></span></header>
<main>
 <div>
  <section><h2>New task</h2>
   <form id="newtask"><div class="row"><input id="goal" placeholder="what should HighhX do?" style="flex:1" aria-label="goal">
   <select id="surface" aria-label="surface"><option>browser</option><option>desktop</option><option>android</option><option>none</option><option>auto</option></select>
   <button class="primary" id="run" type="submit">Run</button></div>
   <details><summary class="dim">steps (optional JSON list; otherwise HighhX Free's resolver plans)</summary><textarea id="steps" aria-label="steps" placeholder='[{"action":"open","parameters":{"url":"https://example.com"}}]'></textarea></details>
   <div id="formerror" class="bad" role="alert"></div></form>
  </section>
  <section><h2>Tasks</h2><ul id="tasks"></ul></section>
  <section><h2>Waiting for you</h2><ul id="approvals"><li class="dim">nothing</li></ul></section>
  <section><h2>Live view</h2>
   <div class="row"><select id="source" aria-label="live source"><option>browser</option><option>desktop</option></select>
   <button id="live">Start</button><span id="livestats" class="dim"></span></div>
   <img id="frame" class="live" alt="live view (not started)">
  </section>
 </div>
 <div>
  <section><h2>Plan and activity</h2><div id="plan" class="dim"></div><ul id="activity" class="log"></ul></section>
  <section><h2>Network</h2><ul id="network" class="log"></ul></section>
  <section><h2>History</h2><ul id="history"></ul><pre id="detail"></pre></section>
  <section><h2>Benchmarks</h2><ul id="benchmarks"></ul></section>
 </div>
</main>
<script nonce="__NONCE__">
const TOKEN = __TOKEN__;
const $ = (id) => document.getElementById(id);
const q = (path) => path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
async function get(path) { const r = await fetch(q(path)); if (!r.ok) throw new Error(r.status); return r.json(); }
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-HighhX-Token": TOKEN }, body: JSON.stringify(body || {}) });
  const data = await r.json().catch(() => ({})); if (!r.ok) throw new Error(data.error || r.status); return data;
}
function item(text, cls) { const li = document.createElement("li"); li.textContent = text; if (cls) li.className = cls; return li; }
function el(tag, props, ...children) { const e = Object.assign(document.createElement(tag), props || {}); e.append(...children); return e; }
// One request per button at a time: disabled (and marked busy) until it answers. A second click
// while it runs does nothing — no duplicate task, no second decision.
async function busy(btn, fn) {
  if (btn.disabled) return;
  btn.disabled = true; btn.setAttribute("aria-busy", "true");
  try { return await fn(); } finally { btn.disabled = false; btn.removeAttribute("aria-busy"); }
}
function button(label, fn) { const b = el("button", { type: "button", textContent: label }); b.addEventListener("click", () => busy(b, () => fn(b))); return b; }
let connected = null, after = 0, lastState = null;
function connection(ok, why) {
  if (ok === connected && ok) return;
  connected = ok;
  if (!ok) { $("status").textContent = "disconnected — retrying" + (why ? " (" + why + ")" : ""); $("status").className = "bad"; }
  else render();
}
async function events() {
  try {
    const data = await get("/api/events?after=" + after); after = data.next; connection(true);
    for (const e of data.events) {
      const p = e.payload || {}; const n = e.event;
      if (n === "plan.created" || n === "plan.updated") {
        const items = (p.items || (p.steps || []).map((s) => ({ title: s, status: "pending" })));
        $("plan").textContent = items.map((i) => ({ done: "✓", running: "●", failed: "✗" }[i.status] || "○") + " " + i.title).join("   ");
      }
      if (n === "network.observed") for (const r of (p.entries || [])) $("network").prepend(item(`${r.method} ${r.url} → ${r.status ?? r.error ?? "…"}${r.ms != null ? " " + r.ms + "ms" : ""}`, (r.error || r.status >= 400) ? "bad" : ""));
      const cls = n.endsWith(".failed") || n.endsWith(".denied") ? "bad" : n.endsWith(".completed") || n.endsWith(".granted") ? "ok" : n.startsWith("approval") ? "warn" : "";
      const detail = p.action || p.task || p.summary || p.reason || p.target || p.error || "";
      $("activity").prepend(item(`${new Date(e.ts).toLocaleTimeString()}  ${n}  ${typeof detail === "string" ? detail : JSON.stringify(detail)}`, cls));
      while ($("activity").children.length > 300) $("activity").lastChild.remove();
      while ($("network").children.length > 300) $("network").lastChild.remove();
    }
  } catch (err) { connection(false, err.message); }
}
const LABEL = { waiting: "waiting for approval", running: "running", paused: "paused", queued: "queued", starting: "starting" };
// What the header says is what the runtime is doing: a task waiting for an answer says so.
function render() {
  if (connected === false || !lastState) return;
  const s = lastState, st = (x) => s.tasks.filter((t) => t.status === x).length;
  const [text, cls] = st("waiting") ? [`waiting for approval (${s.approvals.length})`, "warn"]
    : st("running") ? ["running", "ok"] : st("paused") ? ["paused", "warn"]
    : (st("queued") || st("starting")) ? ["starting", "dim"] : ["idle", "dim"];
  $("status").textContent = text; $("status").className = cls;
}
// Lists keep their rows across polls (keyed by id): a half-typed confirmation, the focus and a
// click in progress survive the refresh; rows change only when their item does.
const taskRows = new Map(), approvalRows = new Map();
function taskRow(t) {
  let row = taskRows.get(t.id);
  if (!row) {
    row = { li: el("li"), text: el("span"), controls: el("span"), err: el("span", { className: "bad err", role: "alert" }), status: null };
    row.li.append(row.text, " ", row.controls, row.err); taskRows.set(t.id, row);
  }
  row.text.textContent = `${LABEL[t.status] || t.status}  ${t.goal}  ${t.summary || ""}`;
  row.li.className = t.status === "completed" ? "ok" : (t.status === "failed" || t.status === "cancelled") ? "bad" : t.status === "waiting" ? "warn" : "";
  if (row.status !== t.status) {
    row.status = t.status; row.controls.replaceChildren();
    const control = (op) => async () => {
      row.err.textContent = "";
      try { await post(`/api/tasks/${t.id}/${op}`); await state(); } catch (e) { row.err.textContent = " " + e.message; }
    };
    if (["running", "waiting", "queued", "starting"].includes(t.status)) row.controls.append(button("Pause", control("pause")), " ", button("Cancel", control("cancel")));
    if (t.status === "paused") row.controls.append(button("Resume", control("resume")), " ", button("Cancel", control("cancel")));
  }
  return row.li;
}
function approvalRow(a) {
  if (approvalRows.has(a.id)) return approvalRows.get(a.id).li;
  const typed = el("input", { placeholder: a.confirm_word ? `type ${a.confirm_word}` : "note (optional)" });
  typed.setAttribute("aria-label", a.confirm_word ? `type ${a.confirm_word} to approve` : "note");
  const err = el("span", { className: "bad err", role: "alert" });
  const li = el("li", { className: "warn" }, `[${a.risk}] ${a.action}  ${(a.reasons || []).join("; ")}`, el("br"), typed, " ");
  const decide = async (decision, extra) => {
    err.textContent = "";
    try {
      await post(`/api/approvals/${a.id}`, Object.assign({ decision, typed: typed.value, note: a.confirm_word ? "" : typed.value }, extra || {}));
      await state();
    } catch (e) { err.textContent = e.message; }
  };
  const approve = button("Approve", () => decide("approve"));
  typed.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); approve.click(); } });
  li.append(approve, " ", button("Reject", () => decide("reject")), " ", button("Later", () => decide("defer")));
  if (a.kind === "confirm") {
    const editor = el("div", { className: "editor", hidden: true });
    const inputs = el("textarea", { value: "{}" }); inputs.setAttribute("aria-label", "new inputs (JSON object)");
    const send = button("Send modified", async () => {
      let parsed;
      try { parsed = JSON.parse(inputs.value); } catch (e) { err.textContent = "the new inputs are not valid JSON"; return; }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) { err.textContent = "the new inputs must be a JSON object"; return; }
      await decide("modify", { inputs: parsed });
    });
    inputs.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { editor.hidden = true; modify.focus(); } });
    editor.append(inputs, send);
    const modify = button("Modify…", () => { editor.hidden = !editor.hidden; if (!editor.hidden) inputs.focus(); });
    li.append(" ", modify, editor);
  }
  li.append(err);
  approvalRows.set(a.id, { li });
  return li;
}
function reconcile(list, rows, items, make, empty) {
  const ids = new Set(items.map((x) => x.id));
  let refocus = false;
  for (const [id, row] of rows) if (!ids.has(id)) { refocus = refocus || row.li.contains(document.activeElement); row.li.remove(); rows.delete(id); }
  const wanted = items.map(make);
  if (wanted.length) {
    for (const node of [...list.children]) if (!wanted.includes(node)) node.remove();
    wanted.forEach((node, i) => { if (list.children[i] !== node) list.insertBefore(node, list.children[i] || null); });
  } else list.replaceChildren(item(empty, "dim"));
  return refocus;
}
async function state() {
  try {
    const s = await get("/api/state"); lastState = s; connection(true);
    const avail = s.capabilities.filter((c) => c.status === "available").length;
    $("caps").textContent = `${avail}/${s.capabilities.length} capabilities available here`;
    reconcile($("tasks"), taskRows, s.tasks, taskRow, "no task started here yet");
    if (reconcile($("approvals"), approvalRows, s.approvals, approvalRow, "nothing")) {
      // the answered approval is gone: focus moves on — to the next one waiting, else to the goal
      const next = $("approvals").querySelector("input"); (next || $("goal")).focus();
    }
    const src = s.streams[$("source").value]; if (src) $("livestats").textContent = `${src.frames} frames · ${(src.bytes / 1e6).toFixed(1)} MB${src.error ? " · " + src.error : ""}`;
    render();
  } catch (err) { connection(false, err.message); }
}
async function history() {
  try {
    const data = await get("/api/history"); const ul = $("history"); ul.replaceChildren();
    for (const t of data.tasks) {
      const li = item(`${t.status}  ${t.task}  (${t.steps} steps, ${t.surface})`, t.status === "completed" ? "ok" : t.status === "failed" ? "bad" : "");
      li.tabIndex = 0; li.style.cursor = "pointer";
      const open = async () => {
        try {
          const d = await get("/api/history/" + t.id);
          const steps = d.trajectory.steps.map((s) => `${s.index}. ${s.intent} — ${s.result.outcome || "?"} (${s.action.action_type || ""}${s.result.error ? ": " + s.result.error : ""})`);
          $("detail").textContent = [d.trajectory.task + " [" + d.trajectory.status + "]", ...steps, "", d.trajectory.summary || ""].join("\n");
        } catch (e) { $("detail").textContent = "could not load this task: " + e.message; }
      };
      li.addEventListener("click", open); li.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); } });
      ul.append(li);
    }
    if (!data.tasks.length) ul.append(item("no trajectories yet", "dim"));
    const b = await get("/api/benchmarks"); const bl = $("benchmarks"); bl.replaceChildren();
    for (const r of b.benchmarks) bl.append(item(`${r.suite}  ${r.benchmark_id}  success ${r.summary && r.summary.task_success_rate != null ? Math.round(r.summary.task_success_rate * 100) + "%" : "-"}`));
    if (!b.benchmarks.length) bl.append(item("no benchmark results", "dim"));
  } catch (err) { connection(false, err.message); }
}
// The live view: one frame loop and one frame request at a time. Stopping aborts the request in
// flight (a 5 s long poll), so Stop then Start quickly neither runs two loops nor piles up requests.
let liveGen = 0, liveAbort = null, seq = 0, objectUrl = null;
async function frames(gen, signal) {
  while (gen === liveGen) {
    try {
      const r = await fetch(q(`/api/frame?source=${$("source").value}&after=${seq}`), { signal });
      if (gen !== liveGen) break;
      if (r.status === 200) {
        seq = Number(r.headers.get("X-Frame-Seq")) || seq + 1;
        const url = URL.createObjectURL(await r.blob()); $("frame").src = url; if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = url;
      } else if (r.status !== 204) { $("livestats").textContent = "live view: " + r.status; await new Promise((ok) => setTimeout(ok, 1000)); }
    } catch (err) { if (gen !== liveGen) break; await new Promise((ok) => setTimeout(ok, 1000)); }
  }
}
function live(on) {
  liveGen += 1; seq = 0;
  if (liveAbort) liveAbort.abort();
  liveAbort = on ? new AbortController() : null;
  $("live").textContent = on ? "Stop" : "Start";
  if (on) frames(liveGen, liveAbort.signal);
}
$("live").addEventListener("click", () => live($("live").textContent === "Start"));
$("source").addEventListener("change", () => { if ($("live").textContent === "Stop") live(true); });
// A new task: Enter or Run. One request at a time; a retry of a request whose answer was lost
// reuses its id, so the console returns the task it started instead of starting a second one.
let lastRequest = null;
$("newtask").addEventListener("submit", (ev) => {
  ev.preventDefault();
  busy($("run"), async () => {
    $("formerror").textContent = "";
    const raw = $("steps").value.trim();
    let steps;
    try { steps = raw ? JSON.parse(raw) : undefined; } catch (e) { $("formerror").textContent = "steps: not valid JSON"; $("steps").focus(); return; }
    const goal = $("goal").value.trim();
    if (!goal) { $("formerror").textContent = "say what HighhX should do"; $("goal").focus(); return; }
    const key = JSON.stringify([goal, $("surface").value, raw]);
    const id = lastRequest && lastRequest.key === key ? lastRequest.id : (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
    lastRequest = { key, id };
    try {
      await post("/api/tasks", { goal, surface: $("surface").value, steps, request_id: id });
      lastRequest = null; $("goal").value = ""; await state();
    } catch (err) {
      if (!(err instanceof TypeError)) lastRequest = null;  // a refusal is final; a lost answer is retried as the same request
      $("formerror").textContent = err instanceof TypeError ? "the console did not answer; Run again to retry the same request" : err.message;
    }
    $("goal").focus();
  });
});
setInterval(events, 1000); setInterval(state, 1500); setInterval(history, 10000);
events(); state(); history();
</script></body></html>
"""


def page(token: str, nonce: str) -> str:
    return _PAGE.replace("__TOKEN__", json.dumps(token)).replace("__NONCE__", nonce)
