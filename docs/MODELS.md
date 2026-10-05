# Models

Deterministic features never need a model. When a task uses one:

| Kind | Configure | Notes |
|---|---|---|
| Local planner (any OpenAI-compatible server: Ollama, llama.cpp, vLLM, LM Studio) | `HIGHHX_PLANNER_BASE_URL`, `HIGHHX_PLANNER_MODEL` | Free and Pro |
| Local vision / grounding model | `HIGHHX_VISION_BASE_URL`, `HIGHHX_VISION_MODEL` | separate from the planner (Agent S's split) |
| HighhX Pro (Anthropic, OpenAI, Gemini through the platform) | `highhx login` | the CLI never reads a provider key |

Remote models see the task and screen, so each run needs `--remote-model` / `--remote-vision`.
Capabilities (vision, context, coordinate format) are detected per model; timeouts and retries are set
per provider; tokens and cost are counted (`model.usage`); a model that fails or times out ends the task
as failed and resumable. Events: `model.request`, `model.response`, `model.error` (with latency).

`highhx agent models --discover` asks the model servers **on this computer** what they offer (Ollama's
`/api/tags`, OpenAI-compatible `/models`) — only loopback addresses, only when asked, never with a key.

**Validation status:** no model server or key was available on the build machine, so model-driven
planning and vision grounding were exercised with scripted models only. The full screenshot → model →
grounding → policy → executor → verification path with a real local model is **not yet validated**.
