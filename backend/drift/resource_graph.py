"""
Azure Resource Graph fetcher.

Replaces the generic ResourceManagementClient.resources.list() approach.
Resource Graph returns ALL resource types including child resources (subnets,
NSG rules, APIM APIs, Key Vault secrets, DNS records, etc.) that the generic
list API misses entirely.

Two queries are run per subscription:
  - Resources          — all resources and child resources
  - ResourceContainers — resource groups (not present in Resources table)

Results are paginated at 1000 rows per page using skip tokens.
"""
from __future__ import annotations

from typing import Any

from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import (
    QueryRequest,
    QueryRequestOptions,
)


_RESOURCES_QUERY = """
Resources
| project id, name, type, location, tags, kind, sku, properties
"""

_RESOURCE_GROUPS_QUERY = """
ResourceContainers
| where type == 'microsoft.resources/subscriptions/resourcegroups'
| project id, name, type, location, tags
"""

_PAGE_SIZE = 1000


def fetch_via_graph(
    subscription_id: str,
    credential,
) -> list[dict[str, Any]]:
    """
    Fetch all resources in a subscription via Azure Resource Graph.

    Returns normalised resource dicts in the same shape as
    backend.drift.parser.extract_resources().

    Raises on auth failure but returns an empty list for individual
    row normalisation errors, so a single bad resource never aborts the run.
    """
    client = ResourceGraphClient(credential)
    rows: list[dict] = []
    rows.extend(_paginate(client, subscription_id, _RESOURCES_QUERY))
    rows.extend(_paginate(client, subscription_id, _RESOURCE_GROUPS_QUERY))

    resources: list[dict[str, Any]] = []
    for row in rows:
        item = _normalise_row(row)
        if item:
            resources.append(item)
    return resources


# ── Internal helpers ──────────────────────────────────────────────────────────

def _paginate(
    client: ResourceGraphClient,
    subscription_id: str,
    query: str,
) -> list[dict]:
    """Run a Resource Graph query with automatic skip-token pagination."""
    rows: list[dict] = []
    options = QueryRequestOptions(result_format="objectArray", top=_PAGE_SIZE)

    while True:
        request = QueryRequest(
            subscriptions=[subscription_id],
            query=query,
            options=options,
        )
        result = client.resources(request)
        if result.data:
            rows.extend(result.data)
        if not result.skip_token:
            break
        options = QueryRequestOptions(
            result_format="objectArray",
            top=_PAGE_SIZE,
            skip_token=result.skip_token,
        )

    return rows


def _normalise_row(row: dict) -> dict[str, Any] | None:
    """
    Convert a Resource Graph result row to our normalised resource dict.

    Attribute coverage is intentionally conservative — we only extract fields
    that are reliably present and comparable against Terraform state, to avoid
    false-positive drift from fields that differ in representation between the
    Graph API and Terraform's stored values.
    """
    azure_id: str = row.get("id") or ""
    if not azure_id:
        return None

    azure_type: str = row.get("type") or ""
    sku = row.get("sku") or {}
    props = row.get("properties") or {}

    attributes: dict[str, Any] = {}

    location = row.get("location")
    if location:
        attributes["location"] = location

    tags = row.get("tags")
    if tags:
        attributes["tags"] = tags

    kind = row.get("kind")
    if kind:
        attributes["kind"] = kind

    sku_name = sku.get("name") if isinstance(sku, dict) else getattr(sku, "name", None)
    sku_tier = sku.get("tier") if isinstance(sku, dict) else getattr(sku, "tier", None)
    if sku_name:
        attributes["sku_name"] = sku_name
    if sku_tier:
        attributes["sku_tier"] = sku_tier

    # Child resource extras — add address_prefixes for subnets
    if azure_type.lower() == "microsoft.network/virtualnetworks/subnets":
        prefixes = props.get("addressPrefixes") or []
        if not prefixes and props.get("addressPrefix"):
            prefixes = [props["addressPrefix"]]
        if prefixes:
            attributes["address_prefixes"] = prefixes

    return {
        "type": _map_type(azure_type),
        "name": row.get("name") or "",
        "module": "",
        "provider_name": "azurerm",
        "id": azure_id,
        "azure_id": azure_id.lower(),
        "attributes": attributes,
    }


