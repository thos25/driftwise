"""
Drift detection engine.
Compares resources from Terraform state against live Azure resources.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DriftItem:
    resource_type: str
    resource_name: str
    resource_id: str
    drift_type: str          # "added" | "deleted" | "modified"
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    changed_fields: list[str] = field(default_factory=list)


def detect_drift(
    state_resources: list[dict[str, Any]],
    live_resources: list[dict[str, Any]],
) -> list[DriftItem]:
    """
    Compare state vs. live and return a list of DriftItems.

    Both lists contain dicts with at minimum: id, type, name, attributes.
    """
    # Use azure_id (lowercased) as the match key — Azure returns mixed-case
    # resource IDs that may not byte-match the IDs stored in TF state.
    state_by_id = {r["azure_id"]: r for r in state_resources if r.get("azure_id")}
    live_by_id = {r["azure_id"]: r for r in live_resources if r.get("azure_id")}

    drift: list[DriftItem] = []

    # Deleted in Azure but still in state
    for rid, res in state_by_id.items():
        if rid not in live_by_id:
            drift.append(
                DriftItem(
                    resource_type=res["type"],
                    resource_name=res["name"],
                    resource_id=rid,
                    drift_type="deleted",
                    expected=res["attributes"],
                )
            )

    # Present in Azure but not in state
    for rid, res in live_by_id.items():
        if rid not in state_by_id:
            drift.append(
                DriftItem(
                    resource_type=res["type"],
                    resource_name=res["name"],
                    resource_id=rid,
                    drift_type="added",
                    actual=res["attributes"],
                )
            )

    # Present in both — check for attribute changes.
    # Only check attributes that exist in BOTH state and live: we iterate live
    # (actual) keys but skip any that Terraform didn't track.  This means:
    #   - Azure-managed fields (auto-tags, ETags, system timestamps) not in TF
    #     state are silently ignored → no false "modified" drift.
    #   - TF-state fields that Azure's list API doesn't return are also ignored
    #     → no false positives for attributes we can't observe.
    for rid in state_by_id.keys() & live_by_id.keys():
        expected = state_by_id[rid]["attributes"]
        actual = live_by_id[rid]["attributes"]
        changed = [k for k in actual if k in expected and actual.get(k) != expected.get(k)]
        if changed:
            drift.append(
                DriftItem(
                    resource_type=state_by_id[rid]["type"],
                    resource_name=state_by_id[rid]["name"],
                    resource_id=rid,
                    drift_type="modified",
                    expected=expected,
                    actual=actual,
                    changed_fields=changed,
                )
            )

    return drift
