# utils/llm_helpers.py
#
# Generic OpenAI helpers used across multiple core scripts.
# Handles rate-limit retries, JSON extraction, and prompt assembly.

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_PROJECT = (os.getenv("OPENAI_PROJECT") or "").strip()

DEFAULT_MODEL = "gpt-4.1-mini"

# How long to wait after hitting a rate limit before retrying
RATE_LIMIT_WAIT = 20   # seconds for per-minute limits
HOURLY_WAIT     = 65   # seconds for per-hour limits


def get_openai_client():
    """Return a shared OpenAI client. Raises if API key is missing."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing — add it to your .env file")
    try:
        from openai import OpenAI
        return OpenAI(api_key=OPENAI_API_KEY, project=(OPENAI_PROJECT or None))
    except ImportError:
        raise RuntimeError("openai package not installed — pip install openai")


def call_llm(
    prompt:      str,
    model:       str = DEFAULT_MODEL,
    system_msg:  str = "Return valid JSON only. No extra keys, no markdown.",
    temperature: float = 0.0,
    max_retries: int = 5,
) -> str:
    """
    Send a prompt to OpenAI and return the raw response string.
    Automatically retries on rate limits with exponential-ish backoff.

    Returns the raw text content (not parsed).
    Raises RuntimeError on non-retryable failure.
    """
    client = get_openai_client()

    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": prompt},
                ],
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content.strip()

        except Exception as e:
            err_str = str(e).lower()
            status  = getattr(e, "status_code", None)

            is_rate_limit = (
                status == 429
                or "rate limit" in err_str
                or "429" in err_str
                or "please slow down" in err_str
            )
            is_hourly = (
                "hour" in err_str
                or "quota" in err_str
            )

            if is_hourly:
                print(f"[LLM] Hourly quota hit — waiting {HOURLY_WAIT}s before retry {attempt + 1}/{max_retries}")
                time.sleep(HOURLY_WAIT)
            elif is_rate_limit:
                wait = RATE_LIMIT_WAIT * (attempt + 1)
                print(f"[LLM] Rate limit — waiting {wait}s before retry {attempt + 1}/{max_retries}")
                time.sleep(wait)
            else:
                raise RuntimeError(f"LLM call failed: {e}")

    raise RuntimeError(f"LLM call failed after {max_retries} retries")


def parse_json_response(raw: str) -> dict[str, Any]:
    """
    Clean and parse a JSON response from the LLM.
    Handles markdown fences and leading 'json' strings.
    Returns an empty dict on parse failure rather than raising.
    """
    clean = raw.strip()
    # Strip markdown code fences
    clean = re.sub(r"^```(?:json)?\s*", "", clean)
    clean = re.sub(r"\s*```$", "", clean)
    clean = clean.strip()
    # Strip leading 'json' label
    if clean.lower().startswith("json"):
        clean = clean[4:].strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        # Try to extract a JSON object from somewhere in the string
        m = re.search(r"\{.*\}", clean, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return {"_parse_error": True, "_raw": raw[:500]}


def call_llm_json(
    prompt:      str,
    model:       str = DEFAULT_MODEL,
    system_msg:  str = "Return valid JSON only. No extra keys, no markdown.",
    temperature: float = 0.0,
) -> dict[str, Any]:
    """
    Convenience wrapper: call the LLM and return parsed JSON.
    Combines call_llm + parse_json_response.
    """
    raw = call_llm(prompt=prompt, model=model, system_msg=system_msg, temperature=temperature)
    return parse_json_response(raw)


def truncate_text(text: str, max_chars: int = 12000) -> str:
    """Truncate text to a safe length for LLM context windows."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[... truncated at {max_chars} chars]"
