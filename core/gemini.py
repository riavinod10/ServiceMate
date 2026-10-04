"""Shared Gemini setup for every agent (team standard).

Provider: Google Gemini through the `google-genai` SDK.
Config:   GEMINI_API_KEY (required) and GEMINI_MODEL (default gemini-3.6-flash).

Agents call generate_structured() with a Pydantic model and get a validated
instance back, or a GeminiUnavailable exception they can fall back from.
Nothing here ever lets an LLM problem crash the workflow.

Notes for gemini-3.6-flash: custom temperature / top-k / top-p values are
ignored and frequency / presence penalties raise an error, so none are set.
"""
import logging
import os

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_ATTEMPTS = 2        # the SDK default is 5, which could stall a workflow for minutes
PLACEHOLDER_KEYS = {"", "your_key_here", "your_gemini_api_key_here"}


class GeminiUnavailable(Exception):
    """No key, SDK missing, network/API error, timeout or unusable output."""


def gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def get_client(timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, attempts: int = DEFAULT_ATTEMPTS):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key in PLACEHOLDER_KEYS:
        raise GeminiUnavailable("GEMINI_API_KEY is not set")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise GeminiUnavailable("google-genai is not installed") from exc
    try:
        return genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=int(timeout_seconds * 1000),  # the SDK takes milliseconds
                retry_options=types.HttpRetryOptions(attempts=attempts),
            ),
        )
    except Exception as exc:
        raise GeminiUnavailable(f"Could not create Gemini client: {exc}") from exc


def generate_structured(prompt: str, schema: type[BaseModel], system_instruction: str,
                        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> BaseModel:
    """One Gemini call that must return JSON matching `schema`."""
    client = get_client(timeout_seconds=timeout_seconds)
    from google.genai import types

    try:
        response = client.models.generate_content(
            model=gemini_model(),
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
    except Exception as exc:  # network, auth, quota, timeout, bad model name
        raise GeminiUnavailable(f"Gemini call failed: {type(exc).__name__}: {exc}") from exc

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema):
        return parsed
    text = getattr(response, "text", None)
    if not text:
        raise GeminiUnavailable("Gemini returned an empty response")
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise GeminiUnavailable(f"Gemini returned malformed output: {exc.error_count()} error(s)") from exc
