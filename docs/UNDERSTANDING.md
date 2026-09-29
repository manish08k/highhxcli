# Understanding requests: HXIR, references and computer context

HighhX turns what people say into structured, validated data before anything runs. The rule is
**understand broadly, resolve precisely, ask when unclear** — and the existing execution
system stays authoritative: the language layer never executes anything.

```text
request
  │  normalise            "can u open github pls" → "open github"
  │  correction?          "No, I meant GitHub" → the previous request with the new object
  │  answer?              "the second one" / "2" → completes a question HighhX asked
  │  execution limits     "…, but don't open anything" → forbid: open
  ▼
clauses                   split before known verbs ("open GitHub, find my repo, and open it")
  │  reference?           "it", "that tab", "the second one", "the file we were working on", "here"
  │  developer rules      run the tests, git, deploy …              (actions/resolver.py, unchanged)
  │  verb grammar         open, search, play, click, type …         (language/grammar.py, unchanged)
  │  extended intents     the browser · repository on a site · project by name · files by kind/time/topic
  ▼
HXIR  (language/hxir.py)  resolved · ambiguous · missing_information · unsupported · invalid · open_ended
  │  to_steps()           only a resolved HXIR; every action re-validated against the action catalog
  ▼
JSON action plan  →  plan runner  →  ActionExecutor (classify → approve → run → verify → audit)
```

The deterministic decider (`decision/deterministic.py`) first runs the existing resolver
exactly as before. Only when that does not resolve a request does it call
`language/understand.py`. So every request that resolved before resolves identically; the new
stage only changes requests that previously went to "unknown" or "Pro".

## HXIR (version 1)

`highhx do --plan --json "…"` prints the decision, including its `hxir`:

```json
{
  "version": "1",
  "request": "find the architecture document from yesterday and open it",
  "status": "resolved",
  "goal": {"type": "multi_step", "description": "…"},
  "clauses": [{"text": "find the architecture document from yesterday", "status": "resolved", "intent": "filesystem.find"},
              {"text": "open it", "status": "resolved", "intent": "filesystem.open"}],
  "entities": [{"kind": "file", "value": "docs/architecture.md", "source": "project_index",
                "confidence": "high", "evidence": "the only matching file",
                "attributes": {"modified": "2026-09-29T11:00:00", "size": "2048"}}],
  "constraints": [{"kind": "file_type", "value": "document"}, {"kind": "time", "value": "yesterday"}],
  "references": [{"text": "it", "kind": "pronoun", "status": "resolved", "entity": 0}],
  "actions": [{"id": "a1", "action": "filesystem.find", "inputs": {…}, "depends_on": []},
              {"id": "a2", "action": "filesystem.open", "inputs": {"path": "docs/architecture.md"}, "depends_on": ["a1"]}],
  "ambiguities": [],
  "question": "",
  "reason": ""
}
```

| status | meaning | what happens |
|---|---|---|
| `resolved` | every clause maps to catalog actions and real entities | the plan runs through the executor |
| `ambiguous` | several real candidates | HighhX lists them and asks; nothing runs |
| `missing_information` | a referent or a file is not known | HighhX says what is missing; nothing runs |
| `unsupported` | HighhX knows what was asked and cannot do it (or does not know the named entity) | explained; nothing runs |
| `invalid` | the request contradicts itself ("open it but don't open anything") | explained; nothing runs |
| `open_ended` | needs understanding beyond the grammar | the existing HighhX Pro route |

Guarantees:

- **Closed schema.** Unknown fields anywhere are errors (`hxir.validate`, `hxir.parse`), so a
  model or file cannot smuggle data through. Error messages name the exact field.
- **Only catalog actions execute.** `hxir.to_steps(hxir, catalog)` accepts only a `resolved`
  HXIR and re-validates it — known action names, inputs against each action's own input
  schema, `depends_on` pointing at earlier actions, and the request's own execution
  constraints — even for an HXIR built in memory. The executor then classifies, approves,
  verifies and audits each step exactly as for any other request.
- **Deterministic serialisation.** `to_json()` is the same bytes for the same HXIR.
- **No secrets.** Typed text (`text`, `content` inputs) is serialised as its length only. Run
  traces pass every free-text field through the existing secret redactor
  (`to_dict(scrub=redactor.redact)`).
