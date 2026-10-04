# Model provider interfaces

The computer-use runtime depends on four small interfaces
([`models/interfaces.py`](../src/highhx/models/interfaces.py)), injected where they are used.
No vendor is hard-coded.

| Interface | Method | Used by |
|---|---|---|
| `LanguageModel` | `complete(system, prompt, images=…) → ModelReply` (text, model, tokens, cost when reported) | `ModelPlanner`, the specialist planner |
| `VisionModel` | `locate(image, query) → [Located]`, `detect(image) → [Located]` (boxes in image pixels, confidence) | `VisionGrounder`, `ModelVisionProvider` |
| `EmbeddingModel` | `embed(texts) → vectors` | trajectory search |
| `OCRModel` | `read(image) → [TextBox]` | perception |

## Adapters

| Adapter | Wraps |
|---|---|
| `ChatLanguageModel` | any HighhX `ModelProvider`: the HighhX gateway (Pro), OpenAI-compatible servers (Ollama, LM Studio, vLLM, UI-TARS endpoints …); the Anthropic, OpenAI and Gemini adapters that already exist in `agent/model/` |
| `ChatVisionModel` | a vision-capable `ChatLanguageModel`, asked for JSON boxes (pixels or a 0–1000 grid), checked before use |
| `TesseractOCRModel` | local tesseract |
| `HashingEmbedding` | no model: hashed word and bigram features (lexical, deterministic) |

## Configuration and Free / Pro

| Model | Configuration | Who can use it |
|---|---|---|
| Vision | `computer.vision` in `.highhx/config.yaml` or `HIGHHX_VISION_PROVIDER/BASE_URL/MODEL/COORDINATES/FORMAT` (existing, see [computer-use.md](computer-use.md)) | a local model: anyone. The HighhX gateway: Pro |
| Planner | `HIGHHX_PLANNER_BASE_URL` + `HIGHHX_PLANNER_MODEL` (+ `HIGHHX_PLANNER_API_KEY`), otherwise the local vision model, otherwise HighhX Pro | a model on 127.0.0.1: anyone. Any other endpoint or the gateway: only with explicit consent (`--remote-model` / `--remote-vision`), and the gateway needs Pro |

A remote model receives task text and screenshots, which is why it needs consent. A local model
keeps them on the computer. HighhX Free's deterministic paths (resolver planner, scripted
plans, replays, grounding without vision, benchmarks with scripted planners) make no model
calls at all.
