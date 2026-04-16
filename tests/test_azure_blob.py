"""Tests for backend/remote/azure_blob.py — config parsing only (no live Azure calls)."""
import json
import pytest
from pathlib import Path

from backend.remote.azure_blob import parse_backend_config, BackendConfig, BackendConfigError


VALID_CONFIG = """\
resource_group_name  = "my-tfstate-rg"
storage_account_name = "mystorageacct"
container_name       = "tfstate"
key                  = "prod/terraform.tfstate"
"""


def test_parse_valid_config(tmp_path):
    f = tmp_path / "backends.tfvars"
    f.write_text(VALID_CONFIG, encoding="utf-8")
    config = parse_backend_config(f)
    assert config.resource_group_name == "my-tfstate-rg"
    assert config.storage_account_name == "mystorageacct"
    assert config.container_name == "tfstate"
    assert config.key == "prod/terraform.tfstate"


def test_parse_ignores_comments(tmp_path):
    f = tmp_path / "backends.tfvars"
    f.write_text(
        '# This is a comment\n'
        'resource_group_name  = "my-rg"\n'
        'storage_account_name = "myacct"  # inline comment\n'
        'container_name       = "tfstate"\n'
        'key                  = "prod.tfstate"\n',
        encoding="utf-8",
    )
    config = parse_backend_config(f)
    assert config.resource_group_name == "my-rg"
    assert config.storage_account_name == "myacct"


def test_parse_ignores_extra_fields(tmp_path):
    f = tmp_path / "backends.tfvars"
    f.write_text(
        VALID_CONFIG + 'access_key = "supersecret"\n',
        encoding="utf-8",
    )
    config = parse_backend_config(f)
    assert isinstance(config, BackendConfig)


def test_parse_missing_field_raises(tmp_path):
    f = tmp_path / "backends.tfvars"
    f.write_text(
        'resource_group_name  = "my-rg"\n'
        'storage_account_name = "myacct"\n'
        'container_name       = "tfstate"\n'
        # key is missing
        ,
        encoding="utf-8",
    )
    with pytest.raises(BackendConfigError, match="key"):
        parse_backend_config(f)


def test_parse_missing_file_raises(tmp_path):
    with pytest.raises(BackendConfigError, match="Cannot read"):
        parse_backend_config(tmp_path / "nonexistent.tfvars")
