"""
Tests for backend.drift.azure_fetcher

All Azure SDK calls are mocked — no real Azure credentials required.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.drift.azure_fetcher import (
    get_live_resources,
    _normalise_resource,
    _normalise_resource_group,
    _map_azure_type,
)

SUB_ID = "12345678-1234-1234-1234-123456789abc"
RG_ID = f"/subscriptions/{SUB_ID}/resourceGroups/demo-rg"
SA_ID = f"/subscriptions/{SUB_ID}/resourceGroups/demo-rg/providers/Microsoft.Storage/storageAccounts/demosa"
VNET_ID = f"/subscriptions/{SUB_ID}/resourceGroups/demo-rg/providers/Microsoft.Network/virtualNetworks/demo-vnet"


# ── Helpers to build mock SDK objects ────────────────────────────────────────

def _mock_rg(azure_id=RG_ID, name="demo-rg", location="eastus", tags=None):
    rg = MagicMock()
    rg.id = azure_id
    rg.name = name
    rg.location = location
    rg.tags = tags or {"env": "demo"}
    return rg


def _mock_resource(azure_id, name, azure_type, location="eastus",
                   tags=None, kind=None, sku_name=None, sku_tier=None):
    r = MagicMock()
    r.id = azure_id
    r.name = name
    r.type = azure_type
    r.location = location
    r.tags = tags or {}
    r.kind = kind
    if sku_name:
        r.sku = MagicMock()
        r.sku.name = sku_name
        r.sku.tier = sku_tier
    else:
        r.sku = None
    return r


def _make_client(rgs, resources):
    """Return a mock ResourceManagementClient with the given iterators."""
    client = MagicMock()
    client.resource_groups.list.return_value = iter(rgs)
    client.resources.list.return_value = iter(resources)
    return client


# ── get_live_resources ────────────────────────────────────────────────────────

@pytest.fixture()
def mock_client():
    rgs = [_mock_rg()]
    resources = [
        _mock_resource(SA_ID, "demosa", "Microsoft.Storage/storageAccounts",
                       kind="StorageV2", sku_name="Standard_LRS", sku_tier="Standard"),
        _mock_resource(VNET_ID, "demo-vnet", "Microsoft.Network/virtualNetworks"),
    ]
    return _make_client(rgs, resources)


@pytest.fixture()
def live_resources(mock_client):
    with patch("backend.drift.azure_fetcher._build_client", return_value=mock_client):
        return get_live_resources(SUB_ID)


def test_get_live_resources_returns_list(live_resources):
    assert isinstance(live_resources, list)


def test_get_live_resources_total_count(live_resources):
    # 1 RG + 2 resources = 3 total
    assert len(live_resources) == 3


def test_get_live_resources_includes_resource_group(live_resources):
    types = [r["type"] for r in live_resources]
    assert "azurerm_resource_group" in types


def test_get_live_resources_includes_storage_account(live_resources):
    types = [r["type"] for r in live_resources]
    assert "azurerm_storage_account" in types


def test_get_live_resources_includes_vnet(live_resources):
    types = [r["type"] for r in live_resources]
    assert "azurerm_virtual_network" in types


def test_no_subscription_id_raises(monkeypatch):
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    with pytest.raises(ValueError, match="No subscription ID"):
        get_live_resources()


def test_subscription_id_from_env(monkeypatch, mock_client):
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", SUB_ID)
    with patch("backend.drift.azure_fetcher._build_client", return_value=mock_client) as build:
        get_live_resources()  # no explicit sub_id
        build.assert_called_once_with(SUB_ID)


# ── resource shape ────────────────────────────────────────────────────────────

REQUIRED_KEYS = {"type", "name", "module", "provider_name", "id", "azure_id", "attributes"}


def test_resource_group_shape(live_resources):
    rg = next(r for r in live_resources if r["type"] == "azurerm_resource_group")
    assert REQUIRED_KEYS == rg.keys()


def test_generic_resource_shape(live_resources):
    sa = next(r for r in live_resources if r["type"] == "azurerm_storage_account")
    assert REQUIRED_KEYS == sa.keys()


def test_module_is_always_empty(live_resources):
    for r in live_resources:
        assert r["module"] == ""


def test_provider_name_is_azurerm(live_resources):
    for r in live_resources:
        assert r["provider_name"] == "azurerm"


def test_azure_id_is_lowercase(live_resources):
    for r in live_resources:
        assert r["azure_id"] == r["id"].lower()


def test_storage_account_has_sku_attributes(live_resources):
    sa = next(r for r in live_resources if r["type"] == "azurerm_storage_account")
    assert sa["attributes"].get("sku_name") == "Standard_LRS"
    assert sa["attributes"].get("sku_tier") == "Standard"
    assert sa["attributes"].get("kind") == "StorageV2"


# ── _normalise_resource ───────────────────────────────────────────────────────

def test_normalise_resource_returns_none_for_no_id():
    r = _mock_resource("", "broken", "Microsoft.Storage/storageAccounts")
    r.id = None
    assert _normalise_resource(r) is None


def test_normalise_resource_drops_none_attributes():
    r = _mock_resource(SA_ID, "demosa", "Microsoft.Storage/storageAccounts",
                       tags=None, kind=None)
    r.tags = None
    r.kind = None
    item = _normalise_resource(r)
    assert "kind" not in item["attributes"]
    # tags defaults to {} (non-None), so it stays
    assert "tags" in item["attributes"]


# ── _normalise_resource_group ─────────────────────────────────────────────────

def test_normalise_resource_group_returns_none_for_no_id():
    rg = _mock_rg(azure_id=None)
    rg.id = None
    assert _normalise_resource_group(rg) is None


def test_normalise_resource_group_type():
    rg = _mock_rg()
    item = _normalise_resource_group(rg)
    assert item["type"] == "azurerm_resource_group"


# ── _map_azure_type ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("azure_type, expected_tf", [
    ("Microsoft.Resources/resourceGroups", "azurerm_resource_group"),
    ("Microsoft.Storage/storageAccounts", "azurerm_storage_account"),
    ("Microsoft.Network/virtualNetworks", "azurerm_virtual_network"),
    ("Microsoft.Network/virtualNetworks/subnets", "azurerm_subnet"),
    ("Microsoft.KeyVault/vaults", "azurerm_key_vault"),
    ("Microsoft.Compute/virtualMachines", "azurerm_linux_virtual_machine"),
    ("Microsoft.ContainerService/managedClusters", "azurerm_kubernetes_cluster"),
])
def test_map_azure_type_known(azure_type, expected_tf):
    assert _map_azure_type(azure_type) == expected_tf


def test_map_azure_type_unknown_fallback():
    result = _map_azure_type("Microsoft.SomeNewService/someResources")
    assert result == "azurerm_somenewservice_someresources"


def test_map_azure_type_case_insensitive():
    assert _map_azure_type("microsoft.storage/storageaccounts") == "azurerm_storage_account"
    assert _map_azure_type("MICROSOFT.STORAGE/STORAGEACCOUNTS") == "azurerm_storage_account"
