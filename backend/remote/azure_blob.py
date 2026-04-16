"""
Azure Blob Storage backend — reads Terraform state directly into memory.

Parses a backends.tfvars file to locate the blob, then streams the content
using DefaultAzureCredential. Nothing is written to disk.

Expected backends.tfvars fields:
    resource_group_name  = "my-tfstate-rg"
    storage_account_name = "mystorageacct"
    container_name       = "tfstate"
    key                  = "prod/terraform.tfstate"
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_KV_RE = re.compile(r'^\s*(\w+)\s*=\s*"([^"]*)"\s*(?:#.*)?$')

REQUIRED_FIELDS = {"resource_group_name", "storage_account_name", "container_name", "key"}


class BackendConfigError(Exception):
    """Raised when the backend config file is missing or invalid."""


class BlobFetchError(Exception):
    """Raised when the state blob cannot be retrieved."""


@dataclass
class BackendConfig:
    resource_group_name: str
    storage_account_name: str
    container_name: str
    key: str


def parse_backend_config(path: Path) -> BackendConfig:
    """
    Parse a backends.tfvars file and return a BackendConfig.

    Only processes simple key = "value" lines — sufficient for all standard
    Azure backend configs. Ignores comments and blank lines.

    Raises:
        BackendConfigError: if the file cannot be read or required fields are missing.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BackendConfigError(f"Cannot read backend config: {exc}") from exc

    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _KV_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2)

    missing = REQUIRED_FIELDS - values.keys()
    if missing:
        raise BackendConfigError(
            f"Backend config is missing required field(s): {', '.join(sorted(missing))}"
        )

    return BackendConfig(
        resource_group_name=values["resource_group_name"],
        storage_account_name=values["storage_account_name"],
        container_name=values["container_name"],
        key=values["key"],
    )


def fetch_state(config: BackendConfig) -> dict[str, Any]:
    """
    Download Terraform state from Azure Blob Storage into memory.

    Uses DefaultAzureCredential — honours az login, environment variables,
    managed identity, and the rest of the standard credential chain.

    Returns the parsed state dict, ready for extract_resources().

    Raises:
        BlobFetchError: if the blob cannot be accessed or is not valid JSON.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
    except ImportError as exc:
        raise BlobFetchError(
            "azure-storage-blob is required for remote state. "
            "Run: pip install azure-storage-blob"
        ) from exc

    account_url = f"https://{config.storage_account_name}.blob.core.windows.net"

    try:
        client = BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())
        blob_client = client.get_blob_client(container=config.container_name, blob=config.key)
        data = blob_client.download_blob().readall()
    except Exception as exc:
        raise BlobFetchError(
            f"Could not download state blob '{config.key}' from "
            f"{config.storage_account_name}/{config.container_name}: {exc}"
        ) from exc

    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise BlobFetchError(f"State blob is not valid JSON: {exc}") from exc
