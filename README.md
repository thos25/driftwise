# DriftWise

Detect drift between your Terraform state and live Azure infrastructure.

```
driftwise compare ./terraform.tfstate
```

DriftWise connects to Azure, compares your state file against what's actually running, and tells you what's been added, deleted, or modified outside of Terraform. Optionally enriches results with AI triage and month-to-date cost data.

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
driftwise compare STATE_FILE [OPTIONS]
```

| Option | Description |
|---|---|
| `--subscription ID` | Azure subscription ID. Falls back to `AZURE_SUBSCRIPTION_ID` env var. |
| `--all` | Also list resources that match (no drift). |
| `--costs` | Show month-to-date spend from Azure Cost Management alongside each resource. |
| `--json` | Output results as JSON — useful for piping into other tools. |

### Examples

```bash
# Basic drift check
driftwise compare ./terraform.tfstate

# Specify subscription explicitly
driftwise compare ./terraform.tfstate --subscription 00000000-0000-0000-0000-000000000000

# Show all resources, including clean ones
driftwise compare ./terraform.tfstate --all

# Show drift + MTD cost data
driftwise compare ./terraform.tfstate --costs

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
    driftwise compare ./terraform.tfstate
  # exits 2 if drift found — fails the pipeline
```

---

## License

[MIT](LICENSE)
