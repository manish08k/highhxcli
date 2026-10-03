"""Adapters: HighhX's existing model providers, OCR and a model-free embedding behind the
interfaces of :mod:`highhx.models.interfaces`."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import math
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.core.errors import ModelProviderError
from highhx.models.interfaces import Located, ModelReply, TextBox

if TYPE_CHECKING:
    from highhx.agent.model.base import ModelProvider
    from highhx.agent.model.capabilities import ModelCapabilities
    from highhx.execution.cancellation import CancellationToken


class ChatLanguageModel:
    """Any streaming ``ModelProvider`` (gateway, Anthropic, OpenAI, Gemini, OpenAI-compatible
    local servers) as a :class:`~highhx.models.interfaces.LanguageModel`."""

    def __init__(self, provider: ModelProvider, capabilities: ModelCapabilities) -> None:
        self.provider = provider
        self.capabilities = capabilities
        self.name = f"{capabilities.provider}:{capabilities.model or provider.default_model}"

    @property
    def local(self) -> bool:
        return self.capabilities.local

    def complete(
        self,
        system: str,
        prompt: str,
        *,
        images: Sequence[bytes] = (),
        max_tokens: int = 2000,
        cancel: CancellationToken | None = None,
    ) -> ModelReply:
        from highhx.agent.messages import Block, ImageBlock, Message, TextBlock
        from highhx.agent.model.base import ModelRequest
        from highhx.agent.streaming import Completed, TextDelta

        if images and not self.capabilities.vision:
            raise ModelProviderError(f"{self.name} cannot see images.")
        blocks: list[Block] = [
            ImageBlock("image/png", base64.b64encode(image).decode("ascii"), label=f"image {i + 1}")
            for i, image in enumerate(images)
        ]
        blocks.append(TextBlock(prompt))
        request = ModelRequest(
            system=system,
            messages=[Message("user", blocks)],
            model=self.capabilities.model or None,
            max_tokens=max_tokens,
        )
        text: list[str] = []
        completed: Completed | None = None
        for event in self.provider.stream(request, cancel=cancel):
            if isinstance(event, TextDelta):
                text.append(event.text)
            elif isinstance(event, Completed):
                completed = event
        answer = "".join(text) or (completed.message.text if completed is not None else "")
        usage = completed.usage if completed is not None else None
        return ModelReply(
            answer,
            (completed.model if completed is not None else "") or self.capabilities.model,
            usage.input_tokens if usage else 0,
            usage.output_tokens if usage else 0,
        )


LOCATE_SYSTEM = """\
You locate user-interface elements in screenshots. You never act; you only answer where things are.
Answer with one JSON object and nothing else."""

LOCATE_PROMPT = """\
The image is {width}x{height} pixels. Find: {query!r}.
Answer {{"found": true, "box": [x1, y1, x2, y2], "label": "<visible text or short description>",
"role": "<button|link|textbox|icon|text|other>", "confidence": <0..1>}}
or {{"found": false}} when it is not visible. {space}"""

DETECT_PROMPT = """\
The image is {width}x{height} pixels. List the interactive elements you can see (at most 60).
Answer {{"elements": [{{"box": [x1, y1, x2, y2], "label": "...", "role": "...", "confidence": 0..1}}]}}. {space}"""

_SPACES = {
    "pixels": "Coordinates are pixels of this image.",
    "relative1000": "Coordinates are on a 0-1000 grid over the image (0,0 top-left, 1000,1000 bottom-right).",
}


def _json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if match is None:
        raise ModelProviderError("The vision model did not answer with JSON.")
    try:
        value = json.loads(match.group(0))
    except ValueError as exc:
        raise ModelProviderError(f"The vision model's JSON is invalid: {exc}") from None
    if not isinstance(value, dict):
        raise ModelProviderError("The vision model's answer is not a JSON object.")
    return value


class ChatVisionModel:
    """A vision-capable :class:`ChatLanguageModel` that answers *where*, as JSON boxes. The
    answer is parsed and checked (inside the image, positive size); nothing it says is executed."""

    def __init__(self, language: ChatLanguageModel) -> None:
        if not language.capabilities.vision:
            raise ModelProviderError(f"{language.name} is not a vision model.")
        self.language = language
        self.name = language.name
        self.local = language.capabilities.local
        self.space = language.capabilities.coordinates if language.capabilities.coordinates in _SPACES else "pixels"

    def _box(self, raw: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
        if not isinstance(raw, list | tuple) or len(raw) != 4:
            return None
        try:
            x1, y1, x2, y2 = (float(v) for v in raw)
        except (TypeError, ValueError):
            return None
        if self.space == "relative1000":
            x1, x2 = x1 * width / 1000, x2 * width / 1000
            y1, y2 = y1 * height / 1000, y2 * height / 1000
        x1, x2 = sorted((max(0.0, min(width, x1)), max(0.0, min(width, x2))))
        y1, y2 = sorted((max(0.0, min(height, y1)), max(0.0, min(height, y2))))
        if x2 - x1 < 1 or y2 - y1 < 1:
            return None
        return round(x1), round(y1), round(x2 - x1), round(y2 - y1)

    def locate(self, image: bytes, query: str, *, cancel: CancellationToken | None = None) -> list[Located]:
        from highhx.perception.png import png_size

        width, height = png_size(image)
        prompt = LOCATE_PROMPT.format(width=width, height=height, query=query, space=_SPACES[self.space])
        reply = self.language.complete(LOCATE_SYSTEM, prompt, images=[image], max_tokens=400, cancel=cancel)
        data = _json_object(reply.text)
        if not data.get("found"):
            return []
        box = self._box(data.get("box"), width, height)
        if box is None:
            return []
        confidence = float(data.get("confidence") or 0.5)
        return [Located(str(data.get("label") or query), box, max(0.0, min(1.0, confidence)), str(data.get("role") or ""), reply)]

    def detect(self, image: bytes, *, cancel: CancellationToken | None = None) -> list[Located]:
        from highhx.perception.png import png_size

        width, height = png_size(image)
        prompt = DETECT_PROMPT.format(width=width, height=height, space=_SPACES[self.space])
        reply = self.language.complete(LOCATE_SYSTEM, prompt, images=[image], max_tokens=3000, cancel=cancel)
        data = _json_object(reply.text)
        out = []
        for item in data.get("elements") or []:
            if not isinstance(item, dict):
                continue
            box = self._box(item.get("box"), width, height)
            if box is None or not str(item.get("label") or "").strip():
                continue
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.5)))
            out.append(Located(str(item["label"]), box, confidence, str(item.get("role") or ""), reply))
        return out


class TesseractOCRModel:
    """Local tesseract as an :class:`~highhx.models.interfaces.OCRModel`."""

    name = "tesseract"

    def __init__(self) -> None:
        from highhx.computer.desktop import TesseractOCR

        self.reader = TesseractOCR()

    def available(self) -> bool:
        return self.reader.capability().available

    def read(self, image: bytes, *, cancel: CancellationToken | None = None) -> list[TextBox]:
        with tempfile.NamedTemporaryFile(prefix="highhx-ocr-", suffix=".png", delete=False) as handle:
            handle.write(image)
        path = Path(handle.name)
        try:
            observation = self.reader.read_image(path, cancel=cancel)
        finally:
            path.unlink(missing_ok=True)
        return [TextBox(e.name, tuple(e.bounds)) for e in observation.elements if e.bounds]  # type: ignore[arg-type]


_TOKEN = re.compile(r"[a-z0-9]+")


class HashingEmbedding:
    """A lexical embedding with no model: word and word-bigram features hashed into a fixed
    vector and L2-normalised. It finds trajectories that share *words* with a query. It does not
    understand synonyms, and a real embedding model can replace it through the same interface."""

    name = "hashing"

    def __init__(self, dimensions: int = 512) -> None:
        self.dimensions = dimensions

    def _features(self, text: str) -> list[str]:
        words = _TOKEN.findall(text.lower())
        return words + [f"{a} {b}" for a, b in itertools.pairwise(words)]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                vector[index] += 1.0 if digest[4] & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            out.append([v / norm for v in vector])
        return out


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
