# Free and Pro

One interface, one engine, two plans. The plan decides which capabilities a session has;
the HighhX platform grants and enforces them.

## What each plan is

| | HighhX Free | HighhX Pro |
|---|---|---|
| Positioning | Deterministic developer and computer automation | Deterministic automation + AI developer agent |
| Account | Not needed | Needed (`highhx login`) |
| Plain language | Deterministic resolver (no AI, no JEv): natural language → deterministic decision → JSON action plan → verified automation ([FREE_AUTOMATION.md](FREE_AUTOMATION.md)); multi-step workflows included | Anything — natural language → LLM agent → context/memory/planning → tools and computer use → verification |
| JEv / advanced reasoning | — (never: no model, no JEv, no AI service) | Yes — behind one gate (`highhx.decision.advanced`) granted only by the platform; today provided by the Pro agent. No separate JEv service exists yet |
| Automation bridge / C# engine | Yes — every desktop action | Yes — the same bridge: the agent's desktop computer-use tools and actions |
| Actions, workflows, shell, files, git, deploy | Yes — same catalog, same executor | Yes — same, plus the agent may propose them |
| Planning | Resolver steps; `/plan` previews | Agent plans with approval; `run_actions` graphs |
| Autonomous tasks | — | "…make sure all tests pass": works until HighhX verifies it (`/task`, `--verify`) |
| Code changes | Formatters/linters (`project.fix`), explicit file actions | AI code generation, refactoring, debugging |
| Browser/desktop | Known websites, apps and project places (target registry): open, search, play, switch to, keys, scroll, click by name — through the HighhX browser and the automation bridge (C#/.NET or Python engine) | AI computer use: dynamic, adaptive, with recovery and replanning |
| Voice | Push-to-talk into the deterministic resolver | Push-to-talk into the agent |
| Sessions | Local event log | Persistent, resumable, synced transcripts |
| Models | none | Anthropic, OpenAI, Gemini via the HighhX gateway |

## Invariants (enforced and tested)

1. **Free never calls a model.** There is no client-side model path: provider adapters are
   used only by the platform server; the CLI builds only the platform provider, and only for
   a Pro session.
2. **Free never reads a provider API key and there is no bring-your-own-key path.** A static
   scan of the package and an instrumented interpreter (which records every environment
   lookup during a Free session) prove it.
3. **Free never constructs the agent runtime.** `create_session` is the only constructor, it
   requires a live platform confirmation of the `agent` feature, and a cached account (an
   editable local file) grants nothing.
4. **Requests that need reasoning go to the Pro panel.** The deterministic resolver never guesses: a request it
   plans completely runs on Free (several deterministic steps included); an unknown name is
   explained; anything open-ended gets the capability it needs ("AI debugging requires
   HighhX Pro") and local actions that can do part of it.
5. **The platform decides.** Capabilities come from `/v1/me`; the AI gateway rejects a Free
   token with `402 plan_required` on every request, including computer-use tools.
6. **Pro adds no execution path.** The agent's actions go through the same executor, with
   stricter approval.

Tests: `tests/unit/agent/test_free_pro_boundary.py` (tripwires on every AI entry point with
positive controls, static scan, instrumented interpreter, tampered-cache cases),
`tests/unit/actions/test_agent_and_boundaries.py` (actions, workflows, voice and plugins on
Free touch no AI), `server/tests` (gateway enforcement).

## Capabilities

`highhx.cloud.capabilities` computes a session's capabilities: local ones always
(`local.commands`, `local.shell`, `local.intents`, `local.automation`) plus the platform's
plan features (`agent`, `agent.code_changes`, `agent.commands`, `agent.git`, `agent.deploy`,
`agent.computer_use`, `cloud.sessions`) when — and only when — the platform reports them.
Within Pro, each action the agent may propose is gated by one of those features.

## Status line

`Free • Local` (signed out) · `Free • Connected` (signed in, Free plan) · `Pro • Connected` ·
`Pro • Offline (cached)` (last known plan; the agent is not attached until the platform
answers) · `… • Platform unavailable`. The session re-checks the platform when a request needs
the agent and attaches it once the platform grants Pro; a plan change (`highhx login`,
`/account`) applies in place.
