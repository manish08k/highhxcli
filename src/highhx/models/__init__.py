"""Model interfaces for the computer-use runtime: provider-neutral, injected, never hard-coded.

    LanguageModel    text (and images) in, text out — planners, reflectors, specialists
    VisionModel      a screenshot and a query in, located boxes out — grounding, detection
    EmbeddingModel   texts in, vectors out — trajectory memory search
    OCRModel         an image in, text lines with boxes out

Adapters wrap what HighhX already speaks: the agent's ``ModelProvider`` (the HighhX gateway,
Anthropic, OpenAI, Gemini, and any OpenAI-compatible local server such as Ollama or a UI-TARS
endpoint), local tesseract, and a lexical hashing embedding that needs no model at all.
:mod:`highhx.models.registry` builds the configured ones and keeps the Free/Pro rules:
a remote model needs HighhX Pro, and a local one keeps every screenshot on this computer.
"""

from highhx.models.adapters import ChatLanguageModel, ChatVisionModel, HashingEmbedding, TesseractOCRModel
from highhx.models.interfaces import (
    EmbeddingModel,
    LanguageModel,
    Located,
    ModelReply,
    OCRModel,
    TextBox,
    VisionModel,
)

__all__ = [
    "ChatLanguageModel",
    "ChatVisionModel",
    "EmbeddingModel",
    "HashingEmbedding",
    "LanguageModel",
    "Located",
    "ModelReply",
    "OCRModel",
    "TesseractOCRModel",
    "TextBox",
    "VisionModel",
]
