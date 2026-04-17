"""
Azure live resource fetcher.
Reads all managed resources from an Azure subscription and returns them
in the same normalised format as backend.drift.parser.extract_resources().

Cross-subscription support:
  get_live_resources_multi() inspects each resource ID in the state file to
  determine which subscription it belongs to. Resources in the primary
  subscription are fetched via a full list (existing behaviour). Resources in
  other subscriptions are looked up individually by resource ID — avoiding
  noise from unrelated resources in shared subscriptions.

Note on attribute coverage:
  Azure's generic resources.list() API returns a limited set of attributes
  (location, tags, kind, sku). Resource-specific APIs (e.g. the Storage or
  Network APIs) return full detail — that's a planned future enhancement.
  Drift comparison today is limited to what Azure returns here.
"""
from __future__ import annotations

import os
import re
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


# Resource types in Terraform that are not returned by the generic Resource
# Management API. These must be filtered from state before drift detection to
# avoid guaranteed false-positive "deleted" drift entries.
_UNFETCHABLE_TF_TYPES: frozenset[str] = frozenset({
    "azurerm_role_assignment",
    "azurerm_role_definition",
})


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

    credential = DefaultAzureCredential()
    return _fetch_subscription(sub_id, credential)


def get_live_resources_multi(
    primary_subscription: str | None,
    state_resources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Fetch live resources, handling cross-subscription state automatically.

    - Primary subscription: full list fetch (same as get_live_resources).
    - Any other subscription found in state resource IDs: individual get-by-ID
      lookup, so unrelated resources in shared subscriptions are never fetched.

    Args:
        primary_subscription: the subscription to do a full list fetch on.
            Falls back to AZURE_SUBSCRIPTION_ID env var.
        state_resources: the parsed resources from the state file — used to
            discover cross-subscription resource IDs.

    Returns:
        Tuple of (live_resources, warnings). Warnings are emitted when a
        cross-subscription lookup fails — the resource is skipped rather than
        silently treated as deleted drift.
    """
    primary_sub = primary_subscription or os.getenv("AZURE_SUBSCRIPTION_ID")
    if not primary_sub:
        raise ValueError(
            "No subscription ID provided. "
            "Pass one explicitly or set the AZURE_SUBSCRIPTION_ID environment variable."
        )

    credential = DefaultAzureCredential()

    # Full fetch for the primary subscription
    live: list[dict[str, Any]] = _fetch_subscription(primary_sub, credential)
    warnings: list[str] = []

    # Find any state resource IDs that belong to a different subscription
    primary_lower = primary_sub.lower()
    cross_sub_ids = [
        r["azure_id"]
        for r in state_resources
        if r.get("azure_id")
        and _parse_subscription_id(r["azure_id"]) not in (None, primary_lower)
    ]

    if cross_sub_ids:
        seen: set[str] = {r["azure_id"] for r in live}
        for resource_id in cross_sub_ids:
            if resource_id in seen:
                continue
            item, error = _get_resource_by_id(resource_id, credential)
            if item:
                live.append(item)
                seen.add(resource_id)
            elif error:
                warnings.append(
                    f"Cross-subscription lookup failed for {resource_id}: {error} — skipped (resource excluded from drift check)"
                )

    return live, warnings


def filter_unsupported_state_resources(
    state_resources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    Remove resource types that cannot be fetched via the Azure Resource Management API.

    Returns (kept_resources, removed_counts_by_type). Callers should warn the
    user about any removed types so drift results are not silently incomplete.
    """
    kept = []
    removed: dict[str, int] = {}
    for r in state_resources:
        if r.get("type") in _UNFETCHABLE_TF_TYPES:
            removed[r["type"]] = removed.get(r["type"], 0) + 1
        else:
            kept.append(r)
    return kept, removed


def _fetch_subscription(
    subscription_id: str,
    credential: DefaultAzureCredential,
) -> list[dict[str, Any]]:
    """Full list fetch for a single subscription (existing behaviour)."""
    client = ResourceManagementClient(credential, subscription_id)
    resources: list[dict[str, Any]] = []

    for rg in client.resource_groups.list():
        item = _normalise_resource_group(rg)
        if item:
            resources.append(item)

    for resource in client.resources.list(expand="properties"):
        item = _normalise_resource(resource)
        if item:
            resources.append(item)

    return resources


def _get_resource_by_id(
    resource_id: str,
    credential: DefaultAzureCredential,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Look up a single resource by its full Azure resource ID.

    Used for cross-subscription resources — avoids fetching unrelated
    resources from shared subscriptions.

    Returns (resource_dict, None) on success, (None, error_message) on failure.
    Never returns (None, None) — a missing resource returns (None, "not found").
    """
    sub_id = _parse_subscription_id(resource_id)
    if not sub_id:
        return None, f"could not parse subscription ID from resource ID"

    provider_info = _parse_provider_info(resource_id)
    if not provider_info:
        return None, f"could not parse provider info from resource ID"

    namespace, resource_type = provider_info
    client = ResourceManagementClient(credential, sub_id)

    try:
        api_version = _resolve_api_version(namespace, resource_type, client)
        resource = client.resources.get_by_id(resource_id, api_version)
        return _normalise_resource(resource), None
    except Exception as exc:
        return None, str(exc)


# ── Internal helpers ─────────────────────────────────────────────────────────

# Cache: (namespace, resource_type) → api_version string
_API_VERSION_CACHE: dict[tuple[str, str], str] = {}

_FALLBACK_API_VERSION = "2021-04-01"

# Regex to extract subscription ID from a resource ID
_SUB_RE = re.compile(r"^/subscriptions/([^/]+)", re.IGNORECASE)


def _parse_subscription_id(resource_id: str) -> str | None:
    """Extract and return the lowercased subscription GUID from a resource ID."""
    match = _SUB_RE.match(resource_id)
    return match.group(1).lower() if match else None


def _parse_provider_info(resource_id: str) -> tuple[str, str] | None:
    """
    Extract the provider namespace and resource type from a resource ID.

    Examples:
      .../providers/Microsoft.KeyVault/vaults/my-vault
        → ("Microsoft.KeyVault", "vaults")
      .../providers/Microsoft.KeyVault/vaults/my-vault/secrets/my-secret
        → ("Microsoft.KeyVault", "vaults/secrets")
      .../resourceGroups/my-rg  (no providers segment)
        → ("Microsoft.Resources", "resourceGroups")
    """
    parts = resource_id.split("/")
    try:
        idx = next(i for i, p in enumerate(parts) if p.lower() == "providers")
    except StopIteration:
        # Resource group ID — no providers segment
        if any(p.lower() == "resourcegroups" for p in parts):
            return ("Microsoft.Resources", "resourceGroups")
        return None

    namespace = parts[idx + 1]
    # Resource type: alternating type/name pairs after namespace
    # e.g. vaults/my-vault/secrets/my-secret → type = "vaults/secrets"
    remainder = parts[idx + 2:]  # [type, name, subtype, subname, ...]
    type_parts = [remainder[i] for i in range(0, len(remainder), 2)]
    resource_type = "/".join(type_parts)

    return (namespace, resource_type)


def _resolve_api_version(
    namespace: str,
    resource_type: str,
    client: ResourceManagementClient,
) -> str:
    """
    Return the latest stable API version for a resource type.

    Results are cached per (namespace, resource_type) to avoid repeated
    provider calls for the same type within a single run.
    """
    cache_key = (namespace.lower(), resource_type.lower())
    if cache_key in _API_VERSION_CACHE:
        return _API_VERSION_CACHE[cache_key]

    def _best_version(api_versions: list) -> str:
        stable = [v for v in api_versions if "preview" not in v.lower()]
        candidates = stable or api_versions
        return sorted(candidates, reverse=True)[0] if candidates else _FALLBACK_API_VERSION

    try:
        provider = client.providers.get(namespace)
        type_map = {rt.resource_type.lower(): rt for rt in (provider.resource_types or [])}

        # Exact match first
        if resource_type.lower() in type_map:
            rt = type_map[resource_type.lower()]
            version = _best_version(rt.api_versions or [])
            _API_VERSION_CACHE[cache_key] = version
            return version

        # Child resource fallback: try parent type (e.g. "service/apis" → "service")
        parent_type = resource_type.split("/")[0]
        if parent_type.lower() != resource_type.lower() and parent_type.lower() in type_map:
            rt = type_map[parent_type.lower()]
            version = _best_version(rt.api_versions or [])
            _API_VERSION_CACHE[cache_key] = version
            return version
    except Exception:
        pass

    _API_VERSION_CACHE[cache_key] = _FALLBACK_API_VERSION
    return _FALLBACK_API_VERSION


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
