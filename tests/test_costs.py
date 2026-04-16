"""
Tests for backend.costs.azure_costs

All Azure SDK calls are mocked — no real credentials required.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.costs.azure_costs import (
    CostEntry,
    SubscriptionCost,
    _parse_query_result,
    get_current_spend,
)

SUB = "00000000-1111-2222-3333-444444444444"
RG = "my-rg"
PERIOD = "2026-04"


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_result(rows: list[list], columns: list[str]):
    """Build a fake QueryResult-like object."""
    cols = [SimpleNamespace(name=c) for c in columns]
    return SimpleNamespace(columns=cols, rows=rows)


# ── _parse_query_result ───────────────────────────────────────────────────────

class TestParseQueryResult:
    _COLUMNS = ["Cost", "Currency", "ResourceId", "ResourceType", "ResourceGroupName"]

    def _result(self, rows):
        return _make_result(rows, self._COLUMNS)

    def test_single_row(self):
        rid = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Storage/storageAccounts/sa1"
        rows = [[12.34, "USD", rid, "microsoft.storage/storageaccounts", RG]]
        cost = _parse_query_result(SUB, PERIOD, self._result(rows))

        assert cost.subscription_id == SUB
        assert cost.billing_period == PERIOD
        assert cost.currency == "USD"
        assert len(cost.entries) == 1
        assert cost.entries[0].cost == 12.34
        assert cost.entries[0].resource_id == rid
        assert cost.entries[0].resource_group == RG
        assert cost.total == 12.34

    def test_multiple_rows_sorted_descending(self):
        base = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers"
        rows = [
            [5.0,  "USD", f"{base}/Microsoft.Network/virtualNetworks/vnet1", "microsoft.network/virtualnetworks", RG],
            [50.0, "USD", f"{base}/Microsoft.Compute/virtualMachines/vm1",   "microsoft.compute/virtualmachines",  RG],
            [1.0,  "USD", f"{base}/Microsoft.Storage/storageAccounts/sa1",   "microsoft.storage/storageaccounts",  RG],
        ]
        cost = _parse_query_result(SUB, PERIOD, self._result(rows))

        assert len(cost.entries) == 3
        assert cost.entries[0].cost == 50.0   # highest first
        assert cost.entries[-1].cost == 1.0
        assert round(cost.total, 4) == 56.0

    def test_skips_unknown_resource_id(self):
        rows = [
            [10.0, "USD", "Unknown", "microsoft.storage/storageaccounts", RG],
            [5.0,  "USD", "", "microsoft.network/virtualnetworks", RG],
        ]
        cost = _parse_query_result(SUB, PERIOD, self._result(rows))
        assert cost.entries == []
        assert cost.total == 0.0

    def test_empty_result(self):
        cost = _parse_query_result(SUB, PERIOD, self._result([]))
        assert cost.entries == []
        assert cost.total == 0.0
        assert cost.currency == "USD"   # default

    def test_column_order_independence(self):
        """Column order from the API is not guaranteed — parser must locate by name."""
        rid = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Compute/virtualMachines/vm1"
        # Swap the column order
        cols = ["ResourceGroupName", "ResourceType", "Cost", "ResourceId", "Currency"]
        rows = [[RG, "microsoft.compute/virtualmachines", 99.9, rid, "GBP"]]
        cost = _parse_query_result(SUB, PERIOD, _make_result(rows, cols))

        assert cost.entries[0].cost == 99.9
        assert cost.entries[0].currency == "GBP"
        assert cost.currency == "GBP"
        assert cost.total == 99.9


# ── SubscriptionCost.cost_for ─────────────────────────────────────────────────

class TestSubscriptionCostCostFor:
    def _make(self, entries):
        return SubscriptionCost(
            subscription_id=SUB,
            billing_period=PERIOD,
            total=sum(e.cost for e in entries),
            currency="USD",
            entries=entries,
        )

    def test_found_exact(self):
        rid = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Storage/storageAccounts/sa1"
        entry = CostEntry(rid, "microsoft.storage/storageaccounts", RG, 42.0, "USD")
        cost = self._make([entry])
        assert cost.cost_for(rid) == 42.0

    def test_found_case_insensitive(self):
        rid = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Storage/storageAccounts/sa1"
        entry = CostEntry(rid.lower(), "microsoft.storage/storageaccounts", RG, 7.5, "USD")
        cost = self._make([entry])
        assert cost.cost_for(rid.upper()) == 7.5

    def test_not_found_returns_none(self):
        cost = self._make([])
        assert cost.cost_for("/subscriptions/x/resourceGroups/y/providers/z") is None


# ── get_current_spend ─────────────────────────────────────────────────────────

class TestGetCurrentSpend:
    def _mock_result(self):
        rid = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Storage/storageAccounts/sa1"
        cols = ["Cost", "Currency", "ResourceId", "ResourceType", "ResourceGroupName"]
        rows = [[25.0, "USD", rid, "microsoft.storage/storageaccounts", RG]]
        return _make_result(rows, cols)

    def test_raises_when_no_subscription(self, monkeypatch):
        monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
        with pytest.raises(ValueError, match="No subscription ID"):
            get_current_spend()

    def test_uses_env_var_subscription(self, monkeypatch):
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", SUB)
        with patch("backend.costs.azure_costs.CostManagementClient") as mock_client_cls, \
             patch("backend.costs.azure_costs.DefaultAzureCredential"):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.query.usage.return_value = self._mock_result()

            cost = get_current_spend()

        assert cost.subscription_id == SUB
        assert len(cost.entries) == 1

    def test_explicit_subscription_overrides_env(self, monkeypatch):
        other_sub = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", other_sub)
        with patch("backend.costs.azure_costs.CostManagementClient") as mock_client_cls, \
             patch("backend.costs.azure_costs.DefaultAzureCredential"):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.query.usage.return_value = self._mock_result()

            cost = get_current_spend(SUB)

        assert cost.subscription_id == SUB

    def test_scope_passed_correctly(self, monkeypatch):
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", SUB)
        with patch("backend.costs.azure_costs.CostManagementClient") as mock_client_cls, \
             patch("backend.costs.azure_costs.DefaultAzureCredential"):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.query.usage.return_value = self._mock_result()

            get_current_spend()

        call_kwargs = mock_client.query.usage.call_args
        scope = call_kwargs[1].get("scope") or call_kwargs[0][0]
        assert scope == f"/subscriptions/{SUB}"
