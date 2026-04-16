"""
Terraform state file parser.
Reads a .tfstate (JSON, version 3 or 4) and returns a normalised list of resources.

Version 4 (Terraform 0.12+): top-level `resources` list with `instances` per resource.
Version 3 (Terraform 0.11 and earlier): top-level `modules` list, resources keyed as
  "type.name" dicts with a `primary` block containing attributes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SUPPORTED_VERSIONS = {3, 4}

# Fields that are computed/metadata and should not be diffed against live state
SKIP_ATTRIBUTES = {"id", "timeouts", "private"}


class StateParseError(Exception):
    """Raised when the state file cannot be parsed or is unsupported."""


def load_state(path: str | Path) -> dict[str, Any]:
    """
    Load a Terraform state file and return the raw state dict.

    Raises:
        FileNotFoundError: if the path does not exist.
        StateParseError: if the file is not valid JSON or an unsupported version.
    """
    path = Path(path)

    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except json.JSONDecodeError as exc:
        raise StateParseError(f"State file is not valid JSON: {exc}") from exc

    version = state.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise StateParseError(
            f"Unsupported state file version: {version!r}. "
            f"Supported versions: {sorted(SUPPORTED_VERSIONS)}. "
            f"Run 'terraform state pull' to get a current state file."
        )

    return state


def extract_resources(state: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Flatten all *managed* resources out of a Terraform state object.

    Supports state file versions 3 and 4. Skips data sources and any resource
    instance that has no Azure resource ID.

    Each returned dict has:
      type          — Terraform resource type (e.g. "azurerm_resource_group")
      name          — Terraform resource name (e.g. "main")
      module        — module path or "" for root-level resources
      provider_name — short provider name (e.g. "azurerm")
      id            — Azure resource ID, original casing from state
      azure_id      — Azure resource ID, lowercased for stable comparison
      attributes    — full attribute dict (minus skip fields)
    """
    if state.get("version") == 3:
        return _extract_v3(state)
    return _extract_v4(state)


def _extract_v4(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract resources from a version 4 state file."""
    resources: list[dict[str, Any]] = []

    for resource in state.get("resources", []):
        if resource.get("mode") != "managed":
            continue

        r_type = resource["type"]
        r_name = resource["name"]
        r_module = resource.get("module", "")
        provider_name = _clean_provider(resource.get("provider", ""))

        for instance in resource.get("instances", []):
            attrs = instance.get("attributes", {})
            azure_id = attrs.get("id", "")

            if not azure_id:
                continue

            resources.append(
                {
                    "type": r_type,
                    "name": r_name,
                    "module": r_module,
                    "provider_name": provider_name,
                    "id": azure_id,
                    "azure_id": azure_id.lower(),
                    "attributes": {k: v for k, v in attrs.items() if k not in SKIP_ATTRIBUTES},
                }
            )

    return resources


def _extract_v3(state: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract resources from a version 3 state file.

    V3 structure:
      modules[].path          — list of path segments, e.g. ["root"] or ["root", "mymod"]
      modules[].resources     — dict keyed as "type.name" (managed) or "data.type.name" (data)
      resources[key].type     — resource type
      resources[key].primary  — primary instance with .id and .attributes
    """
    resources: list[dict[str, Any]] = []

    for module in state.get("modules", []):
        path_parts = module.get("path", ["root"])
        # Build a module string like "" for root, "module.foo" for nested
        if path_parts == ["root"] or path_parts == []:
            r_module = ""
        else:
            r_module = ".".join(f"module.{p}" for p in path_parts[1:])

        for key, resource in module.get("resources", {}).items():
            # Skip data sources — keyed as "data.type.name"
            if key.startswith("data."):
                continue

            r_type = resource.get("type", "")
            # Key format is "type.name" — extract name as everything after first dot
            r_name = key[len(r_type) + 1:] if key.startswith(r_type + ".") else key
            provider_name = _clean_provider(resource.get("provider", r_type.split("_")[0]))

            primary = resource.get("primary", {})
            azure_id = primary.get("id", "")
            if not azure_id:
                continue

            attrs = primary.get("attributes", {})

            resources.append(
                {
                    "type": r_type,
                    "name": r_name,
                    "module": r_module,
                    "provider_name": provider_name,
                    "id": azure_id,
                    "azure_id": azure_id.lower(),
                    "attributes": {k: v for k, v in attrs.items() if k not in SKIP_ATTRIBUTES},
                }
            )

    return resources


# ── Internal helpers ─────────────────────────────────────────────────────────

_PROVIDER_RE = re.compile(r'provider\["[^"]+/([^"/]+)"\]')


def _clean_provider(provider_str: str) -> str:
    """
    Extract the short provider name from a full provider string.

    Examples:
      'provider["registry.terraform.io/hashicorp/azurerm"]' → 'azurerm'
      'registry.terraform.io/hashicorp/azurerm'             → 'azurerm'
      'azurerm'                                             → 'azurerm'
    """
    match = _PROVIDER_RE.search(provider_str)
    if match:
        return match.group(1)
    # Fallback: take the last path segment
    return provider_str.rstrip("/").rsplit("/", 1)[-1]
