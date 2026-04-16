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
    invoke_without_command=True,
)


@app.callback()
def _root(ctx: typer.Context) -> None:
    """DriftWise — detect infrastructure drift."""


app.command("compare")(compare)

if __name__ == "__main__":
    app()
