"""Claude client wrapper, over either Amazon Bedrock or the first-party API.

Two callers: stage 5 (translate) and stage 2 (extract). Both want structured output, so
`structured()` is the single entry point — it forces a JSON schema and validates the
result into a Pydantic model, which means a malformed response fails here rather than
three stages downstream.

**Bedrock is the default provider.** It uses the `AnthropicBedrockMantle` client (the
Messages-API Bedrock endpoint) rather than the legacy `InvokeModel` path, so the request
shape here is identical to the first-party API. Two things differ and are handled by
`Settings.resolved_model()` and the client factory below:

- Model IDs carry an `anthropic.` prefix on Bedrock (`anthropic.claude-opus-5`).
- The API key is a *short-term* credential that expires within 12 hours, so it is read
  through `pipeline/llm_key.py` on every call rather than captured at import.

The client is built per call, not cached, because the key rotates — a long-lived client
would keep using the key it was constructed with and start failing after the rotation.
Client construction is cheap next to a model request.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ValidationError

from pipeline.config import Settings
from pipeline.llm_key import LLMKeyError, require_key

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def _client(settings: Settings):  # noqa: ANN202 - anthropic types are import-time optional
    """A client for the configured provider, using the key in effect right now."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise LLMError("anthropic is not installed. Run: pip install anthropic") from exc

    if settings.llm_provider == "bedrock":
        try:
            key = require_key(settings)
        except LLMKeyError as exc:
            # Re-raised as LLMError so callers keep their single except clause, with the
            # remediation text from llm_key preserved.
            raise LLMError(str(exc)) from exc
        # Passing api_key explicitly selects API-key auth over SigV4 (see the SDK's
        # resolve_auth_mode); aws_region decides the endpoint host.
        return anthropic.AsyncAnthropicBedrockMantle(
            api_key=key,
            aws_region=settings.aws_region,
        )

    key = settings.anthropic_api_key.get_secret_value()
    if not key:
        raise LLMError("ANTHROPIC_API_KEY is empty")
    return anthropic.AsyncAnthropic(api_key=key)


async def structured[T: BaseModel](
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
    model = settings.resolved_model()
    try:
        response = await client.messages.create(
            model=model,
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
        raise LLMError(_call_failure_message(model, settings, exc)) from exc

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


def _call_failure_message(model: str, settings: Settings, exc: Exception) -> str:
    """Turn an SDK exception into something actionable.

    An expired short-term key surfaces as a 401 mid-run, which reads as a config error
    unless the message says otherwise — so name the rotation command in that case rather
    than leaving "401 Unauthorized" for someone to interpret at 3am.
    """
    status = getattr(exc, "status_code", None)
    if settings.llm_provider == "bedrock" and status in (401, 403):
        from pipeline.llm_key import status as key_status

        info = key_status(settings)
        suffix = info.get("suffix", "????")
        age = info.get("age_hours", "?")
        return (
            f"Bedrock rejected the key (HTTP {status}). It most likely expired — short-term "
            f"keys last at most 12 hours, and the loaded one (…{suffix}) is {age}h old.\n"
            "Mint a new key, then run:  python scripts/set_bedrock_key.py <ABSK...>\n"
            f"(underlying error: {exc})"
        )
    return f"{model} call failed: {exc}"


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
