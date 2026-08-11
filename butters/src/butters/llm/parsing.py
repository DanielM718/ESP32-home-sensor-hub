"""Strict, non-executing normalization of model-specific tool-call output."""

from __future__ import annotations

import ast
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from butters.llm.model import ProposalKind, ToolProposal

LFM_TOOL_CALL = re.compile(
    r"\A\s*(?:<\|tool_call_start\|>)?\s*(.*?)\s*"
    r"(?:<\|tool_call_end\|>)?\s*\Z",
    re.DOTALL,
)
CLARIFICATION_TOPICS = frozenset({"sensor", "filament_box", "metric", "request"})


def parse_chat_completion(payload: Mapping[str, Any]) -> tuple[ToolProposal, str]:
    """Normalize one llama.cpp OpenAI-compatible completion, failing closed."""
    choices = payload.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)):
        return _invalid("response has no choices"), ""
    if len(choices) != 1 or not isinstance(choices[0], Mapping):
        return _invalid("response must contain exactly one choice"), ""
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        return _invalid("choice has no message"), ""
    content_value = message.get("content")
    content = content_value if isinstance(content_value, str) else ""
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, Sequence) or isinstance(tool_calls, (str, bytes)):
            return _invalid("tool_calls is not a list"), content
        if len(tool_calls) != 1 or not isinstance(tool_calls[0], Mapping):
            return _invalid("exactly one tool call is required"), content
        function = tool_calls[0].get("function")
        if not isinstance(function, Mapping):
            return _invalid("tool call has no function"), content
        name = function.get("name")
        arguments = function.get("arguments", {})
        if not isinstance(name, str) or not name:
            return _invalid("tool name is missing"), content
        parsed_arguments = _arguments(arguments)
        if parsed_arguments is None:
            return _invalid("tool arguments are not a JSON object"), content
        return proposal_from_call(name, parsed_arguments), content
    if not content.strip():
        return _invalid("model returned neither a tool call nor content"), content
    return parse_model_text(content), content


def parse_model_text(text: str) -> ToolProposal:
    """Parse exact JSON or official LFM2 Pythonic call syntax without eval."""
    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return parse_lfm_tool_text(stripped)
    if not isinstance(value, Mapping) or set(value) != {"skill", "arguments"}:
        return _invalid("JSON proposal must contain only skill and arguments")
    name = value.get("skill")
    arguments = value.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, Mapping):
        return _invalid("JSON proposal types are invalid")
    if not _string_keys(arguments):
        return _invalid("argument names must be strings")
    return proposal_from_call(name, dict(arguments))


def parse_lfm_tool_text(text: str) -> ToolProposal:
    """Parse LFM2's official ``[function(arg=value)]`` representation safely."""
    has_start = text.startswith("<|tool_call_start|>")
    has_end = text.endswith("<|tool_call_end|>")
    if has_start != has_end:
        return _invalid("LFM tool-call markers are incomplete")
    matched = LFM_TOOL_CALL.fullmatch(text)
    if matched is None:
        return _invalid("LFM tool-call markers are malformed")
    body = matched.group(1)
    try:
        expression = ast.parse(body, mode="eval").body
    except (SyntaxError, ValueError):
        return _invalid("LFM tool-call syntax is malformed")
    if not isinstance(expression, ast.List) or len(expression.elts) != 1:
        return _invalid("exactly one LFM tool call is required")
    call = expression.elts[0]
    if (
        not isinstance(call, ast.Call)
        or not isinstance(call.func, ast.Name)
        or call.args
        or any(keyword.arg is None for keyword in call.keywords)
    ):
        return _invalid("LFM output is not a simple named keyword call")
    arguments: dict[str, object] = {}
    try:
        for keyword in call.keywords:
            assert keyword.arg is not None
            if keyword.arg in arguments:
                return _invalid("duplicate LFM keyword argument")
            value = ast.literal_eval(keyword.value)
            if not _literal_value(value):
                return _invalid("LFM argument is not a primitive literal")
            arguments[keyword.arg] = value
    except (ValueError, TypeError, SyntaxError):
        return _invalid("LFM argument is not a literal")
    return proposal_from_call(call.func.id, arguments)


def proposal_from_call(name: str, arguments: Mapping[str, object]) -> ToolProposal:
    if not _string_keys(arguments) or not all(_literal_value(v) for v in arguments.values()):
        return _invalid("arguments must contain primitive JSON values")
    values = dict(arguments)
    if name == "clarify_request":
        if set(values) != {"topic"} or values.get("topic") not in CLARIFICATION_TOPICS:
            return _invalid("clarification topic is invalid")
        return ToolProposal(
            ProposalKind.CLARIFICATION,
            clarification_topic=str(values["topic"]),
        )
    if name == "unsupported_request":
        if values:
            return _invalid("unsupported_request accepts no arguments")
        return ToolProposal(ProposalKind.UNSUPPORTED)
    return ToolProposal(ProposalKind.TOOL, skill=name, arguments=values)


def _arguments(value: object) -> dict[str, object] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, Mapping) or not _string_keys(value):
        return None
    return dict(value)


def _string_keys(value: Mapping[object, object]) -> bool:
    return all(isinstance(key, str) for key in value)


def _literal_value(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if value is None or isinstance(value, (str, int, bool)):
        return True
    if isinstance(value, list):
        return all(_literal_value(item) for item in value)
    return False


def _invalid(reason: str) -> ToolProposal:
    return ToolProposal(ProposalKind.INVALID, error=reason)
