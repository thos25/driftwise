"""
DriftWise CLI entrypoint.
Installed as the `driftwise` command via pyproject.toml.
"""
import typer
from cli.commands.compare import compare

app = typer.Typer(
    name="driftwise",
    help="Detect drift between Terraform state and live Azure infrastructure.",
    no_args_is_help=True,
    add_completion=False,
)

app.command("compare")(compare)

if __name__ == "__main__":
    app()
