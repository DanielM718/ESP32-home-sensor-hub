from __future__ import annotations

import json
from dataclasses import replace

import pytest

from butters.assistant_config import load_assistant_settings
from butters.cloud.general import OpenAIGeneralReasoner
from butters.cloud.model import CloudReasonerError


class Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


def _settings():
    base = load_assistant_settings().cloud
    return replace(base, enabled=True, allow_paid_calls=True)


def test_general_reasoner_builds_bounded_responses_request_and_parses_usage() -> None:
    observed = {}

    def opener(request, **_kwargs):
        observed["url"] = request.full_url
        observed["body"] = json.loads(request.data)
        observed["authorization"] = request.headers["Authorization"]
        return Response(
            {
                "id": "resp_safe_1",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Observed humidity differs; airflow is a hypothesis."}],
                    }
                ],
                "usage": {
                    "input_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens": 30,
                    "output_tokens_details": {"reasoning_tokens": 8},
                },
            }
        )

    settings = _settings()
    reasoner = OpenAIGeneralReasoner(settings, api_key="fake-key", opener=opener)
    result = reasoner.reason(
        text="Why might box three stay humid?",
        context=({"role": "assistant", "content": "bounded observation"},),
        tools=(),
        model=settings.terra_model,
        effort="high",
        max_output_tokens=400,
    )

    assert observed["url"].endswith("/v1/responses")
    assert observed["body"]["store"] is False
    assert observed["body"]["parallel_tool_calls"] is False
    assert observed["body"]["reasoning"] == {"effort": "high"}
    assert observed["body"]["max_output_tokens"] == 400
    assert result.response_id == "resp_safe_1"
    assert result.usage.input_tokens == 120
    assert result.usage.cached_tokens == 20
    assert result.usage.reasoning_tokens == 8
    assert "fake-key" not in repr(result)


def test_general_reasoner_parses_one_typed_tool_call_and_rejects_unknown_model() -> None:
    def opener(_request, **_kwargs):
        return Response(
            {
                "id": "resp_safe_2",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_safe_1",
                        "name": "get_sensor_value",
                        "arguments": json.dumps({"entity": "filament_box_3", "metric": "humidity"}),
                    }
                ],
                "usage": {},
            }
        )

    settings = _settings()
    reasoner = OpenAIGeneralReasoner(settings, api_key="fake-key", opener=opener)
    result = reasoner.reason(
        text="inspect humidity",
        context=(),
        tools=({"type": "function", "name": "get_sensor_value"},),
        model=settings.terra_model,
        effort="medium",
        max_output_tokens=200,
    )

    assert result.tool_request is not None
    assert result.tool_request.name == "get_sensor_value"
    assert result.tool_request.arguments["entity"] == "filament_box_3"
    with pytest.raises(CloudReasonerError) as denied:
        reasoner.reason(
            text="test",
            context=(),
            tools=(),
            model="unpriced-model",
            effort="medium",
            max_output_tokens=200,
        )
    assert denied.value.code == "model_denied"
