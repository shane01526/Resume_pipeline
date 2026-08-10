"""Stage 2: source document → candidate resume entries.

The prompt's job is to make the model *cautious*. A false positive here costs you a review
row to reject, but a hallucinated metric that slips through review ends up on a resume you
send to employers — so the instructions repeatedly push toward "only what the document
says" and toward marking uncertainty as `Low` confidence rather than guessing.

Candidates are separate types from `pipeline.models` on purpose: these are unverified
claims with no Notion identity yet, and giving them the same type as approved content
would make it easy to accidentally render one.
"""

from __future__ import annotations

import base64
import logging

from pydantic import BaseModel, Field

from pipeline.config import Settings
from pipeline.ingest import Source
from pipeline.llm import LLMError, _client
from pipeline.models import Confidence

log = logging.getLogger(__name__)

SYSTEM = """\
You extract résumé-ready facts from a work document. The output is reviewed by a human \
before it reaches a résumé, so your job is accuracy, not completeness.

Hard rules:
- Extract ONLY what the document states. Never infer a metric, a date, or a technology \
that is not written down.
- Never invent numbers. If the document says "improved latency", do not write "improved \
latency by 40%".
- If a date is absent, leave it null rather than guessing from context.
- Set confidence to "Low" for anything you are unsure about, and say why in \
`uncertainty`. A flagged uncertain item is useful; a confident wrong one is harmful.
- Prefer the document's own wording for technical detail. Do not upgrade "helped with" \
into "led".
- Skip anything that is not a professional accomplishment: meeting logistics, personal \
notes, boilerplate.

Bullet style:
- One accomplishment per bullet, past tense, starting with a verb.
- Lead with what was built or changed, then the mechanism, then the measured outcome if \
the document gives one.
- 15-30 words. A bullet that needs two sentences is two bullets.
- Keep technical terms as the document writes them (LLM, RAG, AWS Lambda, LangGraph).

Deciding the type:
- `experience` — a role held over a period at an organization.
- `project` — a discrete piece of work, even if done within a role.
- `skill` — a tool or technology the document shows was actually used.
- `publication` — a paper, poster, or talk.
A document usually yields one project plus a few skills. Do not split one project into \
several just to produce more entries."""


class CandidateExperience(BaseModel):
    role: str = Field(description="Job title as the document states it")
    organization: str
    location: str | None = None
    start: str | None = Field(default=None, description="YYYY-MM-DD, or null if not stated")
    end: str | None = Field(default=None, description="YYYY-MM-DD, or null if ongoing/unstated")
    bullets: list[str]
    tags: list[str] = Field(default_factory=list)


class CandidateProject(BaseModel):
    name: str
    affiliation: str | None = None
    context: str | None = Field(default=None, description="Venue or organization, if stated")
    date: str | None = Field(default=None, description="YYYY-MM-DD, or null")
    bullets: list[str]
    tags: list[str] = Field(default_factory=list)


class CandidateSkill(BaseModel):
    name: str
    category: str = Field(
        description="One of: Languages, Programming, Cloud & Infra, Frameworks, Tools, Certificates"
    )
    detail: str | None = None


class CandidatePublication(BaseModel):
    title: str
    venue: str | None = None
    date: str | None = None
    authors: str | None = None


class Extraction(BaseModel):
    """Everything found in one document."""

    experiences: list[CandidateExperience] = Field(default_factory=list)
    projects: list[CandidateProject] = Field(default_factory=list)
    skills: list[CandidateSkill] = Field(default_factory=list)
    publications: list[CandidatePublication] = Field(default_factory=list)
    confidence: Confidence = Field(
        description="Overall confidence. Low if the document was ambiguous or fragmentary."
    )
    uncertainty: str | None = Field(
        default=None,
        description="What you were unsure about, for the human reviewer. Null if nothing.",
    )

    def total(self) -> int:
        return (
            len(self.experiences) + len(self.projects) + len(self.skills) + len(self.publications)
        )


async def extract_from_source(source: Source, settings: Settings) -> Extraction | None:
    """Extract candidates from one source. Returns None if the call failed."""
    prompt = (
        f"Extract résumé-ready entries from this document.\n\n"
        f"Filename: {source.name}\n"
        f"(The filename often carries the date and project — use it, but do not invent "
        f"detail it merely hints at.)\n"
    )

    try:
        # PDFs go to the model as documents rather than extracted text: layout is where a
        # report's structure lives, and Claude reads it better than a local extractor.
        if source.is_pdf:
            extraction = await _extract_pdf(source, prompt, settings)
        else:
            extraction = await _extract_text(source, prompt, settings)
    except LLMError as exc:
        log.warning("extraction failed for %s: %s", source.name, exc)
        return None

    log.info(
        "%s → %d candidate(s), confidence=%s%s",
        source.name,
        extraction.total(),
        extraction.confidence.value,
        f" ({extraction.uncertainty})" if extraction.uncertainty else "",
    )
    return extraction


async def _extract_text(source: Source, prompt: str, settings: Settings) -> Extraction:
    from pipeline.llm import structured

    assert source.text is not None
    return await structured(
        f"{prompt}\n---\n{source.text}\n---",
        Extraction,
        settings,
        system=SYSTEM,
        # Higher than the translate default: judging what counts as a resume-worthy
        # accomplishment, and how confident to be, is the part worth thinking about.
        effort="high",
    )


async def _extract_pdf(source: Source, prompt: str, settings: Settings) -> Extraction:
    """PDF path, which needs a document content block rather than a text prompt."""
    from pipeline.llm import _strict_schema

    assert source.pdf_bytes is not None
    client = _client(settings)
    # Provider-correct ID: Bedrock needs the `anthropic.` prefix, first-party rejects it.
    model = settings.resolved_model()

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": _strict_schema(Extraction)},
            },
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": base64.standard_b64encode(source.pdf_bytes).decode(),
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        from pipeline.llm import _call_failure_message

        # Shared handler, so an expired Bedrock key gives the same rotation hint here.
        raise LLMError(f"{source.name}: {_call_failure_message(model, settings, exc)}") from exc

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", None) or "no explanation"
        raise LLMError(f"model declined to read {source.name}: {detail}")

    text = next((block.text for block in response.content if block.type == "text"), None)
    if not text:
        raise LLMError(f"no text block for {source.name} (stop_reason={response.stop_reason})")

    return Extraction.model_validate_json(text)
