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


Block = TextBlock | ToolCall | ToolResultBlock


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
    raise ValueError(f"unknown block type {kind!r}")


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
