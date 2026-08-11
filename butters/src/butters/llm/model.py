"""Engine-neutral types for an untrusted semantic-routing model."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProposalKind(str, Enum):
    TOOL = "tool"
    CLARIFICATION = "clarification"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    executable: bool = True

    def as_chat_tool(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolProposal:
    kind: ProposalKind
    skill: str | None = None
    arguments: dict[str, object] = field(default_factory=dict)
    clarification_topic: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LanguageModelResult:
    proposal: ToolProposal
    model: str
    elapsed_seconds: float
    raw_output: str = ""
    prompt_tokens: int | None = None
    generated_tokens: int | None = None
    prompt_tokens_per_second: float | None = None
    generated_tokens_per_second: float | None = None


class LanguageModelError(RuntimeError):
    """The isolated inference worker was unavailable or returned no response."""


class LanguageModel(ABC):
    @abstractmethod
    def propose_tools(
        self,
        request: str,
        available_tools: tuple[ToolDefinition, ...],
        context: tuple[str, ...] = (),
    ) -> LanguageModelResult:
        """Return an untrusted proposal; this method never executes a skill."""

    def close(self) -> None:
        """Release client-owned resources without affecting assistant services."""
