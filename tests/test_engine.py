"""
Tests for backend.drift.engine
"""
import pytest
from backend.drift.engine import detect_drift, DriftItem

SUB = "/subscriptions/abc123"


def _res(azure_id, type_="azurerm_resource_group", name="rg", attrs=None):
    return {
        "type": type_,
        "name": name,
        "module": "",
        "provider_name": "azurerm",
        "id": azure_id,
        "azure_id": azure_id.lower(),
        "attributes": attrs or {"location": "eastus", "tags": {}},
    }


# ── no drift ──────────────────────────────────────────────────────────────────

def test_no_drift_when_identical():
    res = _res(f"{SUB}/resourceGroups/my-rg")
    assert detect_drift([res], [res]) == []


def test_no_drift_empty_inputs():
    assert detect_drift([], []) == []


# ── deleted ───────────────────────────────────────────────────────────────────

def test_detects_deleted():
    state = [_res(f"{SUB}/resourceGroups/rg-a"), _res(f"{SUB}/resourceGroups/rg-b")]
    live = [_res(f"{SUB}/resourceGroups/rg-a")]
    drift = detect_drift(state, live)
    assert len(drift) == 1
    assert drift[0].drift_type == "deleted"
    assert drift[0].resource_id == f"{SUB}/resourceGroups/rg-b".lower()


# ── added ─────────────────────────────────────────────────────────────────────

def test_detects_added():
    state = [_res(f"{SUB}/resourceGroups/rg-a")]
    live = [_res(f"{SUB}/resourceGroups/rg-a"), _res(f"{SUB}/resourceGroups/rg-b")]
    drift = detect_drift(state, live)
    assert len(drift) == 1
    assert drift[0].drift_type == "added"
    assert drift[0].resource_id == f"{SUB}/resourceGroups/rg-b".lower()


# ── modified ──────────────────────────────────────────────────────────────────

def test_detects_modified():
    rid = f"{SUB}/resourceGroups/rg-a"
    state = [_res(rid, attrs={"location": "eastus", "tags": {}})]
    live = [_res(rid, attrs={"location": "westus", "tags": {}})]
    drift = detect_drift(state, live)
    assert len(drift) == 1
    assert drift[0].drift_type == "modified"
    assert "location" in drift[0].changed_fields


def test_no_false_positive_for_extra_state_attrs():
    """Azure returns fewer attrs than TF state — those should not show as drift."""
    rid = f"{SUB}/resourceGroups/rg-a"
    state = [_res(rid, attrs={"location": "eastus", "tags": {}, "min_tls_version": "TLS1_2"})]
    live = [_res(rid, attrs={"location": "eastus", "tags": {}})]   # no min_tls_version
    drift = detect_drift(state, live)
    assert drift == []


def test_modified_shows_correct_changed_fields():
    rid = f"{SUB}/resourceGroups/rg-a"
    state = [_res(rid, attrs={"location": "eastus", "tags": {"env": "dev"}})]
    live = [_res(rid, attrs={"location": "eastus", "tags": {"env": "prod"}})]
    drift = detect_drift(state, live)
    assert drift[0].changed_fields == ["tags"]


# ── case-insensitive ID matching ──────────────────────────────────────────────

def test_matches_despite_id_case_difference():
    """Azure often returns IDs with different casing than TF state stores."""
    state_id = f"{SUB}/resourceGroups/My-RG"         # TF state: mixed case
    live_id = f"{SUB}/resourcegroups/my-rg"           # Azure API: lowercase

    state = [_res(state_id)]
    live = [_res(live_id)]
    # Same resource — should not appear as added/deleted
    drift = detect_drift(state, live)
    assert drift == []


# ── DriftItem fields ──────────────────────────────────────────────────────────

def test_deleted_item_has_expected_populated():
    rid = f"{SUB}/resourceGroups/rg-a"
    state = [_res(rid, attrs={"location": "eastus"})]
    drift = detect_drift(state, [])
    assert drift[0].expected == {"location": "eastus"}
    assert drift[0].actual == {}


def test_added_item_has_actual_populated():
    rid = f"{SUB}/resourceGroups/rg-a"
    live = [_res(rid, attrs={"location": "eastus"})]
    drift = detect_drift([], live)
    assert drift[0].actual == {"location": "eastus"}
    assert drift[0].expected == {}
