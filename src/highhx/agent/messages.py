"""Provider-neutral conversation model.

Every provider adapter converts to and from these types, so the session,
history, the platform gateway and the tools never see a vendor format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant"]


@dataclass
class TextBlock:
    text: str
    type: Literal["text"] = "text"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal["tool_call"] = "tool_call"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "tool_call", "id": self.id, "name": self.name, "input": self.input}


@dataclass
class ToolResultBlock:
    tool_call_id: str
    content: str
    is_error: bool = False
    name: str = ""
    """Name of the tool that produced this result (Gemini needs it)."""
    type: Literal["tool_result"] = "tool_result"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
            "is_error": self.is_error,
            "name": self.name,
        }


@dataclass
class ImageBlock:
    """An image for a vision-capable model: a screenshot, an attached picture, a PDF page.

    ``data`` is base64. ``label`` says what it is ("screenshot c3 of the screen, 1280x800 pixels");
    providers send it as text beside the image, so the model knows which image a coordinate or a
    reference is about."""

    media_type: str
    data: str
    label: str = ""
    type: Literal["image"] = "image"

    def to_dict(self) -> dict[str, Any]:
        return {"type": "image", "media_type": self.media_type, "data": self.data, "label": self.label}


Block = TextBlock | ToolCall | ToolResultBlock | ImageBlock


def block_from_dict(data: dict[str, Any]) -> Block:
    kind = data.get("type")
    if kind == "text":
        return TextBlock(str(data.get("text") or ""))
    if kind == "tool_call":
        raw_input = data.get("input")
        return ToolCall(str(data["id"]), str(data["name"]), raw_input if isinstance(raw_input, dict) else {})
    if kind == "tool_result":
        return ToolResultBlock(
            str(data["tool_call_id"]),
            str(data.get("content") or ""),
            bool(data.get("is_error")),
            str(data.get("name") or ""),
        )
    if kind == "image":
        return ImageBlock(str(data.get("media_type") or "image/png"), str(data.get("data") or ""), str(data.get("label") or ""))
    raise ValueError(f"unknown block type {kind!r}")


MAX_IMAGES = 4
"""Images kept in a request: older screenshots are replaced by their labels (size, cost, privacy)."""


def limit_images(messages: list[Message], keep: int = MAX_IMAGES) -> list[Message]:
    """``messages`` with only the newest ``keep`` images; each older one becomes a text note."""
    seen = 0
    out: list[Message] = []
    for message in reversed(messages):
        if not message.images:
            out.append(message)
            continue
        blocks: list[Block] = []
        for block in reversed(message.blocks):
            if isinstance(block, ImageBlock):
                seen += 1
                if seen > keep:
                    block = TextBlock(f"[earlier image no longer shown: {block.label or block.media_type}]")
            blocks.append(block)
        out.append(Message(message.role, list(reversed(blocks)), message.provider_state))
    return list(reversed(out))


def without_images(message: Message) -> Message:
    """``message`` with each image replaced by its label — what is stored and synced: screenshots
    and attached pictures reach the model for the turn, never the saved history."""
    if not message.images:
        return message
    blocks: list[Block] = [
        TextBlock(f"[image: {b.label or b.media_type}]") if isinstance(b, ImageBlock) else b for b in message.blocks
    ]
    return Message(message.role, blocks, message.provider_state)


@dataclass
class Message:
    role: Role
    blocks: list[Block] = field(default_factory=list)
    provider_state: dict[str, Any] = field(default_factory=dict)
    """Opaque, provider-specific data that must be replayed verbatim (e.g. Anthropic
    thinking blocks, which are only valid when echoed back unchanged)."""

    @classmethod
    def user(cls, text: str) -> Message:
        return cls("user", [TextBlock(text)])

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [b for b in self.blocks if isinstance(b, ToolCall)]

    @property
    def tool_results(self) -> list[ToolResultBlock]:
        return [b for b in self.blocks if isinstance(b, ToolResultBlock)]

    @property
    def images(self) -> list[ImageBlock]:
        return [b for b in self.blocks if isinstance(b, ImageBlock)]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "blocks": [b.to_dict() for b in self.blocks]}
        if self.provider_state:
            data["provider_state"] = self.provider_state
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        role = data.get("role")
        if role not in ("user", "assistant"):
            raise ValueError(f"invalid role {role!r}")
        blocks = [block_from_dict(b) for b in data.get("blocks") or [] if isinstance(b, dict)]
        state = data.get("provider_state")
        return cls(role, blocks, dict(state) if isinstance(state, dict) else {})


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Usage:
        data = data or {}
        return cls(
            int(data.get("input_tokens") or 0),
            int(data.get("output_tokens") or 0),
            int(data.get("cache_read_tokens") or 0),
            int(data.get("cache_write_tokens") or 0),
        )


StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "pause", "error"]
