from pathlib import Path

import click


@click.group()
def cli() -> None:
    """A personal tee-time reservation assistant for municipal golf courses in Austin, TX."""


@cli.command()
def plan() -> None:
    """Interactive picker — build a plan file for an upcoming weekend."""
    raise NotImplementedError("plan: not implemented yet")


@cli.command()
@click.argument("plan_path", type=click.Path(exists=True, path_type=Path))
def schedule(plan_path: Path) -> None:
    """Compute release moment for a plan and schedule its execution."""
    raise NotImplementedError("schedule: not implemented yet")


@cli.command()
@click.argument("plan_path", type=click.Path(exists=True, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Run full flow but stop before the binding POST.")
@click.option("--confirm", is_flag=True, help="Required for a real booking (no-op without it).")
def run(plan_path: Path, dry_run: bool, confirm: bool) -> None:
    """Execute a booking run against a plan."""
    if not dry_run and not confirm:
        raise click.UsageError("Real runs require --confirm. Use --dry-run otherwise.")
    raise NotImplementedError("run: not implemented yet")


@cli.command()
@click.argument("confirmation_number")
@click.argument("tee_time")
def cancel(confirmation_number: str, tee_time: str) -> None:
    """Cancel a booking given its confirmation number and tee-time."""
    raise NotImplementedError("cancel: not implemented yet (flow not captured)")
