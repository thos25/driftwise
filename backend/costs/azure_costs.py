"""
Azure Cost Management API integration.
Pulls current spend and provides cost impact estimates.
"""
from __future__ import annotations

# Placeholder — implementation will use azure-mgmt-costmanagement
# from azure.identity import DefaultAzureCredential
# from azure.mgmt.costmanagement import CostManagementClient


def get_current_spend(subscription_id: str) -> dict:
    """
    Fetch current month-to-date spend for the subscription.
    Returns a dict with currency, total, and per-resource breakdown.
    """
    raise NotImplementedError("Azure Cost Management integration not yet implemented")


def estimate_cost_delta(plan_output: str, subscription_id: str) -> dict:
    """
    Given the output of `terraform plan`, estimate the monthly cost delta.
    Returns a dict with added_cost, removed_cost, net_delta.
    """
    raise NotImplementedError("Cost delta estimation not yet implemented")
