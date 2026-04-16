# DriftWise

Detect drift between your Terraform state and live Azure infrastructure.

```
driftwise compare ./terraform.tfstate
```

DriftWise connects to Azure, compares your Terraform state against what's actually running, and tells you what's been added, deleted, or modified outside of Terraform. Works with local state files or reads directly from Azure Blob Storage — nothing written to disk. Optionally enriches results with AI triage and month-to-date cost data.

---

## Installation

### Standalone binary (recommended)

Download the latest release from the [releases page](https://github.com/thos25/driftwise/releases) and put it somewhere in your PATH.

**Windows**
```powershell
# Download driftwise-windows-amd64.exe, rename it, move it to your PATH
Move-Item driftwise-windows-amd64.exe C:\tools\driftwise.exe
```

**Linux**
```bash
chmod +x driftwise-linux-amd64
sudo mv driftwise-linux-amd64 /usr/local/bin/driftwise
```

### pip

```bash
pip install driftwise
```

---

## Authentication

DriftWise uses `DefaultAzureCredential`, so any of the standard Azure auth methods work:

```bash
# Interactive login (easiest for local use)
az login

# Service principal (CI/CD or unattended)
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...
export AZURE_TENANT_ID=...
```

---

## Usage

```
driftwise compare [STATE_FILE] [OPTIONS]
```

| Option | Description |
|---|---|
| `--backend-config PATH` | Read state from Azure Blob Storage using a `backends.tfvars` file. |
| `--subscription ID` | Azure subscription ID. Falls back to `AZURE_SUBSCRIPTION_ID` env var. |
| `--all` | Also list resources that match (no drift). |
| `--costs` | Show month-to-date spend from Azure Cost Management alongside each resource. |
| `--json` | Output results as JSON — useful for piping into other tools. |
| `--verbose` / `-v` | Show warnings when optional steps fail (e.g. AI triage errors). |
| `--ignore PATTERNS` | Comma-separated resource name patterns to suppress (e.g. `NetworkWatcher*,cloud-shell-*`). |
| `--ignore-file PATH` | Path to a `.driftwise-ignore` YAML file. Defaults to `.driftwise-ignore` in the current directory. |

### Examples

```bash
# Local state file
driftwise compare ./terraform.tfstate

# Remote state via backends.tfvars (reads directly from Azure Blob — nothing written to disk)
driftwise compare --backend-config ./backends.tfvars

# Specify subscription explicitly
driftwise compare ./terraform.tfstate --subscription 00000000-0000-0000-0000-000000000000

# Show all resources, including clean ones
driftwise compare ./terraform.tfstate --all

# Show drift + MTD cost data
driftwise compare --backend-config ./backends.tfvars --costs

# Suppress specific resources inline
driftwise compare ./terraform.tfstate --ignore "NetworkWatcherRG,cloud-shell-*"

# Use a custom ignore file
driftwise compare ./terraform.tfstate --ignore-file ./my-ignore.yaml

# JSON output for scripting
driftwise compare ./terraform.tfstate --json | jq '.drift[]'
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | No drift detected |
| `1` | Error (auth failure, bad state file, etc.) |
| `2` | Drift detected |

Exit code `2` lets you gate CI/CD pipelines on drift — fail a pipeline if infrastructure has changed outside of Terraform.

---

## Remote state (Azure Blob Storage)

Enterprises rarely have state files locally. DriftWise can read state directly from Azure Blob Storage using the same `backends.tfvars` file you already use with Terraform — nothing is written to disk.

```bash
driftwise compare --backend-config ./backends.tfvars
```

Your `backends.tfvars` should contain:

```hcl
resource_group_name  = "my-tfstate-rg"
storage_account_name = "mystorageacct"
container_name       = "tfstate"
key                  = "prod/terraform.tfstate"
```

Auth uses `DefaultAzureCredential` — the same credential chain as the rest of the tool, so `az login` or a service principal covers it automatically. All other flags (`--costs`, `--ignore`, `--json`, etc.) work the same way regardless of whether state comes from a local file or blob storage.

---

## Ignoring resources

Some resources exist in every Azure subscription but aren't managed by Terraform — NetworkWatcher, Cloud Shell storage, etc. Use an ignore file to suppress them permanently, or `--ignore` for one-off runs.

### `.driftwise-ignore` file

Place a `.driftwise-ignore` file in the directory you run driftwise from:

```yaml
ignore:
  # Exact name match
  - name: "NetworkWatcherRG"

  # Wildcard match
  - name: "NetworkWatcher_*"
  - name: "cloud-shell-*"

  # Match by resource type
  - type: "microsoft.network/networkwatchers"

  # Only suppress when drift type is "added" (still report if deleted/modified)
  - name: "my-unmanaged-rg"
    drift_type: added
```

Supported match fields:

| Field | Description |
|---|---|
| `name` | Resource name — supports `*` wildcards, case-insensitive |
| `type` | Resource type (e.g. `microsoft.compute/virtualmachines`) — case-insensitive |
| `drift_type` | Optional: `added`, `deleted`, or `modified`. If omitted, suppresses all drift types. |

Suppressed resources are excluded from the report entirely. A footer note shows how many were suppressed so nothing is hidden silently.

---

## AI triage (optional)

If an OpenAI or Anthropic API key is present, DriftWise automatically runs AI triage on each drift item — plain-English summaries, risk scores (low / medium / high / critical), and remediation suggestions.

```bash
export OPENAI_API_KEY=sk-...
# or
export ANTHROPIC_API_KEY=sk-ant-...
```

No key? The tool works fine without it and shows a tip at the bottom of the report.

---

## How it works

1. Parses the Terraform state file to extract managed resources and their expected attributes
2. Fetches live resources from Azure using `DefaultAzureCredential`
3. Matches resources by Azure resource ID (case-insensitive)
4. Reports resources that are **deleted** (in state, missing in Azure), **modified** (attributes differ), or **added** (in Azure, not tracked in state)

Only attributes that Azure returns via the resource list API are compared — attributes that Terraform tracks but Azure doesn't surface are not flagged as drift.

---

## CI/CD example

```yaml
- name: Check for infrastructure drift
  env:
    AZURE_CLIENT_ID: ${{ secrets.AZURE_CLIENT_ID }}
    AZURE_CLIENT_SECRET: ${{ secrets.AZURE_CLIENT_SECRET }}
    AZURE_TENANT_ID: ${{ secrets.AZURE_TENANT_ID }}
  run: |
    # Local state file
    driftwise compare ./terraform.tfstate

    # Or read directly from Azure Blob Storage — no state file download needed
    driftwise compare --backend-config ./backends.tfvars
  # exits 2 if drift found — fails the pipeline
```

---

## License

[MIT](LICENSE)
