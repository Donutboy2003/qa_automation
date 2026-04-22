# core/page_analyzer.py
#
# Analyze a single page's text content using an LLM with a configurable
# output schema. Supports summary, CTA, theme, audience, keywords,
# relevance classification, and audience classification — all optional and
# independently toggled.
#
# Batch usage:
#   from core.page_analyzer import analyze_page, AnalysisConfig
#
#   config = AnalysisConfig(
#       include_summary=True,
#       include_keywords=True,
#       include_classification=True,
#       classification_prompt="Actionable resources for instructors and TAs",
#       classification_scale=4,
#   )
#   result = analyze_page(text="...", url="https://...", config=config)
#
# Audience classification usage:
#   config = AnalysisConfig(
#       include_summary=True,
#       include_audience_classification=True,
#   )
#   result = analyze_page(text="...", url="https://...", config=config)
#   # result keys: audience_classification, audience_confidence_score,
#   #              audience_primary_indicators, audience_reasoning

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from utils.llm_helpers import call_llm_json, truncate_text, DEFAULT_MODEL


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class AnalysisConfig:
    """
    Controls which fields the LLM generates and how.
    All fields default to off — enable only what you need.
    """

    # ── Content analysis fields ───────────────────────────────────────────────
    include_summary:      bool = True
    """One-paragraph summary of the page (≤50 words)."""

    include_cta:          bool = False
    """Primary call to action — what the page asks/wants users to do."""

    include_theme:        bool = False
    """Main theme or topic of the page in a short phrase."""

    include_audience:     bool = False
    """Target audience(s) — who the page is primarily written for."""

    include_keywords:     bool = False
    """List of important keywords or keyphrases from the content."""
    keyword_min:          int = 5
    keyword_max:          int = 10

    include_meta_tags:    bool = False
    """
    Categorize the page into one or more predefined meta tag categories.
    Provide categories via meta_tag_categories. If empty, the model picks freely.
    """
    meta_tag_categories:  list[str] = field(default_factory=list)

    include_description:  bool = False
    """Short description for SEO/CMS use (≤30 words)."""

    # ── Relevance classification ───────────────────────────────────────────────
    include_classification: bool = False
    """
    Score the page's relevance to a custom prompt.
    Requires classification_prompt to be set.
    """
    classification_prompt:  str = ""
    """
    Plain-language description of what you're looking for.
    e.g. "Actionable resources for instructors and teaching assistants"
    """
    classification_scale:   int = 4
    """
    Max score (1 to N). Common choices: 4 (conservative) or 5 (nuanced).
    Score 1 = not relevant, score N = highly relevant.
    """
    include_classification_reason:      bool = True
    """Include a 1-2 sentence reason for the classification score."""
    include_classification_confidence:  bool = True
    """Include a confidence value (0.0–1.0) in the classification score."""

    # ── Audience classification ────────────────────────────────────────────────
    include_audience_classification: bool = False
    """
    Classify the page as Internal, External, Mixed, or Unclassified based on
    its target audience — current students/staff vs. prospective/public audiences.

    Output keys added to the result:
      audience_classification      — "Internal" | "External" | "Mixed" | "Unclassified"
      audience_confidence_score    — float 0.0–1.0
      audience_primary_indicators  — list of 3 keyword/phrase strings that drove the decision
      audience_reasoning           — one sentence explaining the classification
    """

    # ── Model ─────────────────────────────────────────────────────────────────
    model: str = DEFAULT_MODEL


# ── Audience classification prompt block ──────────────────────────────────────

_AUDIENCE_CLASSIFICATION_INSTRUCTIONS = """\
Audience classification:
  Definitions:
    INTERNAL — audience is current employees (support staff, researchers, instructors,
      librarians, faculty) and/or current students navigating university processes,
      accessing private services, HR forms, internal policy documents, or student portals.
    EXTERNAL — audience is prospective students, prospective employees (job-seekers),
      partners, funders (governments, donors), alumni, or the general public; purpose is
      marketing, admissions, job listings, research impact, giving pages, or public services.
    MIXED    — page intent is genuinely split; no single audience controls ≥80% of the content.
    UNCLASSIFIED — insufficient information to make a determination.

  Classification rules:
    - Choose "Internal"  if >80% of the page targets current students or staff.
    - Choose "External"  if >80% of the page targets prospective or public audiences.
    - Choose "Mixed"     if no single audience controls ≥80%.
    - Choose "Unclassified" only when the content is too sparse to decide.

  Priority keyword signals:
    Internal indicators : Canvas, Blackboard, Employee Benefits Portal, Faculty Handbook
    External indicators : Apply Now, Donate, Alumni Association, Campus Tours

  Identify exactly 3 keywords or phrases from the actual page content that most strongly
  drove your classification decision (primary_indicators).
"""