- **Versioning.** Readers accept only versions they know (`SUPPORTED_VERSIONS`); an unknown
  version is rejected with a clear message. Adding an optional field keeps the version;
  renaming, removing or changing the meaning of a field bumps it, and the old reader stays.

**Confidence** is a decision signal, not a probability: `high` rests on exact evidence (a
registry name, an existing path, a git remote, an entity this conversation acted on);
`medium` on unique but indirect evidence (a keyword in a file name, part of a project name).
Every entity carries its `evidence` in words. `low` is never acted on.

## Computer context (what HighhX looks at)

Context is gathered lazily — nothing is scanned at startup — and only from sources the rest
of HighhX may already use:

| source | what | where |
|---|---|---|
| project file index | names, kinds, sizes and modification times of project files — never contents | `project/index.py` |
| project identity | the project folder's name and its git remotes (`git config`, read-only) | `language/understand.py` (`project_names`), `actions/resolver.py` (`repository_urls`) |
| target registry | websites, applications, project places (`targets.yaml`) | `language/targets.py` |
| installed browsers | whether each known browser is installed (macOS application folders; `PATH` on Linux) | `computer/desktop.py` (`app_installed`) |
| conversation memory | what this session acted on: pages, apps, files, folders, and the last list shown | `language/references.py` (`ConversationMemory`) |

The project file index is built on the first file request (for this repository: ~12 ms, then
cached for 30 s), stops at 20,000 files and reports `partial` beyond that. It skips exactly what
HighhX's file tools skip: secret files (`.env`, keys, `credentials.json`, data files named like
secrets), dependency and build folders, HighhX's own state, and symbolic links (it never
follows a link out of the project).

**Not part of the context layer** (not implemented; requests that need them are answered as
unsupported, missing information, or open-ended — never faked): the list of running
applications, open windows, processes and services, browser tabs other than those HighhX
opened, the clipboard, environment variables, and files outside the project. In particular,
"the PDF I downloaded yesterday" is answered with *Files you downloaded are in your Downloads
folder, outside this project…*: every file action is confined to the project
(`agent.permissions.confine_path`), and this layer does not widen that policy. On Windows,
`app_installed` cannot tell which browsers are installed, so "start the browser" asks which one.

## Entity resolution

| request | resolves to | evidence |
|---|---|---|
| `open my repo`, `open the current repository`, `take me to the project I'm working on` | the current project (`filesystem.open .`) | HighhX runs in it |
| `open my HighhX project` | the current project, when its folder or git remote is called that (exactly: high; as part of the name: medium) | the matched name |
| `open my payroll project` | missing information — HighhX only knows the project it runs in | — |
| `open my repository on GitHub`, `open my github repo`, `open GitHub, find my HighhX repository` | the project's GitHub remote page (origin first) | the git remote |
| `start the browser`, `switch to the browser` | the installed browser; several installed → a question | installed applications |
| `find my PDF`, `show me the latest report`, `find the document about the architecture` | `filesystem.find` (a safe, read-only catalog action) | the file index |
| `open the latest report`, `open the pdf` | `filesystem.open` of the one match — or a question listing the matches with their modification times | the file index |

A file description must say *something* about the file — a kind, a word in its name, or a
time. "Open the second file" or "the file I was working on" with no conversation context is
answered with a question; HighhX never picks a file from the whole project by date alone.

## Constraints

`language/constraints.py` extracts, by fixed phrase rules: file kinds (`pdf`, `image`,
`document`, …), time windows (`today`, `yesterday`, `this week`, `last week`, `this month`,
`last month`, `recently` — calendar ranges in local time), ordering (`latest`, `oldest`),
ordinals (`first` … `tenth`, `2nd`, `last`), `my`, issue/PR state (`open`/`closed`),
`assigned to me`, `I downloaded`, `I was working on`, and **execution constraints**:
`don't open anything`, `without changing anything`, `don't delete anything`, `only show me`.

