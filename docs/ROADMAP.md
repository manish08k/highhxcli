# Roadmap

What is built is described in [PRODUCT_SPEC.md](PRODUCT_SPEC.md). This page lists what is
not built yet — and what is deliberately not built — so that the product never claims more
than it does.

## Next

| Item | Why |
|---|---|
| Background tasks | Run an autonomous task detached (`highhx agent --verify … --background`), with status, logs and cancel — the task runtime already records every attempt |
| Remote execution targets for actions | Run the same action graphs on a CI runner or a server; the handler contract (`ActionContext` → `ActionResult`) is transport-agnostic |
| Background scheduler service | `highhx schedule run` is a foreground process today; an optional service would make schedules survive logout |
| Action-level undo for database changes | Pair `database.migrate` with an automatic backup and a restore compensation |
| Richer structured outputs for command-backed actions | Today they return the command and exit code; parsing the commands' `--json` documents would give typed outputs to workflows and the agent |
| Plain-language entities from more sources | Test names, package names, container names for the resolver |
| Windows voice recording | Recorders are POSIX tools today; SAPI speech already works |
| IDE and chat interfaces | Same session contract as voice: text in, the same engine, outcomes out |

## Deliberately not built

| Item | Reason |
|---|---|
| AI on Free (including "just for intent classification") | Free is deterministic by definition: no model calls, no provider keys, no account |
| Bring-your-own-key model providers in the CLI | All AI goes through the platform, which authenticates, entitles and meters every request |
| Multi-agent orchestration | One agent per session keeps a single auditable decision trail and one approval stream; no demonstrated benefit for the targeted tasks yet |
| A .NET / native worker | Measured: the Python engine dispatches a command through the full safety pipeline in ~3 ms (see [ARCHITECTURE.md](ARCHITECTURE.md#runtime-choice)); a second runtime would cost installability without a measurable gain |
| An always-listening microphone | Voice is push-to-talk by design |
| Automatic retries of non-idempotent actions | A retried push, deploy or install can do damage twice |