_AUDIENCE_CLASSIFICATION_SCHEMA = """\
  "audience_classification": string — one of: "Internal", "External", "Mixed", "Unclassified",
  "audience_confidence_score": float 0.0–1.0 — confidence in the classification,
  "audience_primary_indicators": array of exactly 3 strings — keywords/phrases from the content that drove the decision,
  "audience_reasoning": string — one concise sentence explaining the classification\
"""


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(text: str, url: str, config: AnalysisConfig) -> str:
    """
    Build a single prompt that requests only the enabled fields.
    The model returns a single JSON object with one key per enabled field.
    """
    schema_lines: list[str] = []
    instruction_lines: list[str] = []

    if config.include_summary:
        schema_lines.append('"summary": string — one paragraph, ≤50 words')

    if config.include_description:
        schema_lines.append('"description": string — ≤30 words, suitable for CMS/SEO use')

    if config.include_cta:
        schema_lines.append('"call_to_action": string — the primary action the page asks users to take, or null')

    if config.include_theme:
        schema_lines.append('"main_theme": string — the central topic or subject in a short phrase')

    if config.include_audience:
        schema_lines.append('"target_audience": array of strings — who this page is written for')

    if config.include_keywords:
        schema_lines.append(
            f'"keywords": array of {config.keyword_min}–{config.keyword_max} strings '
            f'— important keyphrases from the content, not category names'
        )

    if config.include_meta_tags:
        if config.meta_tag_categories:
            cats = "\n".join(f"  - {c}" for c in config.meta_tag_categories)
            instruction_lines.append(
                f"For meta_tags, choose only from this list (use exact names):\n{cats}\n"
            )
        schema_lines.append('"meta_tags": array of strings — matching categories from the provided list')

    if config.include_classification:
        if not config.classification_prompt:
            raise ValueError("classification_prompt must be set when include_classification=True")
        instruction_lines.append(
            f"Relevance classification:\n"
            f"  Prompt: \"{config.classification_prompt}\"\n"
            f"  Score the page from 1 to {config.classification_scale} where:\n"
            f"    1 = not relevant at all\n"
            f"    {config.classification_scale} = highly relevant\n"
            f"  Score conservatively when uncertain.\n"
        )
        schema_lines.append(f'"relevance_score": integer 1–{config.classification_scale}')
        if config.include_classification_reason:
            schema_lines.append('"relevance_reason": string — 1–2 sentences explaining the score')
        if config.include_classification_confidence:
            schema_lines.append('"relevance_confidence": float 0.0–1.0 — how confident you are in the score')

    if config.include_audience_classification:
        instruction_lines.append(_AUDIENCE_CLASSIFICATION_INSTRUCTIONS)
        schema_lines.append(_AUDIENCE_CLASSIFICATION_SCHEMA)

    if not schema_lines:
        raise ValueError("AnalysisConfig has no output fields enabled — turn on at least one")

    schema_str = "{\n  " + ",\n  ".join(schema_lines) + "\n}"
    instructions_str = "\n".join(instruction_lines)

    prompt = f"""Analyze the following webpage content and return a JSON object matching this schema exactly:

{schema_str}

{instructions_str}
URL: {url}

Page Content:
{text}

Return ONLY the JSON object. No markdown fences, no extra commentary."""

    return prompt


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_page(
    text:   str,
    url:    str = "",
    config: AnalysisConfig | None = None,
    max_content_chars: int = 12000,
) -> dict[str, Any]:
    """
    Analyze a page's text content using the LLM and return structured results.

    Args:
        text:              The page's clean text content
        url:               The page URL (used as context in the prompt)
        config:            AnalysisConfig controlling which fields to generate.
                           Defaults to summary-only if not provided.
        max_content_chars: Truncate text to this length before sending to the LLM

    Returns:
        Dict containing the requested fields plus metadata:
        {
            "url": str,
            "title": str,           # if extractable from text
            "error": str | None,
            ...requested fields...
        }

        When include_audience_classification=True, the dict also contains:
        {
            "audience_classification":     str,   # Internal | External | Mixed | Unclassified
            "audience_confidence_score":   float,
            "audience_primary_indicators": list[str],
            "audience_reasoning":          str,
        }
    """
    if config is None:
        config = AnalysisConfig(include_summary=True)

    result: dict[str, Any] = {
        "url":   url,
        "error": None,
    }

    if not text or not text.strip():
        result["error"] = "No content to analyze"
        return result

    try:
        content = truncate_text(text.strip(), max_chars=max_content_chars)
        prompt  = _build_prompt(content, url=url, config=config)
        parsed  = call_llm_json(prompt=prompt, model=config.model)
    except Exception as e:
        result["error"] = str(e)
        return result

    # Check for parse errors
    if parsed.get("_parse_error"):
        result["error"] = f"JSON parse failed. Raw: {parsed.get('_raw', '')}"
        return result

    result.update(parsed)
    return result