"""
Tests for backend.drift.parser
"""
import json
import pytest
from pathlib import Path

from backend.drift.parser import (
    load_state,
    extract_resources,
    StateParseError,
    _clean_provider,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_STATE = FIXTURES / "sample.tfstate"


# ── load_state ────────────────────────────────────────────────────────────────

def test_load_state_returns_dict():
    state = load_state(SAMPLE_STATE)
    assert isinstance(state, dict)


def test_load_state_has_expected_keys():
    state = load_state(SAMPLE_STATE)
    assert "version" in state
    assert "resources" in state
    assert state["version"] == 4


def test_load_state_missing_file():
    with pytest.raises(FileNotFoundError):
        load_state(FIXTURES / "does_not_exist.tfstate")


def test_load_state_invalid_json(tmp_path):
    bad = tmp_path / "bad.tfstate"
    bad.write_text("this is not json", encoding="utf-8")
    with pytest.raises(StateParseError, match="not valid JSON"):
        load_state(bad)


def test_load_state_wrong_version(tmp_path):
    old = tmp_path / "old.tfstate"
    old.write_text(json.dumps({"version": 99, "resources": []}), encoding="utf-8")
    with pytest.raises(StateParseError, match="Unsupported state file version"):
        load_state(old)


def test_load_state_v3_accepted(tmp_path):
    v3 = tmp_path / "v3.tfstate"
    v3.write_text(json.dumps({"version": 3, "modules": []}), encoding="utf-8")
    state = load_state(v3)
    assert state["version"] == 3


# ── extract_resources ─────────────────────────────────────────────────────────

@pytest.fixture()
def state():
    return load_state(SAMPLE_STATE)


def test_extract_count(state):
    # fixture has 5 resources: 4 managed + 1 data source
    resources = extract_resources(state)
    assert len(resources) == 4


def test_extract_skips_data_sources(state):
    resources = extract_resources(state)
    types = [r["type"] for r in resources]
    assert "azurerm_subscription" not in types


def test_extract_resource_shape(state):
    resources = extract_resources(state)
    required_keys = {"type", "name", "module", "provider_name", "id", "azure_id", "attributes"}
    for r in resources:
        assert required_keys == r.keys(), f"Missing keys in: {r}"


def test_extract_provider_name(state):
    resources = extract_resources(state)
    for r in resources:
        assert r["provider_name"] == "azurerm"


def test_extract_module_resources(state):
    resources = extract_resources(state)
    module_resources = [r for r in resources if r["module"] == "module.networking"]
    assert len(module_resources) == 2
    types = {r["type"] for r in module_resources}
    assert types == {"azurerm_virtual_network", "azurerm_subnet"}


def test_extract_root_resources_have_empty_module(state):
    resources = extract_resources(state)
    root = [r for r in resources if r["module"] == ""]
    assert len(root) == 2  # resource_group + storage_account


def test_extract_azure_id_is_lowercase(state):
    resources = extract_resources(state)
    for r in resources:
        assert r["azure_id"] == r["id"].lower()


def test_extract_skips_id_in_attributes(state):
    resources = extract_resources(state)
    for r in resources:
        assert "id" not in r["attributes"], "Raw 'id' should be excluded from attributes"


def test_extract_skips_no_id(tmp_path):
    """Resources with no Azure ID should be silently skipped."""
    state_data = {
        "version": 4,
        "resources": [
            {
                "mode": "managed",
                "type": "azurerm_resource_group",
                "name": "incomplete",
                "provider": "provider[\"registry.terraform.io/hashicorp/azurerm\"]",
                "instances": [{"attributes": {}}],  # no id field
            }
        ],
    }
    resources = extract_resources(state_data)
    assert resources == []


# ── _clean_provider ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ('provider["registry.terraform.io/hashicorp/azurerm"]', "azurerm"),
    ('provider["registry.terraform.io/hashicorp/aws"]', "aws"),
    ("registry.terraform.io/hashicorp/azurerm", "azurerm"),
    ("azurerm", "azurerm"),
    ("", ""),
])
def test_clean_provider(raw, expected):
    assert _clean_provider(raw) == expected
