"""
Terraform state file parser.
Reads a .tfstate (JSON, version 4) and returns a normalised list of resources.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SUPPORTED_VERSIONS = {4}

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
            f"Supported versions: {sorted(SUPPORTED_VERSIONS)}"
        )

    return state


def extract_resources(state: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Flatten all *managed* resources out of a Terraform state object.

    Skips data sources and any resource instance that has no Azure resource ID.

    Each returned dict has:
      type          — Terraform resource type (e.g. "azurerm_resource_group")
      name          — Terraform resource name (e.g. "main")
      module        — module path or "" for root-level resources
      provider_name — short provider name (e.g. "azurerm")
      id            — Azure resource ID, original casing from state
      azure_id      — Azure resource ID, lowercased for stable comparison
      attributes    — full attribute dict (minus skip fields)
    """
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
                continue  # skip instances with no Azure resource ID

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
