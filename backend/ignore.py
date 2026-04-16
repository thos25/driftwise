"""
Ignore rule loader and matcher for DriftWise.

Rules are loaded from a YAML file (default: .driftwise-ignore) and/or
from inline --ignore CLI patterns. Matched resources are suppressed from
the drift report entirely.

YAML format:
    ignore:
      - name: "NetworkWatcherRG"
      - name: "cloud-shell-*"
      - type: "microsoft.network/networkwatchers"
      - name: "NetworkWatcher_*"
        drift_type: added
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backend.drift.engine import DriftItem

VALID_DRIFT_TYPES = {"added", "deleted", "modified"}


class IgnoreFileError(Exception):
    """Raised when the ignore file cannot be parsed."""


@dataclass
class IgnoreRule:
    name: Optional[str] = None        # wildcard-capable, matches resource_name
    type: Optional[str] = None        # case-insensitive, matches resource_type
    drift_type: Optional[str] = None  # added | deleted | modified — None means all


def load_ignore_file(path: Path) -> list[IgnoreRule]:
    """
    Parse a .driftwise-ignore YAML file and return a list of IgnoreRules.

    Raises:
        IgnoreFileError: if the file exists but cannot be parsed.
    """
    try:
        import yaml
    except ImportError:
        raise IgnoreFileError(
            "PyYAML is required to use ignore files. Run: pip install pyyaml"
        )

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        raise IgnoreFileError(f"Could not read ignore file: {exc}") from exc

    if data is None:
        return []

    raw_rules = data.get("ignore", [])
    if not isinstance(raw_rules, list):
        raise IgnoreFileError("'ignore' must be a list of rules.")

    rules: list[IgnoreRule] = []
    for i, entry in enumerate(raw_rules):
        if not isinstance(entry, dict):
            raise IgnoreFileError(f"Rule {i + 1} must be a mapping (got {type(entry).__name__}).")

        drift_type = entry.get("drift_type")
        if drift_type is not None and drift_type not in VALID_DRIFT_TYPES:
            raise IgnoreFileError(
                f"Rule {i + 1}: invalid drift_type '{drift_type}'. "
                f"Must be one of: {', '.join(sorted(VALID_DRIFT_TYPES))}."
            )

        rules.append(IgnoreRule(
            name=entry.get("name"),
            type=entry.get("type"),
            drift_type=drift_type,
        ))

    return rules


def rules_from_patterns(patterns: str) -> list[IgnoreRule]:
    """
    Parse a comma-separated string of name patterns into IgnoreRules.
    Used for the --ignore CLI flag.

    Example: "NetworkWatcherRG,cloud-shell-*" → two IgnoreRules matching by name.
    """
    rules = []
    for pattern in patterns.split(","):
        pattern = pattern.strip()
        if pattern:
            rules.append(IgnoreRule(name=pattern))
    return rules


def _matches(rule: IgnoreRule, item: DriftItem) -> bool:
    """Return True if a drift item matches an ignore rule."""
    if rule.drift_type is not None and rule.drift_type != item.drift_type:
        return False

    if rule.name is not None:
        if not fnmatch.fnmatch(item.resource_name.lower(), rule.name.lower()):
            return False

    if rule.type is not None:
        if item.resource_type.lower() != rule.type.lower():
            return False

    # A rule with no name/type criteria (only drift_type) matches everything
    # of that drift type — guard against accidentally suppressing everything.
    if rule.name is None and rule.type is None:
        return False

    return True


def apply_ignores(
    drift_items: list[DriftItem],
    rules: list[IgnoreRule],
) -> tuple[list[DriftItem], int]:
    """
    Filter drift items against ignore rules.

    Returns:
        (kept, suppressed_count) — items that passed the rules and the count
        of items that were suppressed.
    """
    if not rules:
        return drift_items, 0

    kept = []
    suppressed = 0
    for item in drift_items:
        if any(_matches(rule, item) for rule in rules):
            suppressed += 1
        else:
            kept.append(item)

    return kept, suppressed
