"""The four model interfaces. Anything with these methods can be plugged in (dependency
injection): a vendor SDK, a local server, a plugin, or a test double."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from highhx.execution.cancellation import CancellationToken


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    """In US dollars, when the provider reports it (otherwise unknown, never estimated)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost": self.cost,
        }


@dataclass(frozen=True)
class Located:
    """Where a vision model says something is, in the image's pixels."""

    label: str
    box: tuple[int, int, int, int]
    """x, y, width, height."""
    confidence: float = 0.5
    role: str = ""
    reply: ModelReply | None = field(default=None, compare=False)

    @property
    def point(self) -> tuple[int, int]:
        x, y, w, h = self.box
        return round(x + w / 2), round(y + h / 2)


@dataclass(frozen=True)
class TextBox:
    text: str
    box: tuple[int, int, int, int]
    confidence: float = 1.0


@runtime_checkable
class LanguageModel(Protocol):
    name: str

    def complete(
        self,
        system: str,
        prompt: str,
        *,
        images: Sequence[bytes] = (),
        max_tokens: int = 2000,
        cancel: CancellationToken | None = None,
    ) -> ModelReply: ...


@runtime_checkable
class VisionModel(Protocol):
    name: str
    local: bool
    """The model runs on this computer (screenshots sent to it stay here)."""

    def locate(
        self, image: bytes, query: str, *, cancel: CancellationToken | None = None
    ) -> list[Located]: ...

    def detect(self, image: bytes, *, cancel: CancellationToken | None = None) -> list[Located]: ...


@runtime_checkable
class EmbeddingModel(Protocol):
    name: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@runtime_checkable
class OCRModel(Protocol):
    name: str

    def read(self, image: bytes, *, cancel: CancellationToken | None = None) -> list[TextBox]: ...
