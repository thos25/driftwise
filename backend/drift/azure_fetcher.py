"""
Azure live resource fetcher.
Reads all managed resources from an Azure subscription and returns them
in the same normalised format as backend.drift.parser.extract_resources().

Note on attribute coverage:
  Azure's generic resources.list() API returns a limited set of attributes
  (location, tags, kind, sku). Resource-specific APIs (e.g. the Storage or
  Network APIs) return full detail — that's a planned future enhancement.
  Drift comparison today is limited to what Azure returns here.
"""
from __future__ import annotations

import os
from typing import Any

from azure.identity import DefaultAzureCredential
from azure.mgmt.resource import ResourceManagementClient


# Maps lowercase Azure API resource type → Terraform resource type.
# Unknown types fall back to a safe snake_case approximation.
_AZURE_TO_TF_TYPE: dict[str, str] = {
    "microsoft.resources/resourcegroups": "azurerm_resource_group",
    "microsoft.network/virtualnetworks": "azurerm_virtual_network",
    "microsoft.network/virtualnetworks/subnets": "azurerm_subnet",
    "microsoft.storage/storageaccounts": "azurerm_storage_account",
    "microsoft.keyvault/vaults": "azurerm_key_vault",
    "microsoft.compute/virtualmachines": "azurerm_linux_virtual_machine",
    "microsoft.compute/disks": "azurerm_managed_disk",
    "microsoft.compute/availabilitysets": "azurerm_availability_set",
    "microsoft.network/networksecuritygroups": "azurerm_network_security_group",
    "microsoft.network/publicipaddresses": "azurerm_public_ip",
    "microsoft.network/networkinterfaces": "azurerm_network_interface",
    "microsoft.network/loadbalancers": "azurerm_lb",
    "microsoft.network/applicationgateways": "azurerm_application_gateway",
    "microsoft.containerservice/managedclusters": "azurerm_kubernetes_cluster",
    "microsoft.dbforpostgresql/servers": "azurerm_postgresql_server",
    "microsoft.dbforpostgresql/flexibleservers": "azurerm_postgresql_flexible_server",
    "microsoft.sql/servers": "azurerm_mssql_server",
    "microsoft.sql/servers/databases": "azurerm_mssql_database",
    "microsoft.web/sites": "azurerm_linux_web_app",
    "microsoft.insights/components": "azurerm_application_insights",
    "microsoft.operationalinsights/workspaces": "azurerm_log_analytics_workspace",
}


def get_live_resources(subscription_id: str | None = None) -> list[dict[str, Any]]:
    """
    Fetch all managed resources from an Azure subscription.

    Returns a list of resource dicts in the same shape as
    backend.drift.parser.extract_resources():
      type, name, module, provider_name, id, azure_id, attributes

    Args:
        subscription_id: Azure subscription ID. Falls back to the
            AZURE_SUBSCRIPTION_ID environment variable if not provided.

    Raises:
        ValueError: if no subscription ID can be found.
        azure.core.exceptions.ClientAuthenticationError: if credentials fail.
    """
    sub_id = subscription_id or os.getenv("AZURE_SUBSCRIPTION_ID")
    if not sub_id:
        raise ValueError(
            "No subscription ID provided. "
            "Pass one explicitly or set the AZURE_SUBSCRIPTION_ID environment variable."
        )

    client = _build_client(sub_id)
    resources: list[dict[str, Any]] = []

    # Resource groups are not returned by resources.list() — fetch separately.
    for rg in client.resource_groups.list():
        item = _normalise_resource_group(rg)
        if item:
            resources.append(item)

    # All other resource types in the subscription.
    for resource in client.resources.list(expand="properties"):
        item = _normalise_resource(resource)
        if item:
            resources.append(item)

    return resources


# ── Internal helpers ─────────────────────────────────────────────────────────

def _build_client(subscription_id: str) -> ResourceManagementClient:
    """Create an authenticated Azure Resource Management client.

    DefaultAzureCredential tries (in order):
      env vars (AZURE_CLIENT_ID / CLIENT_SECRET / TENANT_ID),
      workload identity, managed identity, Azure CLI, VS Code, etc.
    """
    return ResourceManagementClient(DefaultAzureCredential(), subscription_id)


def _normalise_resource(resource) -> dict[str, Any] | None:
    """
    Convert a GenericResource SDK object to our normalised dict.
    Returns None if the resource has no ID.
    """
    azure_id: str = getattr(resource, "id", None) or ""
    if not azure_id:
        return None

    azure_type: str = getattr(resource, "type", "") or ""
    sku = getattr(resource, "sku", None)

    attributes: dict[str, Any] = {
        "location": getattr(resource, "location", None),
        "tags": getattr(resource, "tags", None) or {},
        "kind": getattr(resource, "kind", None),
        "sku_name": (sku.name if sku and getattr(sku, "name", None) else None),
        "sku_tier": (sku.tier if sku and getattr(sku, "tier", None) else None),
    }
    # Drop None values — avoids false diffs against TF state keys we can't observe
    attributes = {k: v for k, v in attributes.items() if v is not None}

    return {
        "type": _map_azure_type(azure_type),
        "name": getattr(resource, "name", "") or "",
        "module": "",  # live resources have no module concept
        "provider_name": "azurerm",
        "id": azure_id,
        "azure_id": azure_id.lower(),
        "attributes": attributes,
    }


def _normalise_resource_group(rg) -> dict[str, Any] | None:
    """Convert a ResourceGroup SDK object to our normalised dict."""
    azure_id: str = getattr(rg, "id", None) or ""
    if not azure_id:
        return None

    attributes: dict[str, Any] = {
        "location": getattr(rg, "location", None),
        "name": getattr(rg, "name", None),
        "tags": getattr(rg, "tags", None) or {},
    }
    attributes = {k: v for k, v in attributes.items() if v is not None}

    return {
        "type": "azurerm_resource_group",
        "name": getattr(rg, "name", "") or "",
        "module": "",
        "provider_name": "azurerm",
        "id": azure_id,
        "azure_id": azure_id.lower(),
        "attributes": attributes,
    }


def _map_azure_type(azure_type: str) -> str:
    """
    Map an Azure API resource type to the nearest Terraform resource type name.

    Falls back to a snake_case approximation for unknown types:
      "Microsoft.Foo/bars" → "azurerm_foo_bars"
    """
    key = azure_type.lower()
    if key in _AZURE_TO_TF_TYPE:
        return _AZURE_TO_TF_TYPE[key]

    # Strip "microsoft." prefix, replace "/" with "_"
    fallback = key.replace("microsoft.", "", 1).replace("/", "_")
    return f"azurerm_{fallback}"
