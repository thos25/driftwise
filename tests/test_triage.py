"""
Tests for backend.ai.triage

All LLM calls are mocked — no real API keys required.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from backend.drift.engine import DriftItem
from backend.ai.triage import (
    triage_available,
    triage_drift,
    TriageResult,
    _detect_provider,
    _build_prompt,
    _parse_response,
    _call_llm,
)

SUB = "/subscriptions/abc123"

_GOOD_RESPONSE = json.dumps({
    "summary": "The storage account location changed unexpectedly.",
    "risk_level": "high",
    "remediation": "Check the Azure Activity Log and run terraform apply to revert.",
})


def _item(drift_type="modified", resource_type="azurerm_storage_account",
          name="sa", changed=None, expected=None, actual=None):
    return DriftItem(
        resource_type=resource_type,
        resource_name=name,
        resource_id=f"{SUB}/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/{name}",
        drift_type=drift_type,
        expected=expected or {"location": "eastus"},
        actual=actual or {"location": "westus"},
        changed_fields=changed or ["location"],
    )


# ── triage_available ──────────────────────────────────────────────────────────

def test_triage_available_false_when_no_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert triage_available() is False


def test_triage_available_true_with_anthropic_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert triage_available() is True


def test_triage_available_true_with_openai_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert triage_available() is True


# ── _detect_provider ──────────────────────────────────────────────────────────

def test_detect_provider_respects_llm_provider_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert _detect_provider() == "openai"


def test_detect_provider_falls_back_to_anthropic(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _detect_provider() == "anthropic"


def test_detect_provider_falls_back_to_openai(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _detect_provider() == "openai"


# ── triage_drift ──────────────────────────────────────────────────────────────

def test_triage_drift_returns_empty_when_no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = triage_drift([_item()])
    assert result == {}


def test_triage_drift_returns_results_keyed_by_resource_id(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    item = _item()
    with patch("backend.ai.triage._call_llm", return_value=_GOOD_RESPONSE):
        results = triage_drift([item])
    assert item.resource_id in results


def test_triage_drift_result_shape(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    item = _item()
    with patch("backend.ai.triage._call_llm", return_value=_GOOD_RESPONSE):
        results = triage_drift([item])
    r = results[item.resource_id]
    assert isinstance(r, TriageResult)
    assert r.summary
    assert r.risk_level == "high"
    assert r.remediation


def test_triage_drift_handles_api_failure_gracefully(monkeypatch):
    """A failing LLM call should not crash the run — just skip that item."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    item = _item()
    with patch("backend.ai.triage._call_llm", side_effect=Exception("API down")):
        results = triage_drift([item])
    assert results == {}


def test_triage_drift_partial_failure(monkeypatch):
    """One failure doesn't drop results for other items."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    item_a = _item(name="sa-a")
    item_b = _item(name="sa-b")

    call_count = 0
    def side_effect(prompt, provider):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("transient error")
        return _GOOD_RESPONSE

    with patch("backend.ai.triage._call_llm", side_effect=side_effect):
        results = triage_drift([item_a, item_b])

    assert item_a.resource_id not in results   # failed
    assert item_b.resource_id in results       # succeeded


# ── _build_prompt ─────────────────────────────────────────────────────────────

def test_build_prompt_modified_includes_changed_fields():
    item = _item(
        drift_type="modified",
        changed=["location", "tags"],
        expected={"location": "eastus", "tags": {"env": "dev"}},
        actual={"location": "westus", "tags": {"env": "prod"}},
    )
    prompt = _build_prompt(item)
    assert "location" in prompt
    assert "eastus" in prompt
    assert "westus" in prompt


def test_build_prompt_deleted_mentions_missing():
    item = _item(drift_type="deleted", changed=[])
    prompt = _build_prompt(item)
    assert "missing" in prompt.lower() or "no longer exists" in prompt.lower()


def test_build_prompt_added_mentions_untracked():
    item = _item(drift_type="added", changed=[])
    prompt = _build_prompt(item)
    assert "not tracked" in prompt.lower() or "not in terraform" in prompt.lower()


def test_build_prompt_includes_resource_metadata():
    item = _item(resource_type="azurerm_key_vault", name="my-kv")
    prompt = _build_prompt(item)
    assert "azurerm_key_vault" in prompt
    assert "my-kv" in prompt


# ── _parse_response ───────────────────────────────────────────────────────────

def test_parse_response_valid():
    result = _parse_response(_GOOD_RESPONSE)
    assert result.risk_level == "high"
    assert "storage" in result.summary.lower()


def test_parse_response_unknown_risk_level():
    raw = json.dumps({"summary": "x", "risk_level": "extreme", "remediation": "y"})
    result = _parse_response(raw)
    assert result.risk_level == "unknown"


def test_parse_response_case_insensitive_risk():
    raw = json.dumps({"summary": "x", "risk_level": "HIGH", "remediation": "y"})
    result = _parse_response(raw)
    assert result.risk_level == "high"


def test_parse_response_missing_fields():
    raw = json.dumps({"risk_level": "low"})
    result = _parse_response(raw)
    assert result.summary == ""
    assert result.remediation == ""
    assert result.risk_level == "low"
