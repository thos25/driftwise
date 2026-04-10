"""
AI triage layer — optional enhancement to the drift report.

If no LLM API key is configured the tool works fine; this module simply
returns an empty result. Callers should check triage_available() first
if they want to show a hint to the user.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from backend.drift.engine import DriftItem


RISK_LEVELS = {"low", "medium", "high", "critical"}

_SYSTEM_PROMPT = """\
You are an Azure infrastructure drift analyst.
When given a Terraform state drift item, respond with a JSON object containing exactly:
  "summary":     1-2 sentence plain-English description of the change and why it matters.
  "risk_level":  one of: low, medium, high, critical.
  "remediation": 1-2 sentence specific action the engineer should take.

Respond with raw JSON only — no markdown fences, no text outside the JSON object.\
"""


@dataclass
class TriageResult:
    summary: str
    risk_level: str   # low | medium | high | critical
    remediation: str


def triage_available() -> bool:
    """Return True if an LLM API key is present in the environment."""
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY"))


def triage_drift(drift_items: list[DriftItem]) -> dict[str, TriageResult]:
    """
    Call the configured LLM for each drift item and return triage results.

    Returns a dict keyed by resource_id → TriageResult.
    Returns an empty dict if no API key is configured.
    A failed call for one item is silently skipped — it never aborts the report.
    """
    if not triage_available():
        return {}

    provider = _detect_provider()
    results: dict[str, TriageResult] = {}

    for item in drift_items:
        result = _triage_one(item, provider)
        if result is not None:
            results[item.resource_id] = result

    return results


# ── Internal helpers ──────────────────────────────────────────────────────────

def _detect_provider() -> str:
    """
    Return 'anthropic' or 'openai'.

    Respects LLM_PROVIDER env var if set; otherwise uses whichever key exists,
    preferring Anthropic.
    """
    preferred = os.getenv("LLM_PROVIDER", "").lower()
    if preferred == "anthropic" and os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if preferred == "openai" and os.getenv("OPENAI_API_KEY"):
        return "openai"
    # No preference set — use whichever key is available
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "openai"


def _triage_one(item: DriftItem, provider: str) -> TriageResult | None:
    """Call the LLM for a single drift item. Returns None on any failure."""
    try:
        raw = _call_llm(_build_prompt(item), provider)
        return _parse_response(raw)
    except Exception:
        return None


def _build_prompt(item: DriftItem) -> str:
    lines = [
        f"Drift type:    {item.drift_type}",
        f"Resource type: {item.resource_type}",
        f"Resource name: {item.resource_name}",
        f"Resource ID:   {item.resource_id}",
    ]

    if item.drift_type == "modified" and item.changed_fields:
        lines.append("\nChanged attributes (Terraform state → live Azure):")
        for field in item.changed_fields:
            expected_val = item.expected.get(field, "<not in state>")
            actual_val = item.actual.get(field, "<not in Azure>")
            lines.append(f"  {field}: {expected_val!r} → {actual_val!r}")
    elif item.drift_type == "deleted":
        lines.append("\nThis resource is recorded in Terraform state but no longer exists in Azure.")
    elif item.drift_type == "added":
        lines.append("\nThis resource exists in live Azure but is not tracked in Terraform state.")

    return "\n".join(lines)


def _call_llm(prompt: str, provider: str) -> str:
    """Route to the appropriate LLM provider."""
    if provider == "anthropic":
        return _call_anthropic(prompt)
    return _call_openai(prompt)


def _call_anthropic(prompt: str) -> str:
    import anthropic
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _call_openai(prompt: str) -> str:
    import openai
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=model,
        max_tokens=512,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content


def _parse_response(raw: str) -> TriageResult:
    data = json.loads(raw.strip())
    risk = data.get("risk_level", "unknown").lower()
    if risk not in RISK_LEVELS:
        risk = "unknown"
    return TriageResult(
        summary=data.get("summary", ""),
        risk_level=risk,
        remediation=data.get("remediation", ""),
    )
