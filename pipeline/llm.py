"""Claude client wrapper, over either Amazon Bedrock or the first-party API.

Two callers: stage 5 (translate) and stage 2 (extract). Both want a validated object back,
so `structured()` is the single entry point — it pins a JSON schema and validates the
result into a Pydantic model, which means a malformed response fails here rather than
three stages downstream.

**Bedrock is the default provider.** It uses the `AnthropicBedrockMantle` client (the
Messages-API Bedrock endpoint) rather than the legacy `InvokeModel` path, so the request
shape is nearly identical to the first-party API. Three things differ, all handled here
and in `Settings.resolved_model()`:

- Model IDs carry an `anthropic.` prefix on Bedrock (`anthropic.claude-opus-5`).
- The API key is a *short-term* credential that expires within 12 hours, so it is read
  through `pipeline/llm_key.py` on every call rather than captured at import.
- **Structured outputs are not available.** This endpoint rejects both
  `output_config.format` and `strict: true` on a tool with
  `400 invalid_request_error: Extra inputs are not permitted` — measured against
  `anthropic.claude-opus-5` in us-east-1, not assumed. So the schema is enforced the way
  it was before structured outputs existed: one tool carrying the schema, plus
  `tool_choice` forcing it. `output_config.effort` alone *is* accepted, so effort still
  works on both providers.

Both paths end at `schema.model_validate*`, so a provider that quietly drifts from its
schema fails here either way — the tool path is a weaker guarantee from the API, not a
weaker guarantee from this module.

The client is built per call, not cached, because the key rotates — a long-lived client
would keep using the key it was constructed with and start failing after the rotation.
Client construction is cheap next to a model request.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from pipeline.config import Settings
from pipeline.llm_key import LLMKeyError, require_key

log = logging.getLogger(__name__)

# Name of the schema-carrying tool on the forced-tool path. Descriptive because the model
# sees it: an opaque name makes the one available tool look like a trap.
_EMIT_TOOL = "emit_result"


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


def schema_kwargs(schema: type[BaseModel], settings: Settings, *, effort: str) -> dict[str, Any]:
    """The request keywords that pin the response to `schema` on this provider.

    Split out so the PDF path in `extract.py` builds the same request as the text path
    below — the two shapes drifted apart once already, and only the text one was tested.
    """
    json_schema = _strict_schema(schema)

    if settings.llm_provider == "bedrock":
        # No output_config.format here — see the module docstring. `effort` is fine.
        return {
            "output_config": {"effort": effort},
            "tools": [
                {
                    "name": _EMIT_TOOL,
                    "description": f"Return the result as a {schema.__name__} object.",
                    "input_schema": json_schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": _EMIT_TOOL},
        }

    return {
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": json_schema},
        }
    }


def parse_response[T: BaseModel](response: object, schema: type[T], what: str) -> T:
    """Validate a response into `schema`, whichever request shape produced it.

    Accepts both the tool_use block (Bedrock) and the JSON text block (first-party), so
    switching providers doesn't change the caller.
    """
    # A refusal returns HTTP 200 with an empty or partial content array, so checking
    # stop_reason before reading content is required, not defensive.
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        detail = getattr(details, "explanation", None) or "no explanation given"
        raise LLMError(f"model declined {what}: {detail}")

    content = getattr(response, "content", [])
    tool_use = next(
        (block for block in content if getattr(block, "type", None) == "tool_use"), None
    )
    if tool_use is not None:
        try:
            return schema.model_validate(tool_use.input)
        except ValidationError as exc:
            raise LLMError(f"{what}: tool input did not match {schema.__name__}: {exc}") from exc

    text = next((block.text for block in content if getattr(block, "type", None) == "text"), None)
    if not text:
        stop = getattr(response, "stop_reason", "?")
        # Hitting max_tokens mid-object is the common cause and looks nothing like a bug
        # in the schema, so name it rather than reporting an empty response.
        hint = " (ran out of max_tokens mid-object)" if stop == "max_tokens" else ""
        raise LLMError(f"{what}: no tool_use or text block in response (stop_reason={stop}){hint}")

    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise LLMError(f"{what}: response did not match {schema.__name__}: {exc}") from exc


async def structured[T: BaseModel](
    prompt: str,
    schema: type[T],
    settings: Settings,
    *,
    system: str | None = None,
    max_tokens: int = 16000,
    effort: str = "medium",
) -> T:
    """One schema-pinned call, validated into `schema`.

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
            **schema_kwargs(schema, settings, effort=effort),
        )
    except Exception as exc:  # noqa: BLE001 - surface any SDK failure as one error type
        raise LLMError(_call_failure_message(model, settings, exc)) from exc

    return parse_response(response, schema, "the request")


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
        age = info.get("age_hours")

        # Only claim an age when one is actually known. An env-supplied key reports
        # age_hours=None because `set_at` is just when the process read it — the previous
        # version printed "most likely expired ... and the loaded one is 0.0h old", which
        # contradicts itself in one sentence. The key was three days old, seeded by a deploy.
        if age is None:
            provenance = (
                f"The loaded key (…{suffix}) came from the environment (BEDROCK_API_KEY at "
                "deploy time), so its real age is unknown — it can easily predate this "
                "container by days."
            )
        else:
            provenance = f"The loaded key (…{suffix}) was set {age}h ago."

        return (
            f"Bedrock rejected the key (HTTP {status}). It most likely expired — short-term "
            f"keys last at most 12 hours. {provenance}\n"
            "Mint a new key, then run:  python scripts/set_bedrock_key.py <new-key>\n"
            "That updates this machine and the deployed service without a redeploy.\n"
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
