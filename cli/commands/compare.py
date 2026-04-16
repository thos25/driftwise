"""
driftwise compare <state-file> [--subscription <id>]
driftwise compare --backend-config <backends.tfvars>

Loads a Terraform state file (local or from Azure Blob Storage), fetches live
Azure resources, runs the drift engine, optionally enriches results with AI
triage, and prints a report.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel

from backend.drift.parser import load_state, extract_resources, StateParseError
from backend.drift.azure_fetcher import get_live_resources_multi
from backend.drift.engine import detect_drift, DriftItem
from backend.ai.triage import triage_available, triage_drift, TriageResult
from backend.costs.azure_costs import get_current_spend, SubscriptionCost
from backend.ignore import load_ignore_file, rules_from_patterns, apply_ignores, IgnoreFileError
from backend.remote.azure_blob import parse_backend_config, fetch_state, BackendConfigError, BlobFetchError

console = Console()
err = Console(stderr=True, style="bold red")

_RISK_COLORS = {
    "low": "green",
    "medium": "yellow",
    "high": "red",
    "critical": "bold red",
}


def compare(
    state_file: Optional[Path] = typer.Argument(
        None,
        help="Path to terraform.tfstate. Omit when using --backend-config.",
    ),
    subscription: Optional[str] = typer.Option(
        None,
        "--subscription", "-s",
        help="Azure subscription ID. Falls back to AZURE_SUBSCRIPTION_ID env var.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Output results as JSON (useful for CI/CD pipelines).",
    ),
    costs: bool = typer.Option(
        False,
        "--costs",
        help="Fetch month-to-date spend from Azure Cost Management and show alongside drift.",
    ),
    show_all: bool = typer.Option(
        False,
        "--all",
        help="Also list resources that match (no drift).",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose", "-v",
        help="Show warnings when AI triage or other optional steps fail.",
    ),
    ignore_file: Optional[Path] = typer.Option(
        None,
        "--ignore-file",
        help="Path to a .driftwise-ignore YAML file. Defaults to .driftwise-ignore in the current directory.",
    ),
    ignore_patterns: Optional[str] = typer.Option(
        None,
        "--ignore",
        help="Comma-separated resource name patterns to suppress (e.g. 'NetworkWatcher*,cloud-shell-*').",
    ),
    backend_config: Optional[Path] = typer.Option(
        None,
        "--backend-config",
        help="Path to a backends.tfvars file. Reads state directly from Azure Blob Storage.",
    ),
    no_ai: bool = typer.Option(
        False,
        "--no-ai",
        help="Skip AI triage even if an API key is configured.",
    ),
) -> None:
    """
    Compare a Terraform state file against live Azure infrastructure.

    \b
    Examples:
      driftwise compare ./terraform.tfstate
      driftwise compare ./terraform.tfstate --subscription 12345678-...
      driftwise compare --backend-config ./backends.tfvars
      driftwise compare ./terraform.tfstate --json | jq '.drift[]'
    """

    # ── 1. Load and parse state file ──────────────────────────────────────────
    if backend_config and state_file:
        err.print("[ERROR] Provide either a state file or --backend-config, not both.")
        raise typer.Exit(1)

    if not backend_config and not state_file:
        err.print("[ERROR] Provide a state file path or --backend-config.")
        raise typer.Exit(1)

    if backend_config:
        with console.status("[bold cyan]Fetching state from Azure Blob Storage...[/]"):
            try:
                config = parse_backend_config(backend_config)
                raw_state = fetch_state(config)
            except (BackendConfigError, BlobFetchError) as exc:
                err.print(f"[ERROR] {exc}")
                raise typer.Exit(1)
        state_label = Path(config.key).name
    else:
        if not state_file.exists():
            err.print(f"[ERROR] State file not found: {state_file}")
            raise typer.Exit(1)
        try:
            raw_state = load_state(state_file)
        except StateParseError as exc:
            err.print(f"[ERROR] Could not parse state file: {exc}")
            raise typer.Exit(1)
        state_label = state_file

    state_resources = extract_resources(raw_state)

    # ── 2. Fetch live Azure resources ─────────────────────────────────────────
    with console.status("[bold cyan]Fetching live resources from Azure...[/]"):
        try:
            live_resources = get_live_resources_multi(subscription, state_resources)
        except ValueError as exc:
            err.print(f"[ERROR] {exc}")
            raise typer.Exit(1)
        except Exception as exc:
            err.print(f"[ERROR] Azure API call failed: {exc}")
            err.print(
                "Check that AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID "
                "are set, or that you are logged in with `az login`."
            )
            raise typer.Exit(1)

    # ── 3. Detect drift ────────────────────────────────────────────────────────
    drift_items = detect_drift(state_resources, live_resources)

    # ── 4. Apply ignore rules ─────────────────────────────────────────────────
    ignore_rules = []

    # Load from file: explicit --ignore-file, or .driftwise-ignore in cwd
    resolved_ignore_file = ignore_file or Path(".driftwise-ignore")
    if resolved_ignore_file.exists():
        try:
            ignore_rules.extend(load_ignore_file(resolved_ignore_file))
        except IgnoreFileError as exc:
            err.print(f"[ERROR] {exc}")
            raise typer.Exit(1)

    # Add inline --ignore patterns
    if ignore_patterns:
        ignore_rules.extend(rules_from_patterns(ignore_patterns))

    suppressed = 0
    if ignore_rules:
        drift_items, suppressed = apply_ignores(drift_items, ignore_rules)

    # ── 5. Optional: AI triage ────────────────────────────────────────────────
    # Runs only when an API key is present and --no-ai is not set.
    triage_results: dict[str, TriageResult] = {}
    if drift_items and triage_available() and not no_ai:
        with console.status("[bold cyan]Running AI triage...[/]"):
            triage_results = triage_drift(drift_items, verbose=verbose)

    # ── 5. Optional: cost data ────────────────────────────────────────────────
    cost_data: SubscriptionCost | None = None
    if costs:
        with console.status("[bold cyan]Fetching cost data from Azure...[/]"):
            try:
                cost_data = get_current_spend(subscription)
            except ValueError as exc:
                err.print(f"[ERROR] {exc}")
                raise typer.Exit(1)
            except Exception as exc:
                err.print(f"[WARN] Could not fetch cost data: {exc}")
                err.print("Cost data will be omitted from the report.")
                # Non-fatal — continue without costs

    # ── 6. Output ─────────────────────────────────────────────────────────────
    if json_out:
        _print_json(state_resources, live_resources, drift_items, triage_results, cost_data)
    else:
        drifted_ids = {d.resource_id for d in drift_items if d.drift_type in ("deleted", "modified")}
        clean_resources = [
            r for r in state_resources
            if r.get("azure_id") and r["azure_id"] not in drifted_ids
            and r["azure_id"] in {lr["azure_id"] for lr in live_resources if lr.get("azure_id")}
        ]
        _print_report(
            state_label, subscription,
            state_resources, live_resources,
            drift_items, triage_results, cost_data,
            clean_resources=clean_resources if show_all else [],
            suppressed=suppressed,
        )

    # Exit 2 when drift found — lets CI pipelines gate on this cleanly
    if drift_items:
        raise typer.Exit(2)


# ── Report renderer ───────────────────────────────────────────────────────────

def _print_report(
    state_file: Path,
    subscription: Optional[str],
    state_resources: list,
    live_resources: list,
    drift_items: list[DriftItem],
    triage_results: dict[str, TriageResult],
    cost_data: Optional[SubscriptionCost] = None,
    clean_resources: list | None = None,
    suppressed: int = 0,
) -> None:
    deleted  = [d for d in drift_items if d.drift_type == "deleted"]
    added    = [d for d in drift_items if d.drift_type == "added"]
    modified = [d for d in drift_items if d.drift_type == "modified"]
    clean    = len(state_resources) - len(deleted) - len(modified)

    console.print()
    console.print(Panel.fit("[bold]DriftWise — Drift Report[/]", border_style="cyan"))
    console.print()
    state_display = state_file.resolve() if isinstance(state_file, Path) else state_file
    console.print(f"  [dim]State file   :[/]  {state_display}")
    if subscription:
        console.print(f"  [dim]Subscription :[/]  {subscription}")
    console.print(f"  [dim]Resources    :[/]  {len(state_resources)} in state · {len(live_resources)} live")
    if cost_data is not None:
        console.print(
            f"  [dim]Cost (MTD)   :[/]  "
            f"[bold]{cost_data.total:,.2f} {cost_data.currency}[/]  "
            f"[dim]({cost_data.billing_period})[/]"
        )
    console.print()

    if not drift_items:
        console.print("  [bold green]✓ All resources match — no drift detected.[/]")
        console.print()
        return

    # Summary counts
    if clean > 0:
        console.print(f"  [green]✓  {clean} resource(s) match[/]")
    if deleted:
        console.print(f"  [red]✗  {len(deleted)} deleted[/]  [dim](in state, missing from Azure)[/]")
    if modified:
        console.print(f"  [yellow]~  {len(modified)} modified[/]")
    if added:
        console.print(f"  [blue]+  {len(added)} added[/]  [dim](in Azure, not in state)[/]")
    console.print()

    # Per-item detail sections
    if clean_resources:
        _print_clean_section(clean_resources, cost_data)
    if deleted:
        _print_section("Deleted", deleted, "✗", "red", triage_results, cost_data)
    if modified:
        _print_section("Modified", modified, "~", "yellow", triage_results, cost_data)
    if added:
        _print_section("Added", added, "+", "blue", triage_results, cost_data)

    # Suppression notice
    if suppressed:
        console.print(
            f"  [dim]{suppressed} resource(s) suppressed by ignore rules.[/]"
        )
        console.print()

    # Tip when no LLM key is configured
    if not triage_available():
        console.print(
            "  [dim]Tip: set OPENAI_API_KEY or ANTHROPIC_API_KEY to enable AI triage.[/]"
        )
        console.print()


def _print_clean_section(
    resources: list[dict],
    cost_data: Optional[SubscriptionCost] = None,
) -> None:
    console.rule("[bold green]Matching[/]", style="green")
    console.print()
    for r in resources:
        rid = r.get("azure_id", "")
        cost_str = ""
        if cost_data is not None:
            item_cost = cost_data.cost_for(rid)
            if item_cost is not None:
                cost_str = f"  [dim]{item_cost:,.2f} {cost_data.currency} MTD[/]"
        console.print(f"  [green]✓[/]  [bold]{r.get('type', '')}[/]  \"{r.get('name', '')}\"{cost_str}")
        console.print(f"     [dim]{_shorten_id(rid)}[/]")
        console.print()
    console.print()


def _print_section(
    title: str,
    items: list[DriftItem],
    icon: str,
    style: str,
    triage_results: dict[str, TriageResult],
    cost_data: Optional[SubscriptionCost] = None,
) -> None:
    console.rule(f"[bold {style}]{title}[/]", style=style)
    console.print()
    for item in items:
        _print_item(item, icon, style, triage_results.get(item.resource_id), cost_data)
    console.print()


def _print_item(
    item: DriftItem,
    icon: str,
    style: str,
    triage: Optional[TriageResult],
    cost_data: Optional[SubscriptionCost] = None,
) -> None:
    cost_str = ""
    if cost_data is not None:
        item_cost = cost_data.cost_for(item.resource_id)
        if item_cost is not None:
            cost_str = f"  [dim]{item_cost:,.2f} {cost_data.currency} MTD[/]"

    console.print(f"  [{style}]{icon}[/]  [bold]{item.resource_type}[/]  \"{item.resource_name}\"{cost_str}")
    if item.changed_fields:
        console.print(f"     [dim]Changed:[/] {', '.join(item.changed_fields)}")
    console.print(f"     [dim]{_shorten_id(item.resource_id)}[/]")

    if triage is not None:
        _print_triage_panel(triage)

    console.print()


def _print_triage_panel(result: TriageResult) -> None:
    color = _RISK_COLORS.get(result.risk_level, "dim")
    content = (
        f"[bold]Risk:[/] [{color}]{result.risk_level.upper()}[/]\n\n"
        f"{result.summary}\n\n"
        f"[dim]Remediation:[/] {result.remediation}"
    )
    console.print(
        Padding(
            Panel(content, title="[dim]AI Triage[/]", border_style=color, padding=(0, 1)),
            (0, 0, 0, 5),
        )
    )


# ── JSON renderer ─────────────────────────────────────────────────────────────

def _print_json(
    state_resources: list,
    live_resources: list,
    drift_items: list[DriftItem],
    triage_results: dict[str, TriageResult],
    cost_data: Optional[SubscriptionCost] = None,
) -> None:
    output: dict = {
        "state_count": len(state_resources),
        "live_count": len(live_resources),
        "drift_count": len(drift_items),
        "drift": [
            {
                "type": d.drift_type,
                "resource_type": d.resource_type,
                "resource_name": d.resource_name,
                "resource_id": d.resource_id,
                "changed_fields": d.changed_fields,
                "triage": _triage_to_dict(triage_results.get(d.resource_id)),
                "cost_mtd": (cost_data.cost_for(d.resource_id) if cost_data else None),
            }
            for d in drift_items
        ],
    }
    if cost_data is not None:
        output["costs"] = {
            "billing_period": cost_data.billing_period,
            "total": cost_data.total,
            "currency": cost_data.currency,
            "by_resource": [
                {
                    "resource_id": e.resource_id,
                    "resource_type": e.resource_type,
                    "resource_group": e.resource_group,
                    "cost": e.cost,
                    "currency": e.currency,
                }
                for e in cost_data.entries
            ],
        }
    print(json.dumps(output, indent=2))


def _triage_to_dict(result: Optional[TriageResult]) -> Optional[dict]:
    if result is None:
        return None
    return {
        "summary": result.summary,
        "risk_level": result.risk_level,
        "remediation": result.remediation,
    }


def _shorten_id(resource_id: str) -> str:
    """Keep the last two path segments of an Azure resource ID for display."""
    parts = resource_id.split("/")
    if len(parts) > 4:
        return "…/" + "/".join(parts[-2:])
    return resource_id
