"""What we send to each provider, and what we accept back.

These exist because of a real outage-in-waiting: every LLM call was built with
`output_config.format` (structured outputs) and `strict: true`, which the Bedrock
Messages endpoint rejects with `400 ... Extra inputs are not permitted`. The whole suite
passed anyway — the tests mocked `structured()` itself, so nothing ever inspected the
request. The pipeline would have failed on its first real translate call.

So these assert on the *outgoing keyword arguments*, not on a mocked return value. Nothing
here reaches the network.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from pipeline.config import Settings
from pipeline.llm import (
    LLMError,
    _strict_schema,
    parse_response,
    schema_kwargs,
)


class Sample(BaseModel):
    name: str
    scores: list[int] = Field(default_factory=list)


def bedrock() -> Settings:
    return Settings(llm_provider="bedrock")


def first_party() -> Settings:
    return Settings(llm_provider="anthropic")


# --- the request ---------------------------------------------------------------


def test_bedrock_never_sends_structured_outputs() -> None:
    """`output_config.format` is a 400 on the Bedrock Messages endpoint.

    Measured against anthropic.claude-opus-5 in us-east-1:
        400 invalid_request_error: output_config.format: Extra inputs are not permitted
    """
    kwargs = schema_kwargs(Sample, bedrock(), effort="medium")

    assert "format" not in kwargs["output_config"]


def test_bedrock_never_sends_strict_on_a_tool() -> None:
    """Same endpoint, same error, different field:
    400 invalid_request_error: tools.0.custom.strict: Extra inputs are not permitted
    """
    kwargs = schema_kwargs(Sample, bedrock(), effort="medium")

    for tool in kwargs["tools"]:
        assert "strict" not in tool


def test_bedrock_forces_the_schema_tool() -> None:
    """Without `tool_choice` the model may answer in prose and the schema buys nothing."""
    kwargs = schema_kwargs(Sample, bedrock(), effort="medium")

    (tool,) = kwargs["tools"]
    assert kwargs["tool_choice"] == {"type": "tool", "name": tool["name"]}
    assert tool["input_schema"] == _strict_schema(Sample)
    # The model reads the description; it should say what the tool is for.
    assert "Sample" in tool["description"]


def test_effort_survives_on_both_providers() -> None:
    """Effort is the one part of output_config Bedrock does accept. Dropping the whole
    object to avoid `format` would have silently downgraded extraction to the default."""
    assert schema_kwargs(Sample, bedrock(), effort="high")["output_config"]["effort"] == "high"
    assert schema_kwargs(Sample, first_party(), effort="high")["output_config"]["effort"] == "high"


def test_first_party_uses_structured_outputs_not_a_tool() -> None:
    """The stronger guarantee is available there, so use it — and don't burn a tool slot."""
    kwargs = schema_kwargs(Sample, first_party(), effort="medium")

    fmt = kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] == _strict_schema(Sample)
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


def test_every_object_in_the_schema_forbids_extra_properties() -> None:
    """Required by structured outputs, and it keeps the tool path honest too. Nested
    objects are the ones that get missed."""
    schema = _strict_schema(Sample)
    seen = 0

    def walk(node: object) -> None:
        nonlocal seen
        if isinstance(node, dict):
            if node.get("type") == "object":
                seen += 1
                assert node.get("additionalProperties") is False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    assert seen >= 1


def test_schema_carries_no_keyword_the_api_rejects() -> None:
    text = json.dumps(_strict_schema(Sample))

    for keyword in ("minLength", "maxItems", "pattern", "exclusiveMinimum"):
        assert keyword not in text


# --- the response --------------------------------------------------------------


class _Block:
    def __init__(self, type: str, **kw: object) -> None:  # noqa: A002 - mirrors the SDK field
        self.type = type
        for key, value in kw.items():
            setattr(self, key, value)


class _Response:
    def __init__(self, content: list[_Block], stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = None


def test_parses_a_tool_use_block() -> None:
    """The Bedrock shape."""
    response = _Response(
        [_Block("tool_use", input={"name": "Ada", "scores": [1, 2]})], stop_reason="tool_use"
    )

    assert parse_response(response, Sample, "x").name == "Ada"


def test_parses_a_json_text_block() -> None:
    """The first-party shape. Both providers must reach the same object."""
    response = _Response([_Block("text", text='{"name": "Ada", "scores": [1, 2]}')])

    assert parse_response(response, Sample, "x").scores == [1, 2]


def test_thinking_block_before_the_tool_use_is_skipped() -> None:
    """Opus 5 thinks by default, so the answer is rarely content[0]. Indexing content[0]
    would read the thinking block and fail on every real call."""
    response = _Response(
        [
            _Block("thinking", thinking="considering..."),
            _Block("tool_use", input={"name": "Ada"}),
        ],
        stop_reason="tool_use",
    )

    assert parse_response(response, Sample, "x").name == "Ada"


def test_refusal_is_reported_as_a_refusal() -> None:
    """A refusal is HTTP 200 with no usable content, so it must be checked before the
    content array — otherwise it surfaces as a confusing parse failure."""
    response = _Response([], stop_reason="refusal")

    with pytest.raises(LLMError, match="declined"):
        parse_response(response, Sample, "translating")


def test_truncation_says_it_ran_out_of_tokens() -> None:
    """A schema-shaped object cut off mid-write is the most likely real failure, and
    "no tool_use or text block" sends you looking at the schema instead of max_tokens."""
    response = _Response([], stop_reason="max_tokens")

    with pytest.raises(LLMError, match="max_tokens"):
        parse_response(response, Sample, "extracting")


def test_schema_mismatch_names_the_model_and_the_context() -> None:
    response = _Response([_Block("tool_use", input={"scores": "not a list"})], "tool_use")

    with pytest.raises(LLMError, match="reading report.pdf.*Sample"):
        parse_response(response, Sample, "reading report.pdf")