# Mirrors the mapping table in azure_fetcher — kept in sync manually.
_AZURE_TO_TF_TYPE: dict[str, str] = {
    "microsoft.resources/resourcegroups": "azurerm_resource_group",
    "microsoft.resources/subscriptions/resourcegroups": "azurerm_resource_group",
    "microsoft.network/virtualnetworks": "azurerm_virtual_network",
    "microsoft.network/virtualnetworks/subnets": "azurerm_subnet",
    "microsoft.network/networksecuritygroups": "azurerm_network_security_group",
    "microsoft.network/networksecuritygroups/securityrules": "azurerm_network_security_rule",
    "microsoft.network/routetables": "azurerm_route_table",
    "microsoft.network/routetables/routes": "azurerm_route",
    "microsoft.network/publicipaddresses": "azurerm_public_ip",
    "microsoft.network/networkinterfaces": "azurerm_network_interface",
    "microsoft.network/loadbalancers": "azurerm_lb",
    "microsoft.network/applicationgateways": "azurerm_application_gateway",
    "microsoft.network/virtualnetworks/virtualnetworkpeerings": "azurerm_virtual_network_peering",
    "microsoft.network/dnszones": "azurerm_dns_zone",
    "microsoft.network/dnszones/a": "azurerm_dns_a_record",
    "microsoft.network/dnszones/cname": "azurerm_dns_cname_record",
    "microsoft.network/dnszones/mx": "azurerm_dns_mx_record",
    "microsoft.network/dnszones/txt": "azurerm_dns_txt_record",
    "microsoft.network/privatednszones": "azurerm_private_dns_zone",
    "microsoft.storage/storageaccounts": "azurerm_storage_account",
    "microsoft.storage/storageaccounts/blobservices/containers": "azurerm_storage_container",
    "microsoft.keyvault/vaults": "azurerm_key_vault",
    "microsoft.keyvault/vaults/secrets": "azurerm_key_vault_secret",
    "microsoft.keyvault/vaults/keys": "azurerm_key_vault_key",
    "microsoft.keyvault/vaults/certificates": "azurerm_key_vault_certificate",
    "microsoft.compute/virtualmachines": "azurerm_linux_virtual_machine",
    "microsoft.compute/disks": "azurerm_managed_disk",
    "microsoft.compute/availabilitysets": "azurerm_availability_set",
    "microsoft.containerservice/managedclusters": "azurerm_kubernetes_cluster",
    "microsoft.dbforpostgresql/servers": "azurerm_postgresql_server",
    "microsoft.dbforpostgresql/flexibleservers": "azurerm_postgresql_flexible_server",
    "microsoft.sql/servers": "azurerm_mssql_server",
    "microsoft.sql/servers/databases": "azurerm_mssql_database",
    "microsoft.sql/servers/firewallrules": "azurerm_mssql_firewall_rule",
    "microsoft.web/sites": "azurerm_linux_web_app",
    "microsoft.web/serverfarms": "azurerm_service_plan",
    "microsoft.apimanagement/service": "azurerm_api_management",
    "microsoft.apimanagement/service/apis": "azurerm_api_management_api",
    "microsoft.apimanagement/service/apis/policies": "azurerm_api_management_api_policy",
    "microsoft.apimanagement/service/products": "azurerm_api_management_product",
    "microsoft.insights/components": "azurerm_application_insights",
    "microsoft.operationalinsights/workspaces": "azurerm_log_analytics_workspace",
}


def _map_type(azure_type: str) -> str:
    key = azure_type.lower()
    if key in _AZURE_TO_TF_TYPE:
        return _AZURE_TO_TF_TYPE[key]
    fallback = key.replace("microsoft.", "", 1).replace("/", "_")
    return f"azurerm_{fallback}"
