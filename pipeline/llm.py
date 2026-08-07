"""Anthropic client wrapper.

Two callers: stage 5 (translate) and stage 2 (extract). Both want structured output, so
`structured()` is the single entry point — it forces a JSON schema and validates the
result into a Pydantic model, which means a malformed response fails here rather than
three stages downstream.
"""

from __future__ import annotations

import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from pipeline.config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


def _client(settings: Settings):  # noqa: ANN202 - anthropic types are import-time optional
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise LLMError("anthropic is not installed. Run: pip install anthropic") from exc

    key = settings.anthropic_api_key.get_secret_value()
    if not key:
        raise LLMError("ANTHROPIC_API_KEY is empty")
    return anthropic.AsyncAnthropic(api_key=key)


async def structured(
    prompt: str,
    schema: type[T],
    settings: Settings,
    *,
    system: str | None = None,
    max_tokens: int = 16000,
    effort: str = "medium",
) -> T:
    """One structured-output call, validated into `schema`.

    Effort defaults to medium: translating resume bullets and pulling facts out of a
    project doc are both bounded tasks, and the higher tiers mostly buy deliberation
    this doesn't need. Callers can raise it per call.
    """
    client = _client(settings)
    try:
        response = await client.messages.create(
            model=settings.llm_model,
            max_tokens=max_tokens,
            system=system or "",
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": effort,
                "format": {
                    "type": "json_schema",
                    "schema": _strict_schema(schema),
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - surface any SDK failure as one error type
        raise LLMError(f"{settings.llm_model} call failed: {exc}") from exc

    # A refusal returns HTTP 200 with an empty or partial content array, so checking
    # stop_reason before reading content is required, not defensive.
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", None) or "no explanation given"
        raise LLMError(f"model declined the request: {detail}")

    text = next((block.text for block in response.content if block.type == "text"), None)
    if not text:
        raise LLMError(f"no text block in response (stop_reason={response.stop_reason})")

    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise LLMError(f"response did not match {schema.__name__}: {exc}") from exc


def _strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic's JSON schema, adjusted for the structured-outputs constraints.

    Structured outputs require `additionalProperties: false` on every object and reject
    the numeric/string constraint keywords Pydantic emits. Stripping them here keeps the
    models free to carry those constraints for local validation.
    """
    schema = model.model_json_schema()
    _strip_unsupported(schema)
    return schema


_UNSUPPORTED_KEYWORDS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "format",
)


def _strip_unsupported(node: object) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for keyword in _UNSUPPORTED_KEYWORDS:
            node.pop(keyword, None)
        for value in node.values():
            _strip_unsupported(value)
    elif isinstance(node, list):
        for item in node:
            _strip_unsupported(item)