Execution constraints are enforced, not just recorded: a plan whose actions would open,
change or delete something the request forbids is `invalid` and nothing runs. What counts as
a change is the executor's own classification (`decision/risk.py`), so "run the tests
without changing anything" is refused — running tests is a controlled action.

## References

`language/references.py` detects a reference when it is the whole object of a clause
(`open it in chrome` → verb `open`, reference `it`, tail `in chrome`), so a query such as
"search for make it rain" is never rewritten. What a reference can point at comes only from
facts: entities the earlier clauses of the same request produced, and entities earlier turns
of this session **actually acted on** (recorded after execution, successful steps only).

| reference | rule |
|---|---|
| `it`, `that`, `this one` | the entities of the *immediately preceding* clause or turn: one → resolved; several → a question; none → missing. It never reaches further back. |
| `that tab`, `this file` | the same, restricted to that kind |
| `the file I just opened`, `the image I opened earlier`, `the page we were working on` | the most recent entity of that kind in the conversation |
| `the previous tab/page` | the one before the most recent |
| `the current page`, `the current file` | the most recent entity of that kind |
| `the first one`, `the 2nd file`, `number 3`, `the last one` | from the last list HighhX showed (find results, a folder listing, or the candidates of its question) |
| `here`, `the current project`, `this repository` | the current project |
| `there` | the most recent place (page, folder, file) |

An unresolved reference only answers a clause that nothing else resolves: "Deploy this
application" is not about an earlier entity and still goes to HighhX Pro.

**Questions and answers.** When HighhX asks *Which one do you mean?*, the session remembers
the question; `the second one`, `2`, `use the second one` or the candidate's name completes
the original request. **Corrections**: `No, I meant GitHub` replaces the object of the last
request's last clause (`open YouTube` → `open GitHub`); `Actually open my repository` is a new
request. The HXIR records what was corrected (`correction_of`).

The memory is in-process, bounded (20 turns, 50 listed items), cleared by `/clear`, never
persisted, and holds only names, paths and addresses — never typed text or file contents.
`highhx do` has no conversation, so a reference there is answered as missing information.

## Free and Pro

- **Free** stays deterministic: no model, no network, no similarity matching. Everything on
  this page runs on Free. Requests that need understanding are still routed to the existing
  Pro capability panel (`open_ended`), exactly as before.
- **Pro** is unchanged. In a Pro session requests go to the AI agent, whose conversation
  history resolves references itself, and whose actions go through the same action executor,
  risk classification, approval and audit. The model is never given a way around them.
  There is no model → HXIR path wired in yet: `hxir.parse` / `hxir.to_steps` are the gate such
  a path must use (closed schema, catalog validation), but nothing calls them with model
  output today. The browser goal loop (`highhx computer task`) has its own, separate Task IR.

## Extending

| to add | change |
|---|---|
| a website, application or project place | `targets.yaml` (built-in or the user's) — no code |
| a reference phrase | `PATTERNS` / `NOUN_KINDS` in `language/references.py` |
| a constraint | `language/constraints.py` (and `CONSTRAINT_KINDS` in `language/hxir.py`) |
| a file kind | `KINDS` in `project/index.py` (the `filesystem.find` schema follows) |
| an intent | a function in the `_unreferenced` chain of `language/understand.py` that returns steps of existing catalog actions |

## Tests

| file | covers |
|---|---|
| `tests/unit/language/test_normalise_and_constraints.py` | normalisation, constraint extraction, time windows |
| `tests/unit/language/test_references.py` | detection, resolution rules, memory bounds, entities from steps |
| `tests/unit/language/test_hxir.py` | schema, versioning, validation errors, catalog validation, masking, determinism |
| `tests/unit/language/test_project_index.py` | kinds, keywords, times, secrets and links skipped, laziness, bounds |
| `tests/unit/language/test_understand.py` | the pipeline through the decider: compatibility, phrasings, entities, files, constraints, references, answers, corrections |
| `tests/unit/actions/test_find_action.py` | `filesystem.find` through the executor: risk, confinement, schema, verification |
| `tests/unit/agent/test_conversation_context.py` | a Free session end to end: find → open it → a question → "the second one" → corrections |
| `tests/e2e/test_understanding_cli.py` | the real CLI (`highhx do`) with no AI available |
