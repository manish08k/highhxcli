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
button { cursor:pointer; } button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
textarea { width:100%; min-height:70px; font-family: ui-monospace, monospace; font-size:12px; }
.row { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin:4px 0; }
ul { list-style:none; margin:0; padding:0; } li { padding:4px 0; border-bottom:1px solid var(--line); overflow-wrap:anywhere; }
.log { max-height:300px; overflow:auto; font-family: ui-monospace, monospace; font-size:12px; }
img.live { width:100%; border:1px solid var(--line); border-radius:6px; background:#000; min-height:120px; }
.pill { display:inline-block; padding:0 6px; border-radius:10px; border:1px solid var(--line); font-size:12px; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; margin:0; }
</style></head><body>
<header><h1>HighhX</h1><span id="status" class="dim">connecting…</span><span id="caps" class="dim"></span></header>
<main>
 <div>
  <section><h2>New task</h2>
   <div class="row"><input id="goal" placeholder="what should HighhX do?" style="flex:1" aria-label="goal">
   <select id="surface" aria-label="surface"><option>browser</option><option>desktop</option><option>android</option><option>none</option><option>auto</option></select>
   <button class="primary" id="run">Run</button></div>
   <details><summary class="dim">steps (optional JSON list; otherwise HighhX Free's resolver plans)</summary><textarea id="steps" placeholder='[{"action":"open","parameters":{"url":"https://example.com"}}]'></textarea></details>
   <div id="formerror" class="bad"></div>
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
function button(label, fn) { const b = document.createElement("button"); b.textContent = label; b.addEventListener("click", fn); return b; }
let after = 0;
async function events() {
  try {
    const data = await get("/api/events?after=" + after); after = data.next;
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
    }
  } catch (err) { $("status").textContent = "disconnected (" + err.message + ")"; }
}
async function state() {
  try {
    const s = await get("/api/state");
    $("status").textContent = s.tasks.some((t) => t.status === "running") ? "running" : "idle";
    const avail = s.capabilities.filter((c) => c.status === "available").length;
    $("caps").textContent = `${avail}/${s.capabilities.length} capabilities available here`;
    const tasks = $("tasks"); tasks.replaceChildren();
    for (const t of s.tasks) {
      const li = item(`${t.status}  ${t.goal}  ${t.summary || ""}`, t.status === "completed" ? "ok" : (t.status === "failed" || t.status === "cancelled") ? "bad" : "");
      if (["running", "paused", "queued"].includes(t.status)) {
        li.append(" ", button(t.status === "paused" ? "Resume" : "Pause", () => post(`/api/tasks/${t.id}/${t.status === "paused" ? "resume" : "pause"}`)),
                  " ", button("Cancel", () => post(`/api/tasks/${t.id}/cancel`)));
      }
      tasks.append(li);
    }
    if (!s.tasks.length) tasks.append(item("no task started here yet", "dim"));
    const list = $("approvals"); list.replaceChildren();
    for (const a of s.approvals) {
      const li = item(`[${a.risk}] ${a.action}  ${(a.reasons || []).join("; ")}`, "warn");
      const typed = document.createElement("input"); typed.placeholder = a.confirm_word ? `type ${a.confirm_word}` : "note";
      const decide = (decision, extra) => post(`/api/approvals/${a.id}`, Object.assign({ decision, typed: typed.value }, extra || {})).catch((e) => alert(e.message));
      li.append(document.createElement("br"), typed, " ", button("Approve", () => decide("approve")), " ", button("Reject", () => decide("reject")), " ", button("Later", () => decide("defer")));
      if (a.kind === "confirm") li.append(" ", button("Modify…", () => { const v = prompt("New inputs (JSON object)"); if (v) decide("modify", { inputs: JSON.parse(v) }); }));
      list.append(li);
    }
    if (!s.approvals.length) list.append(item("nothing", "dim"));
    const src = s.streams[$("source").value]; if (src) $("livestats").textContent = `${src.frames} frames · ${(src.bytes / 1e6).toFixed(1)} MB${src.error ? " · " + src.error : ""}`;
  } catch (err) { $("status").textContent = "disconnected"; }
}
async function history() {
  const data = await get("/api/history"); const ul = $("history"); ul.replaceChildren();
  for (const t of data.tasks) {
    const li = item(`${t.status}  ${t.task}  (${t.steps} steps, ${t.surface})`, t.status === "completed" ? "ok" : t.status === "failed" ? "bad" : "");
    li.style.cursor = "pointer";
    li.addEventListener("click", async () => {
      const d = await get("/api/history/" + t.id);
      const steps = d.trajectory.steps.map((s) => `${s.index}. ${s.intent} — ${s.result.outcome || "?"} (${s.action.action_type || ""}${s.result.error ? ": " + s.result.error : ""})`);
      $("detail").textContent = [d.trajectory.task + " [" + d.trajectory.status + "]", ...steps, "", d.trajectory.summary || ""].join("\n");
    });
    ul.append(li);
  }
  if (!data.tasks.length) ul.append(item("no trajectories yet", "dim"));
  const b = await get("/api/benchmarks"); const bl = $("benchmarks"); bl.replaceChildren();
  for (const r of b.benchmarks) bl.append(item(`${r.suite}  ${r.benchmark_id}  success ${r.summary && r.summary.task_success_rate != null ? Math.round(r.summary.task_success_rate * 100) + "%" : "-"}`));
  if (!b.benchmarks.length) bl.append(item("no benchmark results", "dim"));
}
let live = false, seq = 0, objectUrl = null;
async function frames() {
  while (live) {
    try {
      const r = await fetch(q(`/api/frame?source=${$("source").value}&after=${seq}`));
      if (r.status === 200) {
        seq = Number(r.headers.get("X-Frame-Seq")) || seq + 1;
        const url = URL.createObjectURL(await r.blob()); $("frame").src = url; if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = url;
      }
    } catch (err) { await new Promise((ok) => setTimeout(ok, 1000)); }
  }
}
$("live").addEventListener("click", () => { live = !live; $("live").textContent = live ? "Stop" : "Start"; seq = 0; if (live) frames(); });
$("run").addEventListener("click", async () => {
  $("formerror").textContent = "";
  try {
    const raw = $("steps").value.trim();
    await post("/api/tasks", { goal: $("goal").value, surface: $("surface").value, steps: raw ? JSON.parse(raw) : undefined });
    $("goal").value = "";
  } catch (err) { $("formerror").textContent = err.message; }
});
setInterval(events, 1000); setInterval(state, 1500); setInterval(history, 10000);
events(); state(); history();
</script></body></html>
"""


def page(token: str, nonce: str) -> str:
    return _PAGE.replace("__TOKEN__", json.dumps(token)).replace("__NONCE__", nonce)
