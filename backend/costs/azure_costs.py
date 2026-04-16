"""
Azure Cost Management API integration.
Pulls month-to-date spend for a subscription, broken down by resource.

Usage:
    from backend.costs.azure_costs import get_current_spend, SubscriptionCost

    cost = get_current_spend(subscription_id)
    print(cost.total, cost.currency)
    for entry in cost.entries:
        print(entry.resource_id, entry.cost)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import (
    QueryDefinition,
    QueryDataset,
    QueryAggregation,
    QueryGrouping,
)


@dataclass
class CostEntry:
    """Cost for a single Azure resource (month-to-date)."""
    resource_id: str
    resource_type: str
    resource_group: str
    cost: float
    currency: str


@dataclass
class SubscriptionCost:
    """Aggregated cost data for a subscription (month-to-date)."""
    subscription_id: str
    billing_period: str          # "YYYY-MM"
    total: float
    currency: str
    entries: list[CostEntry] = field(default_factory=list)

    def cost_for(self, resource_id: str) -> float | None:
        """Return the cost for a specific resource ID, or None if not found."""
        needle = resource_id.lower()
        for entry in self.entries:
            if entry.resource_id.lower() == needle:
                return entry.cost
        return None


def get_current_spend(subscription_id: str | None = None) -> SubscriptionCost:
    """
    Fetch month-to-date spend for an Azure subscription, broken down by resource.

    Uses the Azure Cost Management query API with timeframe="MonthToDate".
    Results are grouped by ResourceId, ResourceType, and ResourceGroupName.

    Args:
        subscription_id: Azure subscription ID. Falls back to the
            AZURE_SUBSCRIPTION_ID environment variable if not provided.

    Returns:
        SubscriptionCost with a total and per-resource CostEntry list.

    Raises:
        ValueError: if no subscription ID can be resolved.
        azure.core.exceptions.ClientAuthenticationError: if credentials fail.
        azure.core.exceptions.HttpResponseError: if the Cost Management API
            returns an error (e.g. insufficient permissions).
    """
    sub_id = subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID")
    if not sub_id:
        raise ValueError(
            "No subscription ID provided. "
            "Pass one explicitly or set the AZURE_SUBSCRIPTION_ID environment variable."
        )

    client = CostManagementClient(DefaultAzureCredential())
    scope = f"/subscriptions/{sub_id}"
    billing_period = date.today().strftime("%Y-%m")

    query = QueryDefinition(
        type="ActualCost",
        timeframe="MonthToDate",
        dataset=QueryDataset(
            granularity="None",
            aggregation={
                "totalCost": QueryAggregation(name="Cost", function="Sum"),
            },
            grouping=[
                QueryGrouping(type="Dimension", name="ResourceId"),
                QueryGrouping(type="Dimension", name="ResourceType"),
                QueryGrouping(type="Dimension", name="ResourceGroupName"),
            ],
        ),
    )

    result = client.query.usage(scope=scope, parameters=query)
    return _parse_query_result(sub_id, billing_period, result)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_query_result(
    subscription_id: str,
    billing_period: str,
    result: Any,
) -> SubscriptionCost:
    """
    Parse the raw Cost Management QueryResult into a SubscriptionCost.

    The API response has:
      result.columns  — list of objects with .name
      result.rows     — list of lists, one per resource

    Column order is not guaranteed — we locate each column by name.
    """
    columns = [col.name for col in (result.columns or [])]
    rows = result.rows or []

    col_lower = [c.lower() for c in columns]

    def _idx(name: str) -> int | None:
        try:
            return col_lower.index(name.lower())
        except ValueError:
            return None

    cost_idx = _idx("Cost")
    currency_idx = _idx("Currency")
    resource_id_idx = _idx("ResourceId")
    resource_type_idx = _idx("ResourceType")
    resource_group_idx = _idx("ResourceGroupName")

    entries: list[CostEntry] = []
    currency = "USD"

    for row in rows:
        cost_val = float(row[cost_idx]) if cost_idx is not None else 0.0
        if currency_idx is not None:
            currency = str(row[currency_idx])

        resource_id = str(row[resource_id_idx]) if resource_id_idx is not None else ""
        resource_type = str(row[resource_type_idx]) if resource_type_idx is not None else ""
        resource_group = str(row[resource_group_idx]) if resource_group_idx is not None else ""

        # Skip rows with no resource ID (e.g. subscription-level charges)
        if not resource_id or resource_id.lower() == "unknown":
            continue

        entries.append(CostEntry(
            resource_id=resource_id,
            resource_type=resource_type,
            resource_group=resource_group,
            cost=cost_val,
            currency=currency,
        ))

    total = sum(e.cost for e in entries)
    return SubscriptionCost(
        subscription_id=subscription_id,
        billing_period=billing_period,
        total=round(total, 4),
        currency=currency,
        entries=sorted(entries, key=lambda e: e.cost, reverse=True),
    )
